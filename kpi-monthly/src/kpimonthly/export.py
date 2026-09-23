"""出力：BI（Power BI / DOMO）が読むテーブルと、確認用HTMLダッシュボード用のデータ。

marts/ 以下は Power BI からフォルダ接続で読み込める形（UTF-8 BOM付きCSV と Parquet）。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

MART_COLUMNS = ["kpi_id", "kpi_name", "scope_type", "scope_key", "scope_label", "entity_code", "product_code",
                "division_code", "team_code", "section_code", "industry", "size_class", "customer_id", "month",
                "num", "den", "value", "prev_value", "diff", "pct", "prev_status", "yoy_value", "yoy_diff", "yoy_pct",
                "ma3", "ytd_value", "target", "target_gap", "achievement", "consecutive_miss", "baseline", "residual",
                "z", "data_status", "small_sample", "value_actual_fx", "fx_effect", "note"]


def _csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")


def write_outputs(frames: dict, cfg, manifest: dict, out_dir: Path, write_dashboard: bool = True) -> None:
    marts = out_dir / "marts"
    marts.mkdir(parents=True, exist_ok=True)
    an = frames["analysis"]
    an[MART_COLUMNS].to_parquet(marts / "kpi_monthly.parquet", index=False)
    _csv(an[MART_COLUMNS], marts / "kpi_monthly.csv")
    frames["fine"].to_parquet(marts / "kpi_fact_fine.parquet", index=False)
    al = frames["alerts"].copy()
    if len(al):
        al["signals"] = al["signals"].map(lambda x: " / ".join(x) if isinstance(x, list) else "")
        al["contributors"] = al["contributors"].map(lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, list) else "")
    _csv(al, marts / "alerts.csv")
    acts = frames["actions"]
    (marts / "actions.json").write_text(json.dumps(acts, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    flat = [{**{k: v for k, v in a.items() if not isinstance(v, (list, dict))},
             "facts": " / ".join(a["facts"]), "hypotheses": " / ".join(f"[{h['status']}] {h['text']}" for h in a["hypotheses"]),
             "checks": " / ".join(a["checks"]), "impact": a["impact"]["text"]} for a in acts]
    _csv(pd.DataFrame(flat), marts / "actions.csv")
    _csv(frames["dq"], marts / "dq_issues.csv")
    _csv(frames["quarantine"], marts / "quarantine.csv")
    _csv(frames["file_log"], marts / "file_log.csv")
    _csv(frames["restatements"], marts / "restatements.csv")
    m = frames["masters"]
    for name in ("entities", "products", "org", "customers"):
        _csv(m[name], marts / f"dim_{name}.csv")
    kdefs = pd.DataFrame([{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
                           for k, v in kpi.items()} for kpi in cfg.kpis])
    _csv(kdefs, marts / "kpi_definitions.csv")
    # 日別・時間別・当月進捗・サプライヤ
    for name in ("daily", "hourly"):
        df = frames.get(name)
        if df is not None and len(df):
            df.to_parquet(marts / f"kpi_{name}.parquet", index=False)
            _csv(df, marts / f"kpi_{name}.csv")
    if frames.get("pace") is not None and len(frames["pace"]):
        _csv(frames["pace"], marts / "pace.csv")
    sup = frames.get("supplier") or {}
    if sup.get("enabled"):
        frames["supplier_cubes"] = supplier_cubes(frames["_con"], manifest["data_end"], sup)
        _csv(pd.concat(sup["bases"].values(), ignore_index=True), marts / "supplier_load.csv")
        _csv(pd.DataFrame(sup["notifications"], columns=NOTIFY_COLS), marts / "notifications.csv")
        _csv(m["suppliers"], marts / "dim_suppliers.csv")
        _csv(m["supplier_capacity"], marts / "dim_supplier_capacity.csv")
        for k, df in frames["supplier_cubes"].items():
            df.to_parquet(marts / f"supplier_{k}.parquet", index=False)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_excel(frames, cfg, manifest, marts / "kpi_report.xlsx")
    bundle = dashboard_bundle(frames, cfg, manifest)
    from .dashboard import _clean
    (out_dir / "dashboard_data.json").write_text(json.dumps(_clean(bundle), ensure_ascii=False, default=_jsonable), encoding="utf-8")
    if write_dashboard:
        from .dashboard import render
        render(bundle, out_dir / "dashboard.html")


NOTIFY_COLS = ["notify_id", "created_at", "channel", "to", "cc", "priority", "reason", "title", "message", "alert_id",
               "scope_key", "status"]


def supplier_cubes(con, data_end: str, sup: dict) -> dict[str, pd.DataFrame]:
    """サプライヤ画面用の集計（日別：直近35日＋今後の予定、週別：27週、月別：全期間）。"""
    from .supplier import COMBO, demand, demand_monthly, quality_delivery
    end = pd.Timestamp(data_end)
    fwd_end = pd.Timestamp(sup["forward_end"])
    w_start = (end - pd.Timedelta(days=7 * 27)).normalize()
    w_start = w_start - pd.Timedelta(days=w_start.weekday())
    d_start = end - pd.Timedelta(days=34)
    dem = demand(con, str(w_start.date()), str(max(fwd_end, end + pd.Timedelta(days=30)).date()))
    qd = quality_delivery(con, str(w_start.date()), data_end, "day")
    qd["day"] = pd.to_datetime(qd.period)
    out = {
        "supply_daily": dem[dem.day >= d_start].assign(day=lambda d: d.day.dt.strftime("%Y-%m-%d")),
        "qd_daily": qd[qd.day >= d_start].assign(day=lambda d: d.day.dt.strftime("%Y-%m-%d")).drop(columns="period"),
    }
    past = dem[dem.day <= end]
    wk = lambda d: (d - pd.to_timedelta(d.dt.weekday, unit="D")).dt.strftime("%Y-%m-%d")
    out["supply_weekly"] = past.assign(week=wk(past.day)).groupby(COMBO + ["week"])[["lines", "qty", "purchase"]].sum().reset_index()
    out["qd_weekly"] = qd.assign(week=wk(qd.day)).groupby(COMBO + ["week"])[["due_lines", "on_time", "shipped", "defects"]].sum().reset_index()
    out["supply_monthly"] = demand_monthly(con, data_end)
    out["qd_monthly"] = quality_delivery(con, "1900-01-01", data_end, "month").rename(columns={"period": "month"})
    return out


def write_excel(frames: dict, cfg, manifest: dict, path: Path) -> None:
    """集計結果を1つのExcelブックに（シートごとに1表）。明細は行数が多いため含めない。"""
    an = frames["analysis"]
    sheets = {
        "概要": pd.DataFrame([{"項目": k, "値": json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}
                            for k, v in manifest.items() if k not in ("landing",)]),
        "KPI月次": an[an.scope_type.isin(["total", "entity", "product", "entity_product"])][MART_COLUMNS],
        "当月進捗": frames.get("pace"),
        "アラート": frames["alerts"].assign(
            signals=lambda d: d.signals.map(lambda x: " / ".join(x) if isinstance(x, list) else ""),
            contributors=lambda d: d.contributors.map(lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, list) else ""))
        if len(frames["alerts"]) else frames["alerts"],
        "アクション": pd.DataFrame([{**{k: v for k, v in a.items() if not isinstance(v, (list, dict))},
                                  "facts": " / ".join(a["facts"]), "checks": " / ".join(a["checks"]),
                                  "impact": a["impact"]["text"]} for a in frames["actions"]]),
        "データ品質": frames["dq"],
    }
    sup = frames.get("supplier") or {}
    if sup.get("enabled"):
        sheets["サプライヤ充足率"] = pd.concat(sup["bases"].values(), ignore_index=True)
        sheets["通知予定"] = pd.DataFrame(sup["notifications"], columns=NOTIFY_COLS)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            if df is None or not len(df):
                df = pd.DataFrame({"メッセージ": ["該当なし"]})
            df.to_excel(xw, sheet_name=name, index=False)


def _jsonable(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return o.strftime("%Y-%m-%d")
    return str(o)


def _records(df: pd.DataFrame, cols: list[str], digits: int = 6) -> list[list]:
    out = []
    for row in df[cols].itertuples(index=False):
        rec = []
        for v in row:
            if isinstance(v, (float, np.floating)):
                rec.append(None if np.isnan(v) else round(float(v), digits))
            elif isinstance(v, (np.bool_, bool)):
                rec.append(bool(v))
            elif v is None or (isinstance(v, float) and np.isnan(v)):
                rec.append(None)
            else:
                rec.append(v)
        out.append(rec)
    return out


def _idx(values) -> tuple[list, dict]:
    vals = sorted({v for v in values if v is not None and not (isinstance(v, float) and np.isnan(v))})
    return vals, {v: i for i, v in enumerate(vals)}


def _compact(df: pd.DataFrame, cols: list[str], maps: dict, digits: int = 2) -> list[list]:
    arrs = []
    for c in cols:
        v = df[c]
        if c in maps:
            arrs.append(v.map(maps[c]).fillna(-1).astype(int).tolist())
        else:
            arrs.append([None if pd.isna(x) else (round(float(x), digits) if float(x) != int(x) else int(x)) for x in v])
    return [list(r) for r in zip(*arrs)] if arrs else []


def daily_bundle(frames: dict, cfg, manifest: dict) -> dict:
    """日別（直近27週）・時間別（直近35日）のKPIと、サプライヤ画面用のデータ。値は番号で持たせてサイズを抑える。"""
    out = {}
    daily, hourly = frames.get("daily"), frames.get("hourly")
    kids = [k["id"] for k in cfg.kpis]
    kmap = {k: i for i, k in enumerate(kids)}
    if daily is not None and len(daily):
        ents, emap = _idx(daily.entity_code)
        prds, pmap = _idx(daily.product_code)
        secs, smap = _idx(daily.section_code)
        days = [str(d.date()) for d in pd.date_range(daily.day.min(), manifest["data_end"])]
        dmap = {d: i for i, d in enumerate(days)}
        maps = {"kpi_id": kmap, "day": dmap, "entity_code": emap, "product_code": pmap, "section_code": smap}
        out["daily"] = {"kpis": kids, "days": days, "ents": ents, "prds": prds, "secs": secs,
                        "rows": _compact(daily, ["kpi_id", "day", "entity_code", "product_code", "section_code", "num", "den",
                                                 "actual_fx"], maps)}
        if hourly is not None and len(hourly):
            out["hourly"] = {"rows": _compact(hourly, ["kpi_id", "day", "hour", "entity_code", "product_code", "section_code",
                                                       "num", "den"], maps),
                             "kpis": sorted(hourly.kpi_id.unique().tolist())}
    pace = frames.get("pace")
    out["pace"] = pace.to_dict("records") if pace is not None and len(pace) else []
    sup = frames.get("supplier") or {}
    if sup.get("enabled"):
        from .supplier import COMBO
        m = frames["masters"]
        cap = m["supplier_capacity"].reset_index(drop=True)
        cidx = {tuple(r): i for i, r in enumerate(cap[COMBO].itertuples(index=False, name=None))}
        cubes = frames["supplier_cubes"]

        def enc(df, tcol, tvals, cols):
            tmap = {t: i for i, t in enumerate(tvals)}
            ci = [cidx.get(t, -1) for t in df[COMBO].itertuples(index=False, name=None)]
            d = df.assign(_c=ci, _t=df[tcol].map(tmap))
            d = d[(d._c >= 0) & d._t.notna()]
            return _compact(d, ["_c", "_t"] + cols, {}, 0)

        sd, sw, sm = cubes["supply_daily"], cubes["supply_weekly"], cubes["supply_monthly"]
        days = sorted(set(sd.day) | set(cubes["qd_daily"].day))
        weeks = sorted(set(sw.week) | set(cubes["qd_weekly"].week))
        months = sorted(set(sm.month) | set(cubes["qd_monthly"].month))
        sp = m["suppliers"]
        out["sup"] = {
            "combos": cap[COMBO + ["cap_count_day", "cap_qty_day"]].values.tolist(),
            "suppliers": {r.supplier_code: {"name": r.supplier_name, "entity": r.entity_code, "owner": r.owner,
                                            "manager": r.manager} for r in sp.itertuples()},
            "days": days, "weeks": weeks, "months": months,
            "daily": enc(sd, "day", days, ["lines", "qty", "purchase", "planned_lines"]),
            "weekly": enc(sw, "week", weeks, ["lines", "qty", "purchase"]),
            "monthly": enc(sm, "month", months, ["lines", "qty", "purchase"]),
            "qd_daily": enc(cubes["qd_daily"], "day", days, ["due_lines", "on_time", "shipped", "defects"]),
            "qd_weekly": enc(cubes["qd_weekly"], "week", weeks, ["due_lines", "on_time", "shipped", "defects"]),
            "qd_monthly": enc(cubes["qd_monthly"], "month", months, ["due_lines", "on_time", "shipped", "defects"]),
            "notifications": sup["notifications"], "forward_end": sup["forward_end"], "threshold": sup["threshold"],
        }
    return out


def dashboard_bundle(frames: dict, cfg, manifest: dict) -> dict:
    """ダッシュボード用のデータ。直近25か月分（前年同月比が13か月描けるだけ）を持たせる。"""
    an, fine, m = frames["analysis"], frames["fine"], frames["masters"]
    tm = manifest["target_month"]
    months = [str(pd.Period(tm, "M") - i) for i in range(24, -1, -1)]
    # 細粒度（顧客は除き、法人×商品×課×業種×規模）… ブラウザ側で任意の絞り込み・集計に使う
    f = fine[fine.month.isin(months)]
    grain = ["kpi_id", "month", "entity_code", "product_code", "section_code", "industry", "size_class"]
    agg = f.groupby(grain, dropna=False)[["num", "den", "actual_fx"]].sum(min_count=1).reset_index()
    # 顧客別（直近2か月のみ・上位の寄与分析用）
    fc = fine[fine.month.isin(months[-2:])]
    cust = fc.groupby(["kpi_id", "month", "entity_code", "product_code", "section_code", "customer_id"], dropna=False)[["num", "den"]].sum().reset_index()
    alert_scopes = [s["type"] for s in cfg.analysis["scopes"] if s.get("alerts")]
    series = an[an.scope_type.isin(alert_scopes) & an.month.isin(months)]
    scols = ["kpi_id", "scope_type", "scope_key", "month", "value", "target", "baseline", "z", "data_status",
             "prev_status", "small_sample", "note"]
    al = frames["alerts"]
    alerts = []
    for a in al.to_dict("records"):
        alerts.append({k: a.get(k) for k in ("alert_id", "month", "kpi_id", "kpi_name", "rule", "rule_label", "priority",
                                            "scope_type", "scope_key", "scope_label", "message", "label", "related_alert_id",
                                            "cause_type", "signals", "contributors", "review_status", "review_comment",
                                            "reviewer", "value", "reference", "data_status", "category", "supplier_code",
                                            "level", "basis", "load_count", "load_qty")})
    org = m["org"]
    return {
        "manifest": manifest,
        "months": months,
        "kpis": [{k: kpi.get(k) for k in ("id", "name", "description", "type", "display", "direction", "status",
                                         "version", "maturity_days", "owner_default", "open_questions", "sources")}
                 | {"thresholds": cfg.kpi_thresholds(kpi["id"])} for kpi in cfg.kpis],
        "thresholds_status": cfg.thresholds.get("status"),
        "dims": {
            "entity_code": dict(zip(m["entities"].entity_code, m["entities"].entity_name)),
            "product_code": dict(zip(m["products"].product_code, m["products"].product_name)),
            "section_code": dict(zip(org.section_code, org.section_name)),
            "team_code": dict(zip(org.team_code, org.team_name)),
            "division_code": dict(zip(org.division_code, org.division_name)),
            "industry": {v: v for v in sorted(m["customers"].industry.unique())},
            "size_class": {v: v for v in ["大", "中", "小"]},
            "customer_id": dict(zip(m["customers"].customer_id, m["customers"].customer_name)),
        },
        "org": org[["section_code", "team_code", "division_code"]].to_dict("records"),
        "fine_cols": grain + ["num", "den", "actual_fx"],
        "fine": _records(agg, grain + ["num", "den", "actual_fx"], 4),
        "cust_cols": ["kpi_id", "month", "entity_code", "product_code", "section_code", "customer_id", "num", "den"],
        "cust": _records(cust, ["kpi_id", "month", "entity_code", "product_code", "section_code", "customer_id", "num", "den"], 4),
        "targets": _records(m["targets"][m["targets"].month.isin(months + [manifest.get("data_end", tm)[:7]])],
                            ["kpi_id", "month", "scope_type", "scope_key", "target"]),
        "series_cols": scols,
        "series": _records(series, scols),
        "alerts": alerts,
        "actions": frames["actions"],
        "dq": frames["dq"].to_dict("records"),
        "restatements": frames["restatements"].to_dict("records"),
        "file_log": frames["file_log"][["source", "entity", "month", "file", "status", "rows", "encoding", "batch"]].to_dict("records")
        if len(frames["file_log"]) else [],
        "comments": frames["comments"].to_dict("records"),
        **daily_bundle(frames, cfg, manifest),
        "quarantine_summary": frames["quarantine"].groupby(["source", "reason"]).size().reset_index(name="rows").to_dict("records")
        if len(frames["quarantine"]) else [],
    }
