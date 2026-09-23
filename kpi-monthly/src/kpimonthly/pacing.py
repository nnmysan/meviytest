"""当月の進捗と着地見込み（毎朝の更新で、月末を待たずに異変に気づくため）。

合計KPI : 着地見込み = 当月累計 ÷ 経過営業日 × 当月の営業日（単純な日割り。曜日・月末偏重は考慮しない）
率KPI   : 当月累計の率（分子・分母の累計から計算）
判定    : 目標に対する見込みが一定以下で、かつ経過日数・件数から見て偶然の範囲を超えるときだけ（誤検知抑制）
成熟待ちのKPI（転換率・品質不良率）は当月の途中では判定しない。
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

SCOPES = [("total", []), ("entity", ["entity_code"]), ("product", ["product_code"])]


def _key(dims, row) -> str:
    return "|".join(f"{d}={row[d]}" for d in dims) or "ALL"


def compute_pacing(daily: pd.DataFrame, cfg, masters: dict, data_end: str, cal, labels: dict) -> tuple[pd.DataFrame, list[dict]]:
    cm = pd.Period(data_end[:7], "M")
    start = cm.start_time.normalize()
    end = pd.Timestamp(data_end)
    # 営業日は平日で数える（法人ごとに休日が異なるため。法人別カレンダーは今後の課題）
    wd = lambda a, b: int(np.busday_count(a.date(), (b + pd.Timedelta(days=1)).date()))
    bdays_total = wd(start, cm.end_time.normalize())
    bdays_elapsed = wd(start, end)
    pc = cfg.g.get("pace") or {}
    d = daily[(daily.day >= str(start.date())) & (daily.day <= data_end)]
    tg = masters["targets"]
    tg = tg[tg.month == str(cm)].set_index(["kpi_id", "scope_key"]).target
    rows = []
    cnt = {}  # 範囲ごとの当月受注件数（ばらつきの目安）
    oc = d[d.kpi_id == "order_count"]
    for st, dims in SCOPES:
        if not dims:
            cnt["ALL"] = oc.num.sum()
        else:
            for k, v in oc.groupby(dims[0]).num.sum().items():
                cnt[f"{dims[0]}={k}"] = v
    for kpi in cfg.kpis:
        sub = d[d.kpi_id == kpi["id"]]
        for st, dims in SCOPES:
            grp = sub.groupby(dims)[["num", "den"]].sum().reset_index() if dims else \
                pd.DataFrame([{"num": sub.num.sum(), "den": sub.den.sum()}])
            for r in grp.to_dict("records"):
                key = _key(dims, r)
                target = tg.get((kpi["id"], key), np.nan)
                if kpi["type"] == "sum":
                    mtd = r["num"]
                    fcst = mtd / bdays_elapsed * bdays_total if bdays_elapsed else np.nan
                    pace_target = target * bdays_elapsed / bdays_total if bdays_total else np.nan
                    ach = fcst / target if target else np.nan
                else:
                    mtd = r["num"] / r["den"] if r["den"] else np.nan
                    fcst, pace_target, ach = mtd, target, np.nan
                rows.append(dict(kpi_id=kpi["id"], kpi_name=kpi["name"], month=str(cm), scope_type=st, scope_key=key,
                                 scope_label=_label(key, labels), mtd=mtd, den=r["den"], forecast=fcst, target=target,
                                 pace_target=pace_target, achievement_forecast=ach, bdays_elapsed=bdays_elapsed,
                                 bdays_total=bdays_total, orders_mtd=cnt.get(key, np.nan),
                                 mature=not kpi.get("maturity_days")))
    pace = pd.DataFrame(rows)
    alerts = []
    if not pc or bdays_elapsed < pc.get("min_business_days", 5):
        return pace, alerts
    sig = pc.get("significance", 2.0)
    for r in pace.itertuples():
        if not r.mature or pd.isna(r.target):
            continue
        kpi = cfg.kpi(r.kpi_id)
        sign = 1.0 if kpi["direction"] == "higher_is_better" else -1.0
        th = (cfg.kpi_thresholds(r.kpi_id) or {}).get("target_miss")
        if kpi["type"] == "sum":
            if pd.isna(r.achievement_forecast) or not r.orders_mtd:
                continue
            shortfall = 1 - r.achievement_forecast
            se = 1.5 / np.sqrt(r.orders_mtd)  # 件数のばらつき＋金額のばらつき（概算）
            level = "P1" if r.achievement_forecast < pc.get("p1", 0.8) else "P2" if r.achievement_forecast < pc.get("p2", 0.9) else None
            ok = shortfall >= sig * se
            fmt = (lambda v: f"{v / 1e6:,.1f}百万円") if kpi["display"] == "yen" else (lambda v: f"{v:,.0f}件")
            msg = (f"{r.scope_label}の{kpi['name']}は当月累計 {fmt(r.mtd)}（{r.bdays_elapsed}/{r.bdays_total}営業日）。"
                   f"このままの日割りでは月末 {fmt(r.forecast)} の見込みで、目標 {fmt(r.target)} の {r.achievement_forecast:.0%}。")
        else:
            if not th or pd.isna(r.mtd) or not r.den:
                continue
            gap = (r.mtd - r.target) * sign * 100
            se = np.sqrt(max(r.target * (1 - r.target), 1e-9) / r.den) * 100 if kpi.get("binomial") else 0
            level = "P1" if gap <= -th["p1"] else "P2" if gap <= -th["p2"] else None
            ok = abs(gap) >= sig * se
            msg = f"{r.scope_label}の{kpi['name']}は当月累計 {r.mtd * 100:.1f}%（目標 {r.target * 100:.1f}%）。"
        if level and ok:
            aid = "P" + hashlib.sha1(f"{r.month}|{r.kpi_id}|forecast|{r.scope_key}".encode()).hexdigest()[:10]
            alerts.append(dict(alert_id=aid, month=r.month, kpi_id=r.kpi_id, kpi_name=kpi["name"], rule="forecast",
                               rule_label="当月の着地見込み未達", priority=level, category="業績", scope_type=r.scope_type,
                               scope_key=r.scope_key, scope_label=r.scope_label, message=msg, label="当月途中",
                               related_alert_id="", cause_type="", signals=[], contributors=[], value=r.forecast,
                               reference=r.target, data_status="当月途中"))
    return pace, alerts


def _label(key: str, labels: dict) -> str:
    if key == "ALL":
        return "全社"
    d, v = key.split("=", 1)
    return labels.get(d, {}).get(v, v)
