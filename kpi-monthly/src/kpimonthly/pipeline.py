"""月次パイプライン：取込 → DQ → 集計 → 分析 → アラート → アクション → 出力。

    PYTHONPATH=src python -m kpimonthly.pipeline --profile dummy \
        --landing data/dummy/S0/landing/batch_01 --target-month 2026-08 --as-of 2026-09-08 \
        --out out/S0 --state out/S0/state
"""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .actions import build_actions
from .alerts import evaluate
from .analysis import label_maps
from .config import Config, load_config
from .core import add_dims, build_db, compute_fine, largest_orders
from .dq import DQ, BusinessCalendar, next_month
from .ingest import load_masters, ingest

RESTATE_SCOPES = ["total", "entity", "product", "entity_product"]


@dataclass
class RunResult:
    status: str
    out_dir: Path
    run_id: str
    frames: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)


def _git_rev(root: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- DQ（ファイル単位）

def check_arrivals(file_log: pd.DataFrame, masters: dict, cfg: Config, target_month: str, as_of: str,
                   cal: BusinessCalendar, registry: pd.DataFrame, dq: DQ) -> pd.DataFrame:
    arr = cfg.sources.get("arrival", {})
    due_bd = arr.get("due_business_day", 5)
    lookback = arr.get("lookback_months", 13)
    months = [str(pd.Period(target_month, "M") - i) for i in range(lookback - 1, -1, -1)]
    loaded = file_log[file_log.status.isin(["loaded", "skipped_duplicate"])] if len(file_log) else file_log
    have = set(zip(loaded.source, loaded.entity, loaded.month)) if len(loaded) else set()
    as_of_ts = pd.Timestamp(as_of)
    for m in months:
        due = cal.nth_business_day(next_month(m), due_bd)
        for s in cfg.sources["sources"]:
            for e in masters["entities"].entity_code:
                if (s, e, m) not in have:
                    overdue = as_of_ts > due
                    dq.add("missing_file", "P1" if overdue else "P2",
                           f"{s} の {e} {m} 分が届いていません（期限 {due:%m/%d}{'を超過' if overdue else '前'}）。該当KPIは暫定扱い・業績判定を保留します",
                           source=s, entity=e, month=m)
    # 期限後の到着（前回までに見ていないファイルで、期限を過ぎてから初めて届いたもの）
    if len(file_log):
        seen = set(registry.sha256) if len(registry) else set()
        new = file_log[(file_log.status == "loaded") & ~file_log.sha256.isin(seen)]
        for r in new.itertuples():
            due = cal.nth_business_day(next_month(r.month), due_bd)
            if len(registry) and as_of_ts > due and r.month <= target_month:
                dq.add("late_arrival", "INFO", f"期限（{due:%m/%d}）後に到着したファイル。前回の集計値から更新されます",
                       source=r.source, entity=r.entity, month=r.month, file=r.file)
    return file_log


def check_row_counts(data: dict, cfg: Config, target_month: str, dq: DQ) -> None:
    rc = cfg.g.get("row_count", {})
    lb = rc.get("lookback", 6)
    for s, spec in cfg.sources["sources"].items():
        if not spec.get("row_count_check") or data[s].empty:
            continue
        cnt = data[s].groupby(["_entity_file", "_month_file"]).size()
        for e in cnt.index.get_level_values(0).unique():
            ser = cnt.loc[e]
            if target_month not in ser.index:
                continue
            hist = ser[(ser.index < target_month)].tail(lb)
            if len(hist) < 3:
                continue
            ratio = ser[target_month] / float(np.median(hist))
            if ratio < rc.get("low", 0.5) or ratio > rc.get("high", 2.0):
                dq.add("row_count_anomaly", "P2", f"当月の行数 {ser[target_month]:,} は直近{len(hist)}か月の中央値 {np.median(hist):,.0f} の {ratio:.0%}。"
                       "抽出条件の変更・一部欠落の可能性を確認してください", source=s, entity=e, month=target_month,
                       count=int(ser[target_month]))


# ---------------------------------------------------------------- スナップショットと過去値修正

def compare_snapshot(an: pd.DataFrame, prev: pd.DataFrame | None, cfg: Config, target_month: str, dq: DQ,
                     prev_run: str) -> pd.DataFrame:
    cols = ["kpi_id", "kpi_name", "scope_type", "scope_key", "scope_label", "month", "prev_run_value", "value",
            "change", "rel_change", "kind", "prev_run_id"]
    if prev is None or prev.empty:
        return pd.DataFrame(columns=cols)
    rs = cfg.g.get("restatement", {})
    cur = an[an.scope_type.isin(RESTATE_SCOPES) & (an.month <= target_month)]
    m = cur.merge(prev[["kpi_id", "scope_key", "month", "value", "data_status"]].rename(
        columns={"value": "prev_run_value", "data_status": "prev_status_run"}), on=["kpi_id", "scope_key", "month"], how="left")
    pct = m.kpi_id.map(lambda k: cfg.kpi(k)["display"] == "percent")
    m["change"] = m.value - m.prev_run_value.fillna(0)
    with np.errstate(divide="ignore", invalid="ignore"):
        m["rel_change"] = np.where(m.prev_run_value.abs() > 0, m.value / m.prev_run_value - 1, np.nan)
    was_provisional = m.prev_status_run.isin(["未着", "一部未着"]) | m.prev_run_value.isna()
    changed = np.where(pct, (m.change.abs() * 100) >= rs.get("ratio_pt", 0.1),
                       (m.rel_change.abs() >= rs.get("sum_rel", 0.005)) | (m.prev_run_value.isna() & m.value.notna()))
    m = m[changed & m.value.notna()].copy()
    m["kind"] = np.where(was_provisional.loc[m.index], "暫定値の確定", "過去値修正")
    m["prev_run_id"] = prev_run
    for kid, g in m[m.kind == "過去値修正"].groupby("kpi_id"):
        k = cfg.kpi(kid)
        pct_k = k["display"] == "percent"
        big = g[(g.change.abs() * 100 >= rs.get("p2_ratio_pt", 1.0)) if pct_k else (g.rel_change.abs() >= rs.get("p2_sum_rel", 0.05))]
        worst = g.iloc[(g.change.abs() if pct_k else g.rel_change.abs()).argmax()]
        chg = f"{worst.change * 100:+.2f}pt" if pct_k else f"{worst.rel_change:+.1%}"
        dq.add("restatement", "P2" if len(big) else "INFO",
               f"{k['name']} の過去値が前回の集計（{prev_run}）から {len(g)} 件修正されました。最大: {worst.scope_label} {worst.month} {chg}。"
               "前月比など過去月との比較も再計算しています", source=kid, month=str(worst.month), count=int(len(g)))
    return m[cols].reset_index(drop=True)


# ---------------------------------------------------------------- 人による確認結果の取り込み

def merge_review(alerts: pd.DataFrame, actions: list[dict], review_dir: Path | None, target_month: str) -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    comments = pd.DataFrame(columns=["month", "kpi_id", "scope_key", "author", "fact", "cause", "action"])
    alerts = alerts.copy()
    alerts["review_status"] = "未確認"
    alerts["review_comment"] = ""
    alerts["reviewer"] = ""
    if review_dir and Path(review_dir).exists():
        rp = Path(review_dir) / "review.csv"
        if rp.exists():
            rv = pd.read_csv(rp, dtype=str, keep_default_na=False)
            rmap = {r.target_id: r for r in rv.itertuples()}
            for i, a in alerts.iterrows():
                r = rmap.get(a.alert_id)
                if r is not None:
                    alerts.loc[i, ["review_status", "review_comment", "reviewer"]] = [r.status, r.comment, r.reviewer]
            for act in actions:
                r = rmap.get(act["action_id"])
                if r is not None:
                    act.update(status=r.status, review_comment=r.comment, reviewer=r.reviewer)
                    if getattr(r, "owner", ""):
                        act["owner"] = r.owner
                    if getattr(r, "due_date", ""):
                        act["due_date"] = r.due_date
        cp = Path(review_dir) / "comments.csv"
        if cp.exists():
            comments = pd.read_csv(cp, dtype=str, keep_default_na=False)
            comments = comments[comments.month == target_month]
    return alerts, actions, comments


# ---------------------------------------------------------------- メイン

def run(cfg: Config, landing_dirs: list[Path], target_month: str, as_of: str, out_dir: Path,
        state_dir: Path | None = None, review_dir: Path | None = None, finalize: bool = False,
        write_dashboard: bool = True) -> RunResult:
    out_dir = Path(out_dir)
    state_dir = Path(state_dir) if state_dir else out_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_log = state_dir / "runs.json"
    history = json.loads(runs_log.read_text(encoding="utf-8")) if runs_log.exists() else []
    seq = sum(1 for h in history if h["target_month"] == target_month) + 1
    run_id = f"{target_month}_v{seq}"
    cal = BusinessCalendar(cfg.holidays)
    dq = DQ()
    landing_dirs = [Path(d) for d in landing_dirs]

    masters = load_masters(landing_dirs, cfg.sources, dq)
    reg_path = state_dir / "file_registry.csv"
    registry = pd.read_csv(reg_path, dtype=str) if reg_path.exists() else pd.DataFrame(columns=["sha256", "file", "first_seen"])
    data, quarantine, file_log = ({}, pd.DataFrame(), pd.DataFrame())
    if not dq.blocking:
        data, quarantine, file_log = ingest(landing_dirs, cfg, masters, dq)
        check_arrivals(file_log, masters, cfg, target_month, as_of, cal, registry, dq)
        check_row_counts(data, cfg, target_month, dq)
    manifest = {
        "run_id": run_id, "profile": cfg.profile["name"], "is_dummy": cfg.is_dummy, "profile_label": cfg.profile.get("label"),
        "target_month": target_month, "as_of": as_of, "landing": [str(d) for d in landing_dirs],
        "generated_at": datetime.now().isoformat(timespec="seconds"), "code_version": __version__,
        "config_revision": _git_rev(cfg.root), "thresholds_status": cfg.thresholds.get("status"),
        "kpi_status": {k["id"]: k.get("status") for k in cfg.kpis}, "finalized": False,
    }
    if dq.blocking:
        manifest["status"] = "blocked"
        dqf = dq.frame()
        dqf.to_csv(out_dir / "dq_issues.csv", index=False, encoding="utf-8-sig")
        if len(file_log):
            file_log.to_csv(out_dir / "file_log.csv", index=False, encoding="utf-8-sig")
        manifest["blocking_issues"] = dqf[dqf.blocking].message.tolist()
        (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        history.append({"run_id": run_id, "target_month": target_month, "as_of": as_of, "status": "blocked"})
        runs_log.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        return RunResult("blocked", out_dir, run_id, {"dq": dqf}, manifest)

    con = build_db(data, masters, as_of, dq)
    fine = add_dims(compute_fine(con, cfg.kpis), masters)
    big = largest_orders(con)
    cfg._label_maps = label_maps(masters)
    from .analysis import analyze
    an = analyze(fine, cfg, masters, target_month, as_of, dq.frame(), big)

    prev_path = state_dir / "latest_kpi_monthly.parquet"
    prev = pd.read_parquet(prev_path) if prev_path.exists() else None
    prev_run = history[-1]["run_id"] if history else ""
    restatements = compare_snapshot(an, prev, cfg, target_month, dq, prev_run)

    alerts, stats = evaluate(an, fine, cfg, target_month, as_of)
    meeting = cal.nth_business_day(next_month(target_month), cfg.analysis.get("meeting_business_day", 8))
    next_meeting = cal.nth_business_day(next_month(target_month, 2), cfg.analysis.get("meeting_business_day", 8))
    due = cal.add_business_days(next_meeting, -cfg.analysis.get("action_due_offset_business_days", 3))
    dqf = dq.frame()
    dq_due = cal.add_business_days(pd.Timestamp(as_of), 2)
    actions = build_actions(alerts, dqf, cfg, masters, due, dq_due)
    rdir = review_dir if review_dir is not None else (cfg.root / cfg.profile["review_dir"] if cfg.profile.get("review_dir") else None)
    alerts, actions, comments = merge_review(alerts, actions, rdir, target_month)

    manifest.update(status="ok", meeting_date=meeting.strftime("%Y-%m-%d"), action_due_default=due.strftime("%Y-%m-%d"),
                    **stats, finalized=finalize,
                    counts={"alerts": alerts.priority.value_counts().to_dict() if len(alerts) else {},
                            "dq": dqf.priority.value_counts().to_dict() if len(dqf) else {},
                            "actions": len(actions), "quarantine_rows": int(len(quarantine)),
                            "restatements": int(len(restatements))})
    frames = {"analysis": an, "fine": fine, "alerts": alerts, "actions": actions, "dq": dqf, "quarantine": quarantine,
              "file_log": file_log, "restatements": restatements, "comments": comments, "masters": masters}

    from .export import write_outputs
    write_outputs(frames, cfg, manifest, out_dir, write_dashboard=write_dashboard)

    # 状態の保存（次回の過去値修正の検出・期限後到着の判定に使う）
    an.to_parquet(prev_path, index=False)
    an.to_parquet(state_dir / f"snapshot_{run_id}.parquet", index=False)
    new_reg = file_log[file_log.status == "loaded"][["sha256", "file"]].assign(first_seen=as_of)
    pd.concat([registry, new_reg[~new_reg.sha256.isin(registry.sha256)]]).to_csv(reg_path, index=False)
    history.append({"run_id": run_id, "target_month": target_month, "as_of": as_of, "status": "ok", "finalized": finalize})
    runs_log.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return RunResult("ok", out_dir, run_id, frames, manifest)


def main(argv=None):
    ap = argparse.ArgumentParser(description="月次KPIパイプライン")
    ap.add_argument("--profile", default="dummy")
    ap.add_argument("--landing", nargs="+", required=True, help="取込対象フォルダ（後に書いたものほど新しい到着）")
    ap.add_argument("--target-month", required=True)
    ap.add_argument("--as-of", required=True, help="取込日（未着判定・期限判定に使用）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--state", default=None)
    ap.add_argument("--review-dir", default=None)
    ap.add_argument("--extra-kpi", nargs="*", default=[])
    ap.add_argument("--finalize", action="store_true", help="定例後の確定版として記録する")
    args = ap.parse_args(argv)
    cfg = load_config(args.profile, extra_kpi_files=[Path(p) for p in args.extra_kpi])
    res = run(cfg, [Path(p) for p in args.landing], args.target_month, args.as_of, Path(args.out),
              Path(args.state) if args.state else None, Path(args.review_dir) if args.review_dir else None, args.finalize)
    print(json.dumps({"status": res.status, "run_id": res.run_id, "out": str(res.out_dir),
                      "counts": res.manifest.get("counts"), "blocking": res.manifest.get("blocking_issues")},
                     ensure_ascii=False, indent=2))
    return 0 if res.status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
