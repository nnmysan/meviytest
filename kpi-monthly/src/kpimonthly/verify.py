"""検証シナリオの実行と照合。

シナリオ YAML の expect（期待結果）を、パイプラインの出力と突き合わせる。
正解値（truth.csv）と注入した事実（facts.json）は生成器が独立に計算したもの。

    PYTHONPATH=src:. python -m kpimonthly.verify --scenario all
"""
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ROOT, load_config
from .dq import PRIORITY_ORDER
from .pipeline import run

SCENARIO_DIR = ROOT / "generator" / "scenarios"


@dataclass
class CheckResult:
    check: str
    ok: bool
    detail: str
    spec: dict


def _fact(facts: dict, path: str):
    cur = facts
    for p in path.split("."):
        cur = cur[p]
    return cur


def _prio_ok(p: str, min_p: str) -> bool:
    return PRIORITY_ORDER[p] <= PRIORITY_ORDER[min_p]


def _months(spec: dict) -> list[str]:
    a, b = spec["months"]
    return [str(p) for p in pd.period_range(a, b, freq="M")]


def evaluate_checks(scn: dict, runs: list, truth: pd.DataFrame, facts: dict) -> list[CheckResult]:
    out = []
    for spec in scn.get("expect", []):
        res = runs[spec.get("run", len(runs)) - 1]
        f = res.frames
        c = spec["check"]
        try:
            ok, detail = CHECKS[c](spec, res, f, truth, facts, runs)
        except Exception as e:  # 期待した出力がない等
            ok, detail = False, f"{type(e).__name__}: {e}"
        out.append(CheckResult(c, bool(ok), detail, spec))
    return out


def _an_row(f, spec, month=None):
    an = f["analysis"]
    r = an[(an.kpi_id == spec["kpi"]) & (an.scope_key == spec["scope_key"]) & (an.month == (month or spec["month"]))]
    if r.empty:
        raise KeyError(f"{spec['kpi']} {spec['scope_key']} {month or spec['month']} の分析行がありません")
    return r.iloc[0]


def ck_status(spec, res, f, truth, facts, runs):
    return res.status == spec["equals"], f"status={res.status}"


def ck_truth_match(spec, res, f, truth, facts, runs):
    an = f["analysis"]
    t = truth[truth.month.isin(_months(spec))]
    if spec.get("kpis", "all") != "all":
        t = t[t.kpi_id.isin(spec["kpis"])]
    if "scope_types" in spec:
        t = t[t.scope_type.isin(spec["scope_types"])]
    if "scope_keys" in spec:
        t = t[t.scope_key.isin(spec["scope_keys"])]
    m = t.merge(an[["kpi_id", "scope_key", "month", "value"]], on=["kpi_id", "scope_key", "month"], how="left",
                suffixes=("_truth", "_pipeline"))
    both_nan = m.value_truth.isna() & m.value_pipeline.isna()
    err = (m.value_pipeline - m.value_truth).abs() / m.value_truth.abs().clip(lower=1e-9)
    bad = m[~both_nan & ~(err <= spec.get("tol_rel", 1e-8))]
    detail = f"{len(m)} 値を照合、不一致 {len(bad)}"
    if len(bad):
        detail += "（例: " + "; ".join(f"{r.kpi_id} {r.scope_key} {r.month} 正解={r.value_truth} 出力={r.value_pipeline}"
                                        for r in bad.head(3).itertuples()) + "）"
    return len(bad) == 0 and len(m) > 0, detail


def _alerts(f, spec):
    al = f["alerts"]
    if al.empty:
        return al
    sel = al
    for k, col in (("kpi", "kpi_id"), ("scope_key", "scope_key"), ("rule", "rule"), ("category", "category")):
        if k in spec:
            sel = sel[sel[col] == spec[k]]
    return sel


def ck_alert(spec, res, f, truth, facts, runs):
    sel = _alerts(f, spec)
    sel = sel[sel.priority.map(lambda p: _prio_ok(p, spec.get("min_priority", "INFO")))] if len(sel) else sel
    if "label" in spec and len(sel):
        sel = sel[sel.label == spec["label"]]
    if len(sel):
        a = sel.iloc[0]
        return True, f"{a.priority} {a.rule_label} {a.scope_label}: {a.message}" + (f" ［{a.label}］" if a.label else "")
    al = _alerts(f, {k: v for k, v in spec.items() if k in ("kpi", "scope_key")})
    return False, "該当アラートなし（同じKPI・内訳のアラート: " + (", ".join(f"{r.priority}/{r.rule}" for r in al.itertuples()) or "なし") + "）"


def ck_alert_field(spec, res, f, truth, facts, runs):
    sel = _alerts(f, spec)
    if sel.empty:
        return False, "対象アラートなし"
    v = sel.iloc[0][spec["column"]]
    return _compare(v, spec, facts)


def ck_no_alert(spec, res, f, truth, facts, runs):
    sel = _alerts(f, spec)
    if len(sel):
        sel = sel[sel.priority.isin(spec.get("priorities", ["P1", "P2"]))]
    if len(sel):
        return False, f"想定外のアラート {len(sel)} 件: " + "; ".join(f"{r.priority} {r.kpi_id} {r.scope_label} {r.rule}"
                                                               for r in sel.head(5).itertuples())
    return True, "該当なし"


def ck_dq(spec, res, f, truth, facts, runs):
    dq = f["dq"]
    sel = dq[dq.rule == spec["rule"]]
    sel = sel[sel.priority.map(lambda p: _prio_ok(p, spec.get("min_priority", "INFO")))]
    if "message_contains" in spec:
        sel = sel[sel.message.str.contains(spec["message_contains"])]
    if sel.empty:
        return False, f"DQ {spec['rule']} なし（検出: {sorted(set(dq.rule))}）"
    if "count_ge" in spec and len(sel) < spec["count_ge"]:
        return False, f"{len(sel)} 件（{spec['count_ge']} 件以上を期待）"
    if "count_equals_fact" in spec:
        exp = _fact(facts, spec["count_equals_fact"])
        got = int(sel["count"].sum())
        return got == exp, f"件数 {got}（注入 {exp}）: {sel.iloc[0].message}"
    return True, f"{len(sel)} 件: {sel.iloc[0].priority} {sel.iloc[0].message}"


def ck_no_dq(spec, res, f, truth, facts, runs):
    dq = f["dq"]
    sel = dq[dq.priority.isin(spec.get("priorities", ["P1", "P2"]))]
    return sel.empty, "該当なし" if sel.empty else "; ".join(sel.message.head(3))


def _compare(v, spec, facts):
    if "equals" in spec:
        ok = (bool(v) == spec["equals"]) if isinstance(spec["equals"], bool) else v == spec["equals"]
        return ok, f"値={v}"
    if "contains" in spec:
        return spec["contains"] in str(v), f"値={v}"
    if "ge" in spec:
        return v >= spec["ge"], f"値={v}"
    if "le" in spec:
        return v <= spec["le"], f"値={v}"
    if "abs_le" in spec:
        return abs(v) <= spec["abs_le"], f"値={v}"
    if "approx_fact" in spec:
        exp = _fact(facts, spec["approx_fact"])
        return abs(v - exp) <= 1e-6 * max(1, abs(exp)), f"値={v:,.2f} 注入={exp:,.2f}"
    raise ValueError("比較条件がありません")


def ck_field(spec, res, f, truth, facts, runs):
    r = _an_row(f, spec)
    return _compare(r[spec["column"]], spec, facts)


def ck_manifest(spec, res, f, truth, facts, runs):
    v = res.manifest.get(spec["key"])
    return v is not None and v >= spec["ge"], f"{spec['key']}={v}"


def ck_restatement(spec, res, f, truth, facts, runs):
    rs = f["restatements"]
    sel = rs[(rs.kpi_id == spec["kpi"]) & (rs.scope_key == spec["scope_key"]) & (rs.month == spec["month"])]
    if "kind" in spec:
        sel = sel[sel.kind == spec["kind"]]
    if sel.empty:
        return False, f"修正ログなし（{len(rs)} 件中）"
    r = sel.iloc[0]
    detail = f"{r.kind}: {r.prev_run_value:,.0f} → {r.value:,.0f}（{r.rel_change:+.2%}）"
    if "rel_change_fact" in spec:
        exp = _fact(facts, spec["rel_change_fact"])
        return abs(r.rel_change - exp) < 1e-6, detail + f" 注入={exp:+.2%}"
    return True, detail


def ck_action(spec, res, f, truth, facts, runs):
    acts = [a for a in f["actions"] if a["kpi_id"] == spec["kpi"] and a["scope_key"] == spec["scope_key"]]
    if not acts:
        return False, "アクション提案なし"
    a = acts[0]
    problems = []
    if "owner_contains" in spec and spec["owner_contains"] not in a["owner"]:
        problems.append(f"担当者={a['owner']}")
    if "verb" in spec and a["verb"] != spec["verb"]:
        problems.append(f"動詞={a['verb']}")
    if "hypothesis_contains" in spec and not any(spec["hypothesis_contains"] in h["text"] for h in a["hypotheses"]):
        problems.append("仮説に該当語なし")
    if any(h["status"] not in ("未検証", "データで示唆") for h in a["hypotheses"]):
        problems.append("仮説が検証済み扱いになっている")
    return not problems, f"{a['title']} / 担当 {a['owner']} / 期限 {a['due_date']}" + (f" 問題: {problems}" if problems else "")


def ck_quarantine(spec, res, f, truth, facts, runs):
    exp = _fact(facts, spec["equals_fact"])
    return len(f["quarantine"]) == exp, f"隔離 {len(f['quarantine'])} 行（注入 {exp} 行）"


def ck_ratio_of_truth(spec, res, f, truth, facts, runs):
    r = _an_row(f, spec)
    t = truth[(truth.scope_key == spec["scope_key"]) & (truth.month == spec["month"])].set_index("kpi_id").value
    exp = t[spec["num_kpi"]] / t[spec["den_kpi"]]
    return abs(r.value - exp) <= 1e-8 * exp, f"値={r.value:,.1f} 正解={exp:,.1f}"


def ck_dashboard_has_kpi(spec, res, f, truth, facts, runs):
    b = json.loads((res.out_dir / "dashboard_data.json").read_text(encoding="utf-8"))
    ids = [k["id"] for k in b["kpis"]]
    return spec["kpi"] in ids, f"ダッシュボードのKPI: {ids}"


def ck_fx_ratio(spec, res, f, truth, facts, runs):
    r = _an_row(f, spec)
    fx = facts["fx_shock"]
    exp = fx["actual_rate"] / fx["budget_rate"]
    got = r.value_actual_fx / r.value
    return abs(got - exp) < 1e-9, f"実績レート換算/予算レート換算={got:.4f}（期待 {exp:.4f}）、為替影響 {r.fx_effect:,.0f}円"


def ck_idempotent(spec, res, f, truth, facts, runs):
    """同じ入力で再実行しても KPI 値が変わらないこと。"""
    cfg = res.frames["_cfg"]
    tmp = res.out_dir.parent / "_idempotent"
    if tmp.exists():
        shutil.rmtree(tmp)
    r2 = run(cfg, res.frames["_landing"], res.manifest["target_month"], res.manifest["as_of"], tmp, tmp / "state",
             write_dashboard=False)
    a, b = f["analysis"], r2.frames["analysis"]
    cols = ["kpi_id", "scope_key", "month", "value"]
    m = a[cols].merge(b[cols], on=cols[:3], suffixes=("_1", "_2"))
    diff = ~((m.value_1 == m.value_2) | (m.value_1.isna() & m.value_2.isna()))
    shutil.rmtree(tmp)
    return not diff.any() and len(m) == len(a), f"{len(m)} 値を比較、差異 {int(diff.sum())}"


def _sup(f):
    sup = f.get("supplier") or {}
    if not sup.get("enabled"):
        raise KeyError("サプライヤ分析が実行されていません")
    return sup


def ck_supplier_truth(spec, res, f, truth, facts, runs):
    """締め月の充足率（全組み合わせ＋サプライヤ全体）が、生成元から独立に計算した値と一致すること。"""
    t = res.frames["_truth_supplier"]
    t = t[t.month == spec["month"]]
    tbl = _sup(f)["bases"]["month"]
    keys = ["supplier_code", "material", "part_size", "surface", "heat"]
    m = t[t.material != "*"].merge(tbl[keys + ["load_count", "load_qty"]], on=keys, suffixes=("_t", "_p"))
    sp = tbl.groupby("supplier_code")[["lines", "qty", "cap_lines", "cap_qty"]].sum()
    sp = (sp.lines / sp.cap_lines).rename("load_count_p").reset_index()
    ms = t[t.material == "*"].merge(sp, on="supplier_code")
    bad = int((~np.isclose(m.load_count_t, m.load_count_p, rtol=1e-9)).sum() + (~np.isclose(m.load_qty_t, m.load_qty_p, rtol=1e-9)).sum()
              + (~np.isclose(ms.load_count, ms.load_count_p, rtol=1e-9)).sum())
    return bad == 0 and len(m) > 0, f"組み合わせ {len(m)}・サプライヤ {len(ms)} の充足率を照合、不一致 {bad}"


def ck_supplier_eval(spec, res, f, truth, facts, runs):
    ev = _sup(f)["evals"]
    r = ev[(ev.scope_key == spec["scope_key"]) & (ev.basis == spec["basis"])]
    if r.empty:
        return False, "評価行なし（最低件数に満たない可能性）"
    v = float(r.max_load.iloc[0])
    return v >= spec["load_ge"], f"{spec['basis']} の充足率 {v:.0%}（{r.period.iloc[0]}）"


def ck_supplier_action(spec, res, f, truth, facts, runs):
    acts = [a for a in f["actions"] if a.get("category") == "供給" and a["scope_key"] == spec["scope_key"]]
    if not acts:
        return False, "供給のアクション提案なし"
    a = acts[0]
    types = [p["type"] for p in a.get("proposals", [])]
    problems = [f"「{x}」がない" for x in spec.get("include", []) if x not in types]
    problems += [f"「{x}」があってはいけない" for x in spec.get("exclude", []) if x in types]
    if "candidate" in spec and not any(spec["candidate"] in (p.get("basis") or "") for p in a.get("proposals", [])):
        problems.append(f"振替候補に {spec['candidate']} がない")
    if "owner_contains" in spec and spec["owner_contains"] not in a["owner"]:
        problems.append(f"担当={a['owner']}")
    return not problems, f"打ち手: {types} / 担当 {a['owner']}・{a['manager']} / 期限 {a['due_date']}" + (f" 問題: {problems}" if problems else "")


def ck_notification(spec, res, f, truth, facts, runs):
    ns = [n for n in _sup(f)["notifications"] if n["scope_key"] == spec["scope_key"]]
    if not ns:
        return False, "通知なし"
    n = ns[0]
    ok = n["reason"] == spec.get("reason", n["reason"]) and spec.get("to_contains", "") in n["to"] and spec.get("cc_contains", "") in n["cc"]
    return ok, f"{n['channel']} 宛先 {n['to']} / CC {n['cc']} / {n['reason']}: {n['title']}"


def ck_notify_rerun(spec, res, f, truth, facts, runs):
    """同じデータで翌朝もう一度実行しても、同じ事象は再通知されないこと（状態を引き継ぐ）。"""
    cfg = res.frames["_cfg"]
    r2 = run(cfg, res.frames["_landing"], res.manifest["target_month"], res.manifest["as_of"], res.out_dir.parent / "_rerun",
             res.out_dir.parent / "state", write_dashboard=False)
    ns = [n for n in r2.frames["supplier"]["notifications"] if n["scope_key"] == spec["scope_key"]]
    shutil.rmtree(res.out_dir.parent / "_rerun", ignore_errors=True)
    still = any(a["scope_key"] == spec["scope_key"] for a in r2.frames["supplier"]["alerts"])
    return not ns and still, f"再実行: アラート継続={still}、再通知 {len(ns)} 件"


def ck_pace_truth(spec, res, f, truth, facts, runs):
    p = f["pace"]
    r = p[(p.kpi_id == spec["kpi"]) & (p.scope_key == spec["scope_key"])]
    t = truth[(truth.kpi_id == spec["kpi"]) & (truth.scope_key == spec["scope_key"]) & (truth.month == r.month.iloc[0])]
    got, exp = float(r.mtd.iloc[0]), float(t.value.iloc[0])
    return abs(got - exp) <= 1e-8 * max(1, abs(exp)), f"当月累計 {got:,.0f}（正解 {exp:,.0f}）、着地見込み {float(r.forecast.iloc[0]):,.0f}・見込み達成率 {float(r.achievement_forecast.iloc[0]):.0%}"


CHECKS = {
    "status": ck_status, "truth_match": ck_truth_match, "alert": ck_alert, "alert_field": ck_alert_field,
    "no_alert": ck_no_alert, "dq": ck_dq, "no_dq": ck_no_dq, "field": ck_field, "manifest": ck_manifest,
    "restatement": ck_restatement, "action": ck_action, "quarantine": ck_quarantine,
    "ratio_of_truth": ck_ratio_of_truth, "dashboard_has_kpi": ck_dashboard_has_kpi, "fx_ratio": ck_fx_ratio,
    "idempotent": ck_idempotent, "supplier_truth": ck_supplier_truth, "supplier_eval": ck_supplier_eval,
    "supplier_action": ck_supplier_action, "notification": ck_notification, "notify_rerun": ck_notify_rerun,
    "pace_truth": ck_pace_truth,
}


def load_scenario(sid: str) -> dict:
    import yaml
    return yaml.safe_load((SCENARIO_DIR / f"{sid}.yaml").read_text(encoding="utf-8"))


def run_scenario(sid: str, data_root: Path, out_root: Path, regenerate: bool = True, write_dashboard: bool = True) -> dict:
    from generator.generate import generate
    scn = load_scenario(sid)
    sdir = data_root / sid
    if regenerate or not (sdir / "manifest.json").exists():
        generate(scn, data_root)
    gm = json.loads((sdir / "manifest.json").read_text(encoding="utf-8"))
    truth = pd.read_csv(sdir / "truth.csv")
    facts = json.loads((sdir / "facts.json").read_text(encoding="utf-8"))
    cfg = load_config("dummy", extra_kpi_files=[SCENARIO_DIR / p for p in scn.get("extra_kpis", [])])
    odir = out_root / sid
    if odir.exists():
        shutil.rmtree(odir)
    runs = []
    for i, r in enumerate(gm["runs"], start=1):
        landing = [sdir / p for p in r["landing"]]
        res = run(cfg, landing, gm["target_month"], r["as_of"], odir / f"run{i}", odir / "state",
                  review_dir=cfg.root / "config" / "review" / "dummy", write_dashboard=write_dashboard)
        res.frames["_cfg"], res.frames["_landing"] = cfg, landing
        ts = sdir / "truth_supplier.csv"
        res.frames["_truth_supplier"] = pd.read_csv(ts) if ts.exists() else pd.DataFrame()
        runs.append(res)
    results = evaluate_checks(scn, runs, truth, facts)
    return {"id": sid, "name": scn["name"], "description": scn.get("description", ""), "effects": scn.get("effects", []),
            "runs": [{"run_id": r.run_id, "status": r.status, "as_of": r.manifest.get("as_of"), "out": str(r.out_dir)} for r in runs],
            "results": [dict(check=c.check, ok=c.ok, detail=c.detail, spec={k: v for k, v in c.spec.items() if k != "check"})
                        for c in results],
            "passed": all(c.ok for c in results)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", nargs="+", default=["all"])
    ap.add_argument("--data", default="data/dummy")
    ap.add_argument("--out", default="out/scenarios")
    ap.add_argument("--no-regenerate", action="store_true")
    args = ap.parse_args(argv)
    ids = sorted((p.stem for p in SCENARIO_DIR.glob("S*.yaml")), key=lambda s: int(s[1:])) if args.scenario == ["all"] else args.scenario
    report = []
    for sid in ids:
        r = run_scenario(sid, Path(args.data), Path(args.out), regenerate=not args.no_regenerate)
        report.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{mark}] {sid} {r['name']}")
        for c in r["results"]:
            print(f"    {'ok ' if c['ok'] else 'NG '} {c['check']}: {c['detail']}")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if all(r["passed"] for r in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
