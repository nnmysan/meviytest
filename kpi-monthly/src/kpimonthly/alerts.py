"""アラート判定（業績）と、原因の手がかり（データから言える示唆）の付与。

誤検知を抑える仕組み
- 母数が小さい内訳は判定しない（min_denominator）
- 重要度：乖離が全社値に対して一定以上のものだけ（materiality）
- 率KPIの目標未達は標本誤差の2倍以上の差のみ（binomial）
- 季節性・成長は基準値に織り込み、「いつもと違う」ものだけを急変とする
- データ未着・成熟待ちの内訳は業績判定を保留（数字の欠けを業績悪化と誤認しない）
- 上位の内訳でも出ている場合は関連付け、下位だけで出ている場合は「内訳のみ悪化」と明示
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from .analysis import count_price, decompose
from .dq import PRIORITY_ORDER

DIM_JP = {"entity_code": "現地法人", "product_code": "商品", "division_code": "事業部", "team_code": "チーム",
          "section_code": "課", "industry": "業種", "size_class": "顧客規模", "customer_id": "顧客"}
CONCENTRATION = 0.6          # 変化の60%以上を1つの内訳が占め、
CONCENTRATION_RATIO = 1.5    # かつ普段の構成比の1.5倍以上 → 「特定の内訳に集中」
RULE_LABEL = {"target_miss": "目標未達", "abnormal": "急変（平常の範囲外）", "seasonal": "季節要因の可能性",
              "trend": "連続悪化", "large_order": "大口案件", "restatement": "過去値の修正"}


def alert_id(month: str, kpi: str, rule: str, scope_key: str) -> str:
    return "A" + hashlib.sha1(f"{month}|{kpi}|{rule}|{scope_key}".encode()).hexdigest()[:10]


def eval_month(kpi: dict, target_month: str, as_of: str) -> str:
    """成熟待ちのKPIは、直近の成熟済み月で判定する（例：転換率は見積から30日経過後に判定）。
    データ状態（未着など）ではなく、日付と成熟期間だけで決める。"""
    m = pd.Period(target_month, "M")
    while m.end_time.normalize() + pd.Timedelta(days=kpi.get("maturity_days", 0)) > pd.Timestamp(as_of):
        m -= 1
    return str(m)


def _fmt(kpi: dict, v: float) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if kpi["display"] == "percent":
        return f"{v * 100:.1f}%"
    if kpi["display"] == "yen":
        return f"{v / 1e6:,.1f}百万円" if abs(v) >= 1e6 else f"{v:,.0f}円"
    return f"{v:,.0f}件"


def _fmt_diff(kpi: dict, v: float) -> str:
    if v is None or np.isnan(v):
        return "—"
    if kpi["display"] == "percent":
        return f"{v * 100:+.1f}pt"
    if kpi["display"] == "yen":
        return f"{v / 1e6:+,.1f}百万円" if abs(v) >= 1e6 else f"{v:+,.0f}円"
    return f"{v:+,.0f}件"


def _materiality(th: dict, kpi: dict, dev: float, den: float, total_value: float, total_den: float) -> bool:
    m = th.get("materiality") or {}
    if "share_of_total" in m:
        return abs(dev) >= m["share_of_total"] * abs(total_value)
    if "units" in m:
        return abs(dev) * den >= m["units"]
    if "share_of_total_den" in m:
        return abs(dev) * den >= m["share_of_total_den"] * total_den
    return True


def evaluate(an: pd.DataFrame, fine: pd.DataFrame, cfg, target_month: str, as_of: str) -> tuple[pd.DataFrame, dict]:
    alert_scopes = {s["type"] for s in cfg.analysis["scopes"] if s.get("alerts")}
    glb = cfg.g
    sig_z = glb.get("significance_z", 2.0)
    info_scopes = set(glb.get("info_scopes", ["total", "entity", "product", "entity_product"]))
    trend_sig = glb.get("trend_significance", 2.0)
    rows, stats = [], {"suppressed_business_checks": 0, "eval_months": {}, "kpis_without_thresholds": []}
    for kpi in cfg.kpis:
        kid = kpi["id"]
        th = cfg.kpi_thresholds(kid)
        if not th:
            stats["kpis_without_thresholds"].append(kid)
            continue
        em = eval_month(kpi, target_month, as_of)
        stats["eval_months"][kid] = em
        pct = kpi["display"] == "percent"
        sign = 1.0 if kpi["direction"] == "higher_is_better" else -1.0
        cur = an[(an.kpi_id == kid) & (an.month == em) & an.scope_type.isin(alert_scopes)]
        if cur[cur.scope_type == "total"].empty:
            continue
        tot = cur[cur.scope_type == "total"].iloc[0]
        for r in cur.itertuples(index=False):
            if r.data_status in ("未着", "一部未着"):
                stats["suppressed_business_checks"] += 1
                continue
            if pd.isna(r.value) or r.small_sample:
                continue
            base = dict(month=em, kpi_id=kid, kpi_name=kpi["name"], scope_type=r.scope_type, scale=r.scale, scope_key=r.scope_key,
                        scope_label=r.scope_label, value=r.value, prev_value=r.prev_value, diff=r.diff, pct=r.pct,
                        yoy_value=r.yoy_value, target=r.target, baseline=r.baseline, z=r.z, den=r.den,
                        consecutive_miss=int(r.consecutive_miss), data_status=r.data_status, note=r.note,
                        entity_code=r.entity_code, product_code=r.product_code, seasonal_factor=r.seasonal_factor)
            fired = set()
            # 1) 目標未達
            tm = th.get("target_miss")
            if tm and not pd.isna(r.target):
                gap = r.value - r.target
                if pct:
                    bad = gap * sign * 100
                    se = np.sqrt(max(r.target * (1 - r.target), 1e-9) / r.den) if kpi.get("binomial") and r.den else 0
                    significant = abs(gap) >= sig_z * se
                    level = "P1" if bad <= -tm["p1"] else "P2" if bad <= -tm["p2"] else None
                else:
                    ach = r.value / r.target if r.target else np.nan
                    significant = True
                    level = "P1" if ach < tm["p1"] else "P2" if ach < tm["p2"] else None
                if level and significant and _materiality(th, kpi, gap, r.den, tot.value, tot.den):
                    fired.add("target_miss")
                    msg = (f"{r.scope_label}の{kpi['name']}は {_fmt(kpi, r.value)}（目標 {_fmt(kpi, r.target)}、"
                           f"目標差 {_fmt_diff(kpi, gap)}）。")
                    if r.consecutive_miss >= 2:
                        msg += f"{int(r.consecutive_miss)}か月連続で目標を下回っています。"
                    rows.append(base | dict(rule="target_miss", priority=level, message=msg, reference=r.target))
            # 2) 急変（季節性・成長を織り込んだ基準値からの乖離）
            ab = th.get("abnormal")
            if ab and not pd.isna(r.z) and r.z * sign < 0:
                dev = r.value - r.baseline
                level = "P1" if abs(r.z) >= ab["p1"] else "P2" if abs(r.z) >= ab["p2"] else None
                # 金額・件数の影響が全社の一定割合を超えるものは統計的な外れ度にかかわらず P1
                if level == "P2" and ab.get("p1_share_of_total") and abs(dev) >= ab["p1_share_of_total"] * abs(tot.value):
                    level = "P1"
                if level and _materiality(th, kpi, dev, r.den, tot.value, tot.den):
                    fired.add("abnormal")
                    msg = (f"{r.scope_label}の{kpi['name']}は {_fmt(kpi, r.value)}。季節性と直近の傾向から見込まれる "
                           f"{_fmt(kpi, r.baseline)} に対して {_fmt_diff(kpi, dev)}（平常のばらつきの {abs(r.z):.1f} 倍）。")
                    if not pd.isna(r.prev_value):
                        msg += f"前月 {_fmt(kpi, r.prev_value)}（{_fmt_diff(kpi, r.diff)}）。"
                    rows.append(base | dict(rule="abnormal", priority=level, message=msg, reference=r.baseline))
            # 3) 季節要因の可能性（前月比は大きく悪化しているが、過去の同月も同じように下がっており、季節調整後は平常）
            si = glb.get("seasonal_info")
            if si and not pct and "abnormal" not in fired and r.scope_type in info_scopes and not pd.isna(r.pct) \
                    and not pd.isna(r.seasonal_factor) and not pd.isna(r.z):
                if r.pct * sign <= -si["mom_drop"] and (r.seasonal_factor - 1) * sign <= -si["mom_drop"] / 2 \
                        and abs(r.z) < si.get("max_z", 2.0) and _materiality(th, kpi, r.diff, r.den, tot.value, tot.den):
                    yoy = f"、前年同月比 {r.value / r.yoy_value - 1:+.0%}" if r.yoy_value else ""
                    rows.append(base | dict(rule="seasonal", priority="INFO", reference=r.prev_value, message=(
                        f"{r.scope_label}の{kpi['name']}は前月比 {r.pct:+.0%}{yoy}。過去の同じ月も直前3か月の平均より"
                        f"{1 - r.seasonal_factor:.0%}程度低く、季節性を考慮した見込みの範囲内です（業績悪化とは判定していません）。")))
            # 4) 連続悪化
            tr = th.get("trend")
            if tr and not fired and r.scope_type in info_scopes and r.consecutive_worse >= tr["months"] \
                    and not pd.isna(r.scale):
                hist = an[(an.kpi_id == kid) & (an.scope_key == r.scope_key) & (an.month <= em)].sort_values("month")
                if len(hist) > tr["months"]:
                    v0 = hist.value.iloc[-1 - tr["months"]]
                    change = (r.value - v0) * 100 if pct else (r.value / v0 - 1 if v0 else np.nan)
                    rel = (r.value - v0) if kpi["type"] == "ratio" else change
                    if not pd.isna(change) and change * sign <= -tr["cum"] and abs(rel) >= trend_sig * r.scale and \
                            _materiality(th, kpi, r.value - v0, r.den, tot.value, tot.den):
                        rows.append(base | dict(rule="trend", priority="P3", reference=v0, message=(
                            f"{r.scope_label}の{kpi['name']}は {int(r.consecutive_worse)}か月連続で悪化"
                            f"（{tr['months']}か月前 {_fmt(kpi, v0)} → {_fmt(kpi, r.value)}）。")))
            # 5) 大口案件（情報）
            if kid == "order_amount" and r.scope_type == "entity_product" and r.note.startswith("当月に大口"):
                rows.append(base | dict(rule="large_order", priority="INFO", reference=np.nan,
                                        message=f"{r.scope_label}: {r.note}翌月以降の反動に注意。"))
    al = pd.DataFrame(rows)
    if al.empty:
        return _empty_alerts(), stats
    al["category"] = "業績"
    al["rule_label"] = al.rule.map(RULE_LABEL)
    al["alert_id"] = [alert_id(m, k, ru, s) for m, k, ru, s in zip(al.month, al.kpi_id, al.rule, al.scope_key)]
    _relate(al, cfg)
    _explain(al, fine, cfg)
    al["_o"] = al.priority.map(PRIORITY_ORDER)
    al = al.sort_values(["_o", "kpi_id", "scope_type"]).drop(columns="_o").reset_index(drop=True)
    return al, stats


def _empty_alerts() -> pd.DataFrame:
    return pd.DataFrame(columns=["alert_id", "month", "kpi_id", "kpi_name", "rule", "rule_label", "priority", "category",
                                 "scope_type", "scope_key", "scope_label", "message", "label", "related_alert_id",
                                 "cause_type", "signals", "value", "reference"])


def _relate(al: pd.DataFrame, cfg) -> None:
    """上位の内訳との関係付け。上位でも出ていれば関連付け、出ていなければ『内訳のみ悪化』。"""
    parents = {s["type"]: s.get("parents", []) for s in cfg.analysis["scopes"]}
    main = al[al.rule.isin(["target_miss", "abnormal"])]
    idx = {(r.kpi_id, r.scope_type, r.scope_key): r.alert_id for r in main.itertuples()}
    labels, related = [], []
    for r in al.itertuples():
        lab, rel = "", ""
        if r.rule in ("target_miss", "abnormal") and r.scope_type != "total":
            pks = []
            for pt in parents.get(r.scope_type, []):
                if pt == "total":
                    pks.append(("total", "ALL"))
                else:
                    dims = next(s["dims"] for s in cfg.analysis["scopes"] if s["type"] == pt)
                    kv = dict(x.split("=", 1) for x in r.scope_key.split("|"))
                    if all(d in kv for d in dims):
                        pks.append((pt, "|".join(f"{d}={kv[d]}" for d in dims)))
            hits = [idx[(r.kpi_id, t, k)] for t, k in pks if (r.kpi_id, t, k) in idx]
            if hits:
                rel = ",".join(hits)
            elif pks:
                lab = "内訳のみ悪化"
        labels.append(lab)
        related.append(rel)
    al["label"] = labels
    al["related_alert_id"] = related


def _explain(al: pd.DataFrame, fine: pd.DataFrame, cfg) -> None:
    """前月からの変化を分解し、『データから言える示唆』を付ける（原因の断定はしない）。"""
    drill = cfg.analysis.get("drilldown", {})
    mix_dim = cfg.analysis.get("mix_dimension", "product_code")
    labels = cfg._label_maps if hasattr(cfg, "_label_maps") else {}
    causes, signals, contribs = [], [], []
    for r in al.itertuples():
        kpi = cfg.kpi(r.kpi_id)
        if r.rule not in ("target_miss", "abnormal", "trend") or pd.isna(r.prev_value):
            causes.append("")
            signals.append([])
            contribs.append([])
            continue
        filt = {} if r.scope_key == "ALL" else dict(x.split("=", 1) for x in r.scope_key.split("|"))
        prev = str(pd.Period(r.month, "M") - 1)
        sig, cause, top = [], "", []
        gap = r.value - r.reference if not pd.isna(r.reference) else np.nan
        if r.rule == "target_miss" and not pd.isna(gap) and abs(r.diff) < 0.5 * abs(gap):
            # 前月からの変化は小さい＝今月新たに起きた変化ではない。前月差の分解はノイズになるため行わない
            n = int(r.consecutive_miss)
            causes.append("persistent")
            signals.append([f"前月差は {_fmt_diff(kpi, r.diff)} と小さく、目標との差（{_fmt_diff(kpi, gap)}）は今月新たに生じたものではありません"
                            + (f"（{n}か月連続で未達）。" if n >= 2 else "。") + "未達が始まった月の前後を比較して要因を確認してください。"])
            contribs.append([])
            continue
        if kpi["type"] == "ratio" and kpi["display"] == "percent":
            dcmp = decompose(fine, kpi, filt, mix_dim, r.month, prev) if mix_dim not in filt else None
            if dcmp is not None and len(dcmp) > 1:
                mix, rate = dcmp.mix.sum(), dcmp.rate.sum()
                total = mix + rate
                if total and abs(mix) >= 0.6 * abs(total) and np.sign(mix) == np.sign(total):
                    cause = "mix"
                elif total and abs(rate) >= 0.6 * abs(total) and np.sign(rate) == np.sign(total):
                    cause = "rate"
                sig.append(f"前月差 {total * 100:+.2f}pt のうち、商品構成の変化による分 {mix * 100:+.2f}pt、"
                           f"各商品の率の変化による分 {rate * 100:+.2f}pt。")
        if kpi.get("drivers", {}).get("count_kpi"):
            cp = count_price(fine, r.kpi_id, kpi["drivers"]["count_kpi"], filt, r.month, prev)
            if cp:
                t = cp["total"]
                sig.append(f"前月差 {t / 1e6:+,.1f}百万円 のうち、件数の変化による分 {cp['count_effect'] / 1e6:+,.1f}百万円"
                           f"（{cp['count0']:,.0f}→{cp['count1']:,.0f}件）、単価の変化による分 {cp['price_effect'] / 1e6:+,.1f}百万円。")
                if t and abs(cp["count_effect"]) >= 0.6 * abs(t) and np.sign(cp["count_effect"]) == np.sign(t):
                    cause = cause or "count"
                elif t and abs(cp["price_effect"]) >= 0.6 * abs(t) and np.sign(cp["price_effect"]) == np.sign(t):
                    cause = cause or "price"
        for by in drill.get(r.scope_type, []):
            d = decompose(fine, kpi, filt, by, r.month, prev)
            if len(d) < 2:
                continue
            total = d.contribution.sum()
            if not total:
                continue
            base = d.den1 if kpi["type"] == "ratio" else d.num0.abs()
            d = d.assign(base_share=base / base.sum() if base.sum() else np.nan)
            worst = d.sort_values("contribution", ascending=kpi["direction"] == "higher_is_better").head(3)
            names = labels.get(by, {})
            items = []
            for w in worst.itertuples():
                if w.contribution * total <= 0:
                    continue
                share = float(w.contribution / total)
                items.append(dict(dim=by, segment=w.segment, label=names.get(w.segment, w.segment),
                                  contribution=float(w.contribution), share=share, base_share=float(w.base_share),
                                  concentrated=bool(share >= CONCENTRATION and share >= CONCENTRATION_RATIO * w.base_share)))
            if items:
                top.extend(items)
                if items[0]["concentrated"] and not cause:
                    cause = "concentrated"
                txt = "、".join(f"{i['label']}（{_fmt_diff(kpi, i['contribution'])}、寄与 {i['share']:.0%}）" for i in items)
                sig.append(f"前月からの変化が大きい{DIM_JP.get(by, by)}: {txt}")
        causes.append(cause)
        signals.append(sig)
        contribs.append(top)
    al["cause_type"] = causes
    al["signals"] = signals
    al["contributors"] = contribs
    al["top_contributor"] = [c[0]["segment"] if c else "" for c in contribs]
