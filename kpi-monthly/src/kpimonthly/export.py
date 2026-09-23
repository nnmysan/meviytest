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
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    bundle = dashboard_bundle(frames, cfg, manifest)
    from .dashboard import _clean
    (out_dir / "dashboard_data.json").write_text(json.dumps(_clean(bundle), ensure_ascii=False, default=_jsonable), encoding="utf-8")
    if write_dashboard:
        from .dashboard import render
        render(bundle, out_dir / "dashboard.html")


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
                                            "reviewer", "value", "reference", "data_status")})
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
        "targets": _records(m["targets"][m["targets"].month.isin(months)], ["kpi_id", "month", "scope_type", "scope_key", "target"]),
        "series_cols": scols,
        "series": _records(series, scols),
        "alerts": alerts,
        "actions": frames["actions"],
        "dq": frames["dq"].to_dict("records"),
        "restatements": frames["restatements"].to_dict("records"),
        "file_log": frames["file_log"][["source", "entity", "month", "file", "status", "rows", "encoding", "batch"]].to_dict("records")
        if len(frames["file_log"]) else [],
        "comments": frames["comments"].to_dict("records"),
        "quarantine_summary": frames["quarantine"].groupby(["source", "reason"]).size().reset_index(name="rows").to_dict("records")
        if len(frames["quarantine"]) else [],
    }
