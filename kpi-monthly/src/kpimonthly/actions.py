"""アクション提案（下書き）の生成。

各提案は「事実（データで確認済み）」「示唆（分解結果から言えること）」「仮説（未検証）」を分けて持つ。
仮説が人の確認で「支持」になるまで、提案は「確認・調査」にとどめ、施策の実施を断定しない。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .alerts import _fmt, _fmt_diff

VERB = "確認"


def resolve_owner(owners: pd.DataFrame, kpi_id: str, entity: str | None, product: str | None, default: str) -> tuple[str, str]:
    best, score = None, -1
    for r in owners.itertuples():
        s = 0
        ok = True
        for field, val, w in (("entity_code", entity, 2), ("product_code", product, 2), ("kpi_id", kpi_id, 1)):
            rv = getattr(r, field)
            if rv == "*":
                continue
            if val is None or (isinstance(val, float) and np.isnan(val)) or rv != val:
                ok = False
                break
            s += w
        if ok and s > score:
            best, score = r, s
    if best is None:
        return default, ""
    return best.owner, best.manager


def _impact(kpi: dict, a) -> dict:
    ref = a.reference if not pd.isna(a.reference) else a.prev_value
    if pd.isna(ref):
        return {"value": None, "unit": "", "text": "算定不可（比較対象なし）"}
    dev = a.value - ref
    basis = {"target_miss": "目標", "abnormal": "見込み値（季節性・傾向考慮）", "trend": "3か月前"}.get(a.rule, "比較値")
    if kpi["display"] == "percent":
        units = dev * a.den
        unit_name = {"on_time_delivery": "件の遅延明細", "defect_rate": "件の不良", "quote_conversion": "件の受注機会"}.get(kpi["id"])
        if kpi["id"] == "gross_margin":
            return {"value": dev * a.den, "unit": "円", "text": f"粗利への影響 約{dev * a.den / 1e6:+,.1f}百万円（{basis}の率との差×当月受注金額、試算）"}
        if unit_name:
            n = -units if kpi["direction"] == "lower_is_better" else units
            return {"value": units, "unit": "件", "text": f"{basis}比で 約{abs(units):,.0f}{unit_name}の{'悪化' if n < 0 else '改善'}（率の差×当月の母数、試算）"}
    return {"value": dev, "unit": "円" if kpi["display"] == "yen" else "件",
            "text": f"{basis}比 {_fmt_diff(kpi, dev)}（試算）"}


MAIN_RULES = ("target_miss", "abnormal")
PRIMARY_SCOPES = ("total", "entity", "product", "entity_product")  # 法人×商品の階層。組織・業種・規模は別の切り口


def select_for_action(alerts: pd.DataFrame) -> pd.DataFrame:
    """同じ事象が複数の内訳に出るとき、アクションを置く内訳を1つに絞る。
    - 上位の変化が特定の下位内訳に集中（寄与60%以上）し、その下位でもアラートが出ている → 下位（最も具体的な内訳）に置く
    - 変化が広く分散している → 上位の内訳に置き、下位は上位のアクションに含める
    - 同じ内訳に目標未達と急変の両方 → 1件にまとめる（優先度の高い方を代表に）"""
    main = alerts[(alerts.category == "業績") & alerts.rule.isin(MAIN_RULES) & alerts.priority.isin(["P1", "P2"])]
    if main.empty:
        return main.assign(covered_by="", merged_messages=[[]] * 0)
    keys = set(zip(main.kpi_id, main.scope_key))
    by_id = {r.alert_id: r for r in main.itertuples()}
    rule_order = {"target_miss": 0, "abnormal": 1}
    main = main.assign(_p=main.priority, _r=main.rule.map(rule_order)).sort_values(["_p", "_r"])
    reps = main.groupby(["kpi_id", "scope_key"], sort=False).head(1)
    delegated = set()   # 集中している子にアクションを委ねた上位
    child_of_focus = set()  # 集中先として選ばれた子
    for r in reps.itertuples():
        # 今月起きた変化（前月差が乖離の半分以上）で、かつ特定の下位内訳に集中している場合だけ下位に委ねる
        dev = r.value - r.reference if not pd.isna(r.reference) else np.nan
        if pd.isna(dev) or pd.isna(r.diff) or abs(r.diff) < 0.5 * abs(dev):
            continue
        filt = {} if r.scope_key == "ALL" else dict(x.split("=", 1) for x in r.scope_key.split("|"))
        for c in (r.contributors or []):
            if not c.get("concentrated"):
                continue
            child = "|".join(f"{d}={v}" for d, v in sorted({**filt, c["dim"]: c["segment"]}.items(),
                                                           key=lambda kv: ["entity_code", "product_code"].index(kv[0])
                                                           if kv[0] in ("entity_code", "product_code") else 9))
            if (r.kpi_id, child) in keys:
                delegated.add((r.kpi_id, r.scope_key))
                child_of_focus.add((r.kpi_id, child))
                break
    primary_kpis = set(main[main.scope_type.isin(PRIMARY_SCOPES)].kpi_id)
    rows = []
    for (kid, key), g in main.groupby(["kpi_id", "scope_key"], sort=False):
        rep = g.iloc[0]
        parents = [by_id[x] for x in str(rep.related_alert_id or "").split(",") if x in by_id]
        if (kid, key) in delegated:
            continue
        if parents and (kid, key) not in child_of_focus:
            continue  # 上位のアクションに含める
        if rep.scope_type not in PRIMARY_SCOPES and kid in primary_kpis:
            continue  # 同じKPIで法人・商品側のアクションがある → 組織・業種・規模の切り口は関連内訳として示す
        rows.append(rep.to_dict() | {"merged_messages": list(g.message)})
    return pd.DataFrame(rows).drop(columns=["_p", "_r"], errors="ignore")


def build_actions(alerts: pd.DataFrame, dq_frame: pd.DataFrame, cfg, masters: dict, due_date: pd.Timestamp,
                  dq_due: pd.Timestamp | None = None) -> list[dict]:
    tpl = cfg.templates
    owners = masters["owners"]
    actions = []
    sel = select_for_action(alerts) if len(alerts) else alerts
    for a in sel.itertuples():
        kpi = cfg.kpi(a.kpi_id)
        t = tpl.get("kpis", {}).get(a.kpi_id) or tpl["default"]
        owner, manager = resolve_owner(owners, a.kpi_id, a.entity_code, a.product_code, kpi.get("owner_default", ""))
        facts = list(a.merged_messages)
        if not pd.isna(a.yoy_value):
            facts.append(f"前年同月 {_fmt(kpi, a.yoy_value)}。")
        if kpi["type"] == "ratio":
            lab = kpi.get("denominator_label", "分母")
            den = f"{a.den / 1e6:,.1f}百万円" if "金額" in lab else f"{a.den:,.0f}"
            facts.append(f"母数（{lab}）{den}。")
        hyps = []
        if a.cause_type in tpl.get("signals", {}):
            hyps.append({"text": tpl["signals"][a.cause_type], "status": "データで示唆", "basis": "前月差の分解結果"})
        hyps += [{"text": h, "status": "未検証", "basis": "テンプレート（一般的な要因）"} for h in t["hypotheses"]]
        checks = list(t["checks"])
        if a.contributors:
            c0 = a.contributors[0]
            checks.insert(0, f"変化への寄与が最も大きい「{c0['label']}」の明細を確認する")
        verb = VERB
        title = f"【{verb}】{a.scope_label}の{kpi['name']}（{a.rule_label}）の原因を{verb}する"
        actions.append({
            "action_id": "X" + a.alert_id[1:], "alert_id": a.alert_id, "month": a.month, "kpi_id": a.kpi_id,
            "kpi_name": kpi["name"], "scope_key": a.scope_key, "scope_label": a.scope_label, "priority": a.priority,
            "title": title, "verb": verb, "facts": facts, "signals": list(a.signals), "hypotheses": hyps,
            "checks": checks, "impact": _impact(kpi, a), "owner": owner, "manager": manager,
            "due_date": due_date.strftime("%Y-%m-%d"), "status": "下書き",
            "confidence": "原因は未検証（仮説の確認が必要）",
            "related_scopes": _related_labels(alerts, a),
        })
    for q in dq_frame[dq_frame.priority == "P1"].itertuples():
        actions.append({
            "action_id": "X" + q.issue_id[1:], "alert_id": q.issue_id, "month": q.month, "kpi_id": "", "kpi_name": "データ品質",
            "scope_key": "", "scope_label": " ".join(x for x in (q.source, q.entity, q.month) if x), "priority": "P1",
            "title": f"【確認】{q.rule_label}: {q.source} {q.entity} {q.month}".strip(), "verb": "確認",
            "facts": [q.message], "signals": [], "hypotheses": [], "checks": ["出力元システムの担当者に状況を確認し、再出力・再送を依頼する"],
            "impact": {"value": None, "unit": "", "text": "該当データを含むKPIは暫定値。業績アラートは保留中"},
            "owner": "データ管理者（仮）", "manager": "経営企画部長（仮）",
            "due_date": (dq_due or due_date).strftime("%Y-%m-%d"), "status": "下書き", "confidence": "事実（取込時に検知）",
        })
    return actions


def _related_labels(alerts: pd.DataFrame, a) -> list[str]:
    """同じKPIで同時にアラートが出ている他の内訳（このアクションに含めて確認する）。"""
    same = alerts[(alerts.kpi_id == a.kpi_id) & (alerts.scope_key != a.scope_key) & alerts.rule.isin(MAIN_RULES)
                  & alerts.priority.isin(["P1", "P2"])]
    return sorted(set(same.scope_label))[:12]
