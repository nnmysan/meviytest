"""サプライヤの生産充足率（負荷率）・アラート・打ち手・通知。

充足率（出荷日ベース）
    件数ベース = 出荷した（出荷予定の）明細数 ÷（日あたり供給可能件数 × 平日数）
    数量ベース = 出荷した（出荷予定の）数量   ÷（日あたり供給可能数量 × 平日数）
高いほど供給能力が逼迫している。目標は 80% 以下（仮の閾値は thresholds.yaml の supplier）。

判定する3つの観点
    締め月   : 直近の締め月（月次定例の対象）
    当月累計 : 当月1日〜データ最終日（毎朝更新）
    今後     : 取込日から N 営業日先までの出荷予定（受注済み分。先の逼迫を早めに知る）
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

COMBO = ["supplier_code", "material", "part_size", "surface", "heat"]
LEVELS = [("over", "供給能力超過（100%超）", "P1"), ("p1", "充足率95%超", "P1"), ("p2", "充足率80%超", "P2")]
LEVEL_RANK = {"": 0, "p2": 1, "p1": 2, "over": 3}
BASIS_LABEL = {"month": "締め月", "mtd": "当月累計", "forward": "今後の出荷予定"}


def weekdays(start, end_inclusive) -> int:
    s = pd.Timestamp(start).date()
    e = (pd.Timestamp(end_inclusive) + pd.Timedelta(days=1)).date()
    return int(np.busday_count(s, e)) if e > s else 0


def combo_key(r) -> str:
    return "|".join(f"{k}={getattr(r, k) if not isinstance(r, dict) else r[k]}" for k in COMBO)


def combo_label(r, sup_names: dict) -> str:
    g = (lambda k: r[k]) if isinstance(r, dict) else (lambda k: getattr(r, k))
    return f"{g('supplier_code')}（{sup_names.get(g('supplier_code'), '')}）× {g('material')} × {g('part_size')} × 表面:{g('surface')} × 熱処理:{g('heat')}"


def demand(con: duckdb.DuckDBPyConnection, start: str, end: str) -> pd.DataFrame:
    return con.execute(f"""
        SELECT {', '.join(COMBO)}, plan_day AS day, COUNT(*) AS lines, SUM(quantity) AS qty, SUM(purchase_jpy) AS purchase,
               SUM(CASE WHEN planned THEN 1 ELSE 0 END) AS planned_lines
        FROM v_supply_lines WHERE plan_day BETWEEN DATE '{start}' AND DATE '{end}'
        GROUP BY ALL ORDER BY ALL""").df().assign(day=lambda d: pd.to_datetime(d.day))


def demand_monthly(con: duckdb.DuckDBPyConnection, end: str) -> pd.DataFrame:
    return con.execute(f"""
        SELECT {', '.join(COMBO)}, strftime(plan_day, '%Y-%m') AS month, COUNT(*) AS lines, SUM(quantity) AS qty,
               SUM(purchase_jpy) AS purchase
        FROM v_supply_lines WHERE plan_day <= DATE '{end}' GROUP BY ALL ORDER BY ALL""").df()


def quality_delivery(con: duckdb.DuckDBPyConnection, start: str, end: str, grain: str) -> pd.DataFrame:
    """サプライヤ別の納期遵守・品質不良（納期到来明細・出荷明細ベース）。grain: 'day' or 'month'。"""
    g = "CAST(day AS VARCHAR)" if grain == "day" else "month"
    due = con.execute(f"""SELECT {', '.join(COMBO)}, {g} AS period, COUNT(*) AS due_lines, SUM(on_time) AS on_time
        FROM v_due_lines WHERE day BETWEEN DATE '{start}' AND DATE '{end}' GROUP BY ALL""").df()
    shp = con.execute(f"""SELECT {', '.join(COMBO)}, {g} AS period, COUNT(*) AS shipped, SUM(defect_count) AS defects
        FROM v_shipped_lines WHERE day BETWEEN DATE '{start}' AND DATE '{end}' GROUP BY ALL""").df()
    out = due.merge(shp, on=COMBO + ["period"], how="outer").fillna({"due_lines": 0, "on_time": 0, "shipped": 0, "defects": 0})
    return out.sort_values(COMBO + ["period"]).reset_index(drop=True)


def _loads(dem: pd.DataFrame, cap: pd.DataFrame, wd: int) -> pd.DataFrame:
    """組み合わせ単位の負荷率。需要のない組み合わせも能力マスタから0として含める。"""
    g = dem.groupby(COMBO)[["lines", "qty", "purchase"]].sum().reset_index() if len(dem) else \
        pd.DataFrame(columns=COMBO + ["lines", "qty", "purchase"])
    c = cap.merge(g, on=COMBO, how="left").fillna({"lines": 0, "qty": 0, "purchase": 0})
    c["wd"] = wd
    c["cap_lines"] = c.cap_count_day * wd
    c["cap_qty"] = c.cap_qty_day * wd
    with np.errstate(divide="ignore", invalid="ignore"):
        c["load_count"] = np.where(c.cap_lines > 0, c.lines / c.cap_lines, np.nan)
        c["load_qty"] = np.where(c.cap_qty > 0, c.qty / c.cap_qty, np.nan)
    return c


def _supplier_loads(c: pd.DataFrame) -> pd.DataFrame:
    s = c.groupby(["supplier_code", "entity_code"])[["lines", "qty", "purchase", "cap_lines", "cap_qty"]].sum().reset_index()
    s["wd"] = c.wd.iloc[0] if len(c) else 0
    s["load_count"] = s.lines / s.cap_lines
    s["load_qty"] = s.qty / s.cap_qty
    return s


def _level(load: float, th: dict) -> str:
    if pd.isna(load):
        return ""
    lv = th["levels"]
    if load > lv["over"]:
        return "over"
    if load > lv["p1"]:
        return "p1"
    if load > lv["p2"]:
        return "p2"
    return ""


def analyze_suppliers(con, masters: dict, cfg, target_month: str, as_of: str, data_end: str, state_dir: Path,
                      cal) -> dict:
    th = cfg.g.get("supplier") or {}
    if not th or "suppliers" not in masters:
        return {"enabled": False}
    sup = masters["suppliers"]
    sup_names = dict(zip(sup.supplier_code, sup.supplier_name))
    cap = masters["supplier_capacity"].merge(sup[["supplier_code", "entity_code"]], on="supplier_code", how="left")
    as_of_ts, end_ts = pd.Timestamp(as_of), pd.Timestamp(data_end)
    fwd_days = int(th.get("forward_business_days", 10))
    fwd_end = pd.Timestamp(np.busday_offset(as_of_ts.date(), fwd_days - 1, roll="forward"))
    tm = pd.Period(target_month, "M")
    cm = pd.Period(data_end[:7], "M")

    bases = {}
    d_month = demand(con, str(tm.start_time.date()), str(tm.end_time.date()))
    bases["month"] = (target_month, _loads(d_month, cap, weekdays(tm.start_time, tm.end_time)))
    if cm > tm:
        wd = weekdays(cm.start_time, end_ts)
        if wd >= th.get("mtd_min_business_days", 5):
            d_mtd = demand(con, str(cm.start_time.date()), data_end)
            bases["mtd"] = (f"{cm}（{data_end[5:]}まで）", _loads(d_mtd, cap, wd))
    d_fwd = demand(con, str(as_of_ts.date()), str(fwd_end.date()))
    bases["forward"] = (f"{as_of[5:]}〜{fwd_end:%m-%d}", _loads(d_fwd, cap, weekdays(as_of_ts, fwd_end)))

    # 判定（組み合わせ・サプライヤ）。最低件数に満たない組み合わせは単独では判定しない。
    min_cap = th.get("min_capacity_lines_month", 40)
    min_dem = th.get("min_demand_lines", 15)
    evals = []
    for basis, (label, c) in bases.items():
        # 窓が短いほど件数が少なくぶれるため、最低規模は窓の長さにかかわらず同じ件数で判定する
        ok = (c.cap_lines >= min_cap) & (c.lines >= min_dem)
        for r in c[ok].itertuples(index=False):
            evals.append(dict(scope_type="supplier_combo", scope_key=combo_key(r), basis=basis, period=label,
                              supplier_code=r.supplier_code, entity_code=r.entity_code, material=r.material,
                              part_size=r.part_size, surface=r.surface, heat=r.heat, lines=r.lines, qty=r.qty,
                              purchase=r.purchase, cap_lines=r.cap_lines, cap_qty=r.cap_qty, wd=r.wd,
                              load_count=r.load_count, load_qty=r.load_qty, cap_count_day=r.cap_count_day,
                              cap_qty_day=r.cap_qty_day))
        for r in _supplier_loads(c).itertuples(index=False):
            if r.cap_lines < min_cap:
                continue
            evals.append(dict(scope_type="supplier", scope_key=f"supplier_code={r.supplier_code}", basis=basis,
                              period=label, supplier_code=r.supplier_code, entity_code=r.entity_code, material="*",
                              part_size="*", surface="*", heat="*", lines=r.lines, qty=r.qty, purchase=r.purchase,
                              cap_lines=r.cap_lines, cap_qty=r.cap_qty, wd=r.wd, load_count=r.load_count,
                              load_qty=r.load_qty, cap_count_day=r.cap_lines / max(r.wd, 1), cap_qty_day=r.cap_qty / max(r.wd, 1)))
    ev = pd.DataFrame(evals)
    alerts, actions = [], []
    if len(ev):
        ev["max_load"] = ev[["load_count", "load_qty"]].max(axis=1)
        ev["level"] = ev.max_load.map(lambda x: _level(x, th))
        hot = ev[ev.level != ""]
        for key, g in hot.groupby("scope_key", sort=False):
            g = g.assign(rank=g.level.map(LEVEL_RANK)).sort_values(["rank", "max_load"], ascending=False)
            rep = g.iloc[0]
            alerts.append(_alert(rep, g, ev, sup_names, target_month))
        # サプライヤ全体のアラートは、配下の組み合わせで出ていない場合だけアクションを作る
        combo_sups = {a["supplier_code"] for a in alerts if a["scope_type"] == "supplier_combo"}
        for a in alerts:
            if a["scope_type"] == "supplier" and a["supplier_code"] in combo_sups:
                continue
            actions.append(_action(a, ev, bases, cap, sup, sup_names, th, as_of, cal))
    notifications, new_state = _notifications(alerts, actions, sup, state_dir, as_of)
    tables = {b: c.assign(basis=b, period=lbl) for b, (lbl, c) in bases.items()}
    return {"enabled": True, "alerts": alerts, "actions": actions, "notifications": notifications,
            "evals": ev, "bases": tables, "state": new_state, "forward_end": str(fwd_end.date()), "threshold": th}


def _alert(rep, g, ev, sup_names, target_month) -> dict:
    lvl = next(x for x in LEVELS if x[0] == rep.level)
    label = combo_label(rep._asdict() if hasattr(rep, "_asdict") else rep.to_dict(), sup_names) \
        if rep.scope_type == "supplier_combo" else f"{rep.supplier_code}（{sup_names.get(rep.supplier_code, '')}）全体"
    parts = []
    for r in g.itertuples():
        parts.append(f"{BASIS_LABEL[r.basis]}（{r.period}）: 件数 {r.load_count:.0%}・数量 {r.load_qty:.0%}"
                     f"（{r.lines:,.0f}件／能力 {r.cap_lines:,.0f}件）")
    msg = f"{label} の生産充足率が {rep.max_load:.0%}（目標80%以下）。" + " / ".join(parts)
    aid = "S" + hashlib.sha1(f"{target_month}|supplier_load|{rep.scope_key}".encode()).hexdigest()[:10]
    signals = []
    if rep.scope_type == "supplier_combo":
        sib = ev[(ev.basis == rep.basis) & (ev.scope_type == "supplier_combo") & (ev.supplier_code == rep.supplier_code)]
        signals.append(f"同じサプライヤの他の組み合わせの充足率（{BASIS_LABEL[rep.basis]}）: 中央値 {sib.max_load.median():.0%}。"
                       + ("この組み合わせに負荷が集中しています。" if rep.max_load > sib.max_load.median() + 0.2 else ""))
    return dict(alert_id=aid, month=target_month, kpi_id="supplier_load", kpi_name="生産充足率", rule="capacity",
                rule_label=lvl[1], priority=lvl[2], category="供給", scope_type=rep.scope_type, scope_key=rep.scope_key,
                scope_label=label, message=msg, label=BASIS_LABEL[rep.basis], related_alert_id="", cause_type="",
                signals=signals, contributors=[], value=float(rep.max_load), reference=0.8, data_status="OK",
                supplier_code=rep.supplier_code, entity_code=rep.entity_code, level=rep.level, basis=rep.basis,
                load_count=float(rep.load_count), load_qty=float(rep.load_qty))


def _action(a: dict, ev: pd.DataFrame, bases: dict, cap: pd.DataFrame, sup: pd.DataFrame, sup_names: dict, th: dict,
            as_of: str, cal) -> dict:
    target = th.get("target_load", 0.8)
    rep = ev[(ev.scope_key == a["scope_key"]) & (ev.basis == a["basis"])].iloc[0]
    wd = max(int(rep.wd), 1)
    dem_day, dem_qty_day = rep.lines / wd, rep.qty / wd
    need_day = dem_day / target - rep.cap_count_day
    need_qty = dem_qty_day / target - rep.cap_qty_day
    excess_day = max(dem_day - rep.cap_count_day * target, 0)
    facts = [a["message"],
             f"1日あたり：出荷 {dem_day:,.1f}件（数量 {dem_qty_day:,.0f}）に対し供給能力 {rep.cap_count_day:,.1f}件（数量 {rep.cap_qty_day:,.0f}）。",
             f"対象期間の発注金額 {rep.purchase / 1e6:,.1f}百万円。"]
    proposals, signals = [], list(a["signals"])
    # 振替候補：同じ法人で、同じ材質・サイズ・表面処理・熱処理を扱い、負荷に余裕のあるサプライヤ
    cands = pd.DataFrame()
    if a["scope_type"] == "supplier_combo":
        _, tbl = bases[a["basis"]]
        same = tbl[(tbl.entity_code == rep.entity_code) & (tbl.supplier_code != rep.supplier_code)
                   & (tbl.material == rep.material) & (tbl.part_size == rep.part_size) & (tbl.surface == rep.surface)
                   & (tbl.heat == rep.heat)].copy()
        same["headroom_day"] = same.cap_count_day * target - same.lines / wd
        cands = same[(same.load_count < th.get("transfer_max_load", 0.6)) & (same.headroom_day > 0)] \
            .sort_values("headroom_day", ascending=False).head(3)
    head = float(cands.headroom_day.sum()) if len(cands) else 0.0
    if len(cands):
        names = "、".join(f"{r.supplier_code}（充足率 {r.load_count:.0%}、余力 約{r.headroom_day:,.1f}件/日）" for r in cands.itertuples())
        signals.append(f"同じ仕様を扱える他のサプライヤ：{names}。")
        proposals.append({"type": "振替", "text": f"余力のあるサプライヤへ1日あたり約{min(excess_day, head):,.1f}件を振り替える"
                          + ("（余力で超過分を吸収できる見込み）" if head >= excess_day else "（余力だけでは不足。拡大交渉と併用）"),
                          "basis": names})
    if need_day > 0 or need_qty > 0:
        proposals.append({"type": "拡大交渉", "text": f"{rep.supplier_code} と生産領域の拡大を交渉する（80%以下に収めるには 1日あたり"
                          f" +{max(need_day, 0):,.1f}件・数量 +{max(need_qty, 0):,.0f} の能力が必要）",
                          "basis": "必要能力 ＝ 1日あたりの出荷 ÷ 0.8 − 現在の能力"})
    fwd = bases.get("forward")
    if fwd is not None and a["basis"] == "forward":
        proposals.append({"type": "平準化", "text": "今後の出荷予定のうち、ピーク日の案件を前後の余裕がある日へ移せないか、納期の調整余地を営業と確認する",
                          "basis": f"{BASIS_LABEL['forward']}（{fwd[0]}）"})
    if a["scope_type"] == "supplier_combo" and not len(cands):
        proposals.append({"type": "新規開拓", "text": "同じ仕様を扱えるサプライヤが同じ法人内にないため、新規サプライヤの開拓・他法人のサプライヤ活用を検討する",
                          "basis": "振替候補 0 社"})
    if head >= excess_day and len(cands):
        proposals.sort(key=lambda x: {"振替": 0, "拡大交渉": 1}.get(x["type"], 2))
    hyps = [{"text": "受注（需要）の増加", "status": "未検証", "basis": "出荷の増減を前月と比較して確認"},
            {"text": "サプライヤの供給能力の低下（設備停止・人員不足）または能力マスタの未更新", "status": "未検証",
             "basis": "サプライヤ担当が能力の実態を確認"}]
    owner_row = sup[sup.supplier_code == rep.supplier_code]
    owner = owner_row.owner.iloc[0] if len(owner_row) else "購買 担当（仮）"
    manager = owner_row.manager.iloc[0] if len(owner_row) else ""
    due = cal.add_business_days(pd.Timestamp(as_of), 5 if a["priority"] == "P1" else 10)
    impact_amt = rep.purchase * (excess_day / dem_day) if dem_day else 0
    return {
        "action_id": "X" + a["alert_id"][1:], "alert_id": a["alert_id"], "month": a["month"], "kpi_id": "supplier_load",
        "kpi_name": "生産充足率", "scope_key": a["scope_key"], "scope_label": a["scope_label"], "priority": a["priority"],
        "title": f"【確認・交渉】{a['scope_label']} の供給逼迫（{a['rule_label']}）",
        "verb": "確認", "facts": facts, "signals": signals, "hypotheses": hyps,
        "checks": [p["text"] for p in proposals] + ["サプライヤに現在の供給能力と今後の見通しを確認する"],
        "proposals": proposals,
        "impact": {"value": impact_amt, "unit": "円",
                   "text": f"80%を超える分に相当する発注額 約{impact_amt / 1e6:,.1f}百万円（対象期間、試算）"},
        "owner": owner, "manager": manager, "due_date": due.strftime("%Y-%m-%d"), "status": "下書き",
        "confidence": "充足率は事実。原因（需要増か能力低下か）は未検証", "category": "供給", "related_scopes": [],
    }


def _notifications(alerts: list[dict], actions: list[dict], sup: pd.DataFrame, state_dir: Path, as_of: str):
    """前回の状態と比べ、新規・悪化・解消のときだけ通知する（同じ事象を毎日通知しない）。"""
    path = Path(state_dir) / "supplier_alert_state.json"
    prev = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cur = {a["scope_key"]: a["level"] for a in alerts}
    act_by_alert = {x["alert_id"]: x for x in actions}
    owners = {r.supplier_code: (r.owner, r.manager) for r in sup.itertuples()}
    out = []
    for a in alerts:
        before = prev.get(a["scope_key"], "")
        if LEVEL_RANK[a["level"]] <= LEVEL_RANK.get(before, 0):
            continue
        reason = "新規" if not before else "悪化"
        to, cc = owners.get(a["supplier_code"], ("", ""))
        act = act_by_alert.get(a["alert_id"])
        out.append(dict(notify_id="N" + hashlib.sha1(f"{as_of}|{a['scope_key']}|{a['level']}".encode()).hexdigest()[:10],
                        created_at=f"{as_of} 08:00", channel="Teams", to=to, cc=cc, priority=a["priority"], reason=reason,
                        title=f"［{a['priority']}・{reason}］{a['scope_label']}：{a['rule_label']}",
                        message=a["message"] + (f" 打ち手の候補：{act['proposals'][0]['text']}" if act and act.get("proposals") else ""),
                        alert_id=a["alert_id"], scope_key=a["scope_key"], status="送信予定（ダミー）"))
    sup_owner = {f"supplier_code={c}": o for c, o in owners.items()}
    for key, before in prev.items():
        if key not in cur and before:
            sc = key.split("|")[0].split("=")[1]
            to, cc = owners.get(sc, ("", ""))
            out.append(dict(notify_id="N" + hashlib.sha1(f"{as_of}|{key}|resolved".encode()).hexdigest()[:10],
                            created_at=f"{as_of} 08:00", channel="Teams", to=to, cc="", priority="INFO", reason="解消",
                            title=f"［解消］{key.replace('|', ' × ')}：充足率が80%以下に戻りました", message="",
                            alert_id="", scope_key=key, status="送信予定（ダミー）"))
    return out, cur


def save_state(state_dir: Path, state: dict) -> None:
    (Path(state_dir) / "supplier_alert_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
