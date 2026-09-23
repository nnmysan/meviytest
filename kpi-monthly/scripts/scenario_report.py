"""検証シナリオの「入力 → 期待結果 → 実際の結果」を Markdown にまとめる。

    PYTHONPATH=src:. python -m kpimonthly.verify --scenario all --out out/scenarios
    PYTHONPATH=src:. python scripts/scenario_report.py out/scenarios/verification.json docs/scenarios.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from generator import params as P  # noqa: E402
from kpimonthly.config import load_config  # noqa: E402

CFG = load_config("dummy", extra_kpi_files=[ROOT / "generator/scenarios/extra/avg_order_value.yaml"])
KNAME = {k["id"]: k["name"] for k in CFG.kpis}
RULE = {"target_miss": "目標未達", "abnormal": "急変", "seasonal": "季節要因の可能性", "trend": "連続悪化",
        "large_order": "大口案件"}
DQ = {"missing_file": "ファイル未着", "late_arrival": "期限後の到着", "schema_error": "列構成の不一致",
      "encoding_mismatch": "文字コードの相違", "quarantine_rows": "不正な行の隔離", "exact_duplicates": "完全重複行",
      "key_conflicts": "同一キーで値が異なる行", "duplicate_file": "同一ファイルの再送", "unknown_code": "未登録コード",
      "control_mismatch": "件数・金額の照合不一致", "restatement": "過去値の修正"}
COL = {"cause_type": "示唆の種類", "consecutive_miss": "連続未達月数", "top_contributor": "最大の寄与内訳",
       "label": "ラベル", "pct": "前月比", "diff": "前月差", "prev_status": "前月の状態", "small_sample": "母数少フラグ",
       "value": "値", "data_status": "データ状態", "note": "注記"}


def scope(key: str) -> str:
    if key in (None, "ALL"):
        return "全社"
    names = {**{e: v["name"] for e, v in P.ENTITIES.items()}, **{p: v["name"] for p, v in P.PRODUCTS.items()}, "UNK": "未分類"}
    return " × ".join(names.get(kv.split("=")[1], kv.split("=")[1]) for kv in key.split("|"))


def op(s: dict) -> str:
    for k, t in (("equals", "＝ {}"), ("ge", "≧ {}"), ("le", "≦ {}"), ("abs_le", "の絶対値 ≦ {}"), ("contains", "に「{}」を含む"),
                 ("approx_fact", "＝ 注入値（{}）")):
        if k in s:
            return t.format(s[k])
    return ""


def describe(c: dict) -> str:
    s, k = c["spec"], c["check"]
    kn = KNAME.get(s.get("kpi"), s.get("kpi", ""))
    run = f"（{s['run']}回目の取込）" if "run" in s else ""
    if k == "status":
        return ("取込・集計が正常終了" if s["equals"] == "ok" else "取込を停止し、集計結果を公開しない") + run
    if k == "truth_match":
        kp = "全KPI" if s.get("kpis", "all") == "all" else "・".join(KNAME[x] for x in s["kpis"])
        sc = "・".join(scope(x) for x in s["scope_keys"]) if "scope_keys" in s else "・".join(s.get("scope_types", []))
        return f"{kp}の値が正解値と一致（{sc}、{s['months'][0]}〜{s['months'][1]}）"
    if k == "alert":
        rule = RULE.get(s.get("rule"), "いずれか")
        lab = f"、ラベル「{s['label']}」" if "label" in s else ""
        return f"{kn}・{scope(s['scope_key'])}に「{rule}」アラート（{s.get('min_priority', 'INFO')}以上{lab}）"
    if k == "alert_field":
        return f"{kn}・{scope(s['scope_key'])}の{RULE.get(s.get('rule'), '')}アラートの{COL.get(s['column'], s['column'])} {op(s)}"
    if k == "no_alert":
        tgt = f"{kn}・{scope(s['scope_key'])}" if "scope_key" in s else (kn or ("業績" if s.get("category") else "全体"))
        return f"{tgt}に {'・'.join(s.get('priorities', ['P1', 'P2']))} のアラートが出ない（誤検知しない）"
    if k == "dq":
        cnt = "（件数＝注入数）" if "count_equals_fact" in s else f"（{s['count_ge']}件以上）" if "count_ge" in s else ""
        msg = f"、内容に「{s['message_contains']}」" if "message_contains" in s else ""
        return f"データ品質「{DQ.get(s['rule'], s['rule'])}」を検知（{s.get('min_priority', 'INFO')}以上）{cnt}{msg}{run}"
    if k == "no_dq":
        return "データ品質の P1・P2 を誤検知しない"
    if k == "field":
        return f"{kn}・{scope(s['scope_key'])}・{s['month']} の{COL.get(s['column'], s['column'])} {op(s)}{run}"
    if k == "manifest":
        return "データ未着の内訳は業績判定を保留（保留件数 ≧ 1）"
    if k == "restatement":
        extra = "、変化率＝注入値" if "rel_change_fact" in s else ""
        return f"修正ログに {kn}・{scope(s['scope_key'])}・{s['month']} を「{s.get('kind', '')}」として記録{extra}"
    if k == "action":
        parts = []
        if "owner_contains" in s:
            parts.append(f"担当に「{s['owner_contains']}」")
        if "verb" in s:
            parts.append(f"表現は「{s['verb']}」にとどめる")
        if "hypothesis_contains" in s:
            parts.append(f"仮説に「{s['hypothesis_contains']}」")
        return f"アクション提案：{kn}・{scope(s['scope_key'])}（{'、'.join(parts)}）。仮説は未検証として扱う"
    if k == "quarantine":
        return "隔離した行数＝注入した不正行数"
    if k == "ratio_of_truth":
        return f"追加KPI「{kn}」（{scope(s['scope_key'])}）＝ 正解の受注金額 ÷ 受注件数"
    if k == "dashboard_has_kpi":
        return f"ダッシュボードに追加KPI「{kn}」が表示される"
    if k == "fx_ratio":
        return "実績レート換算 ÷ 予算レート換算 ＝ 注入した為替の比率（為替影響を分離）"
    if k == "idempotent":
        return "同じ入力で再実行しても全KPI値が完全一致（冪等）"
    return k


def effects_text(effs: list[dict]) -> str:
    if not effs:
        return "注入なし（季節性・成長・ランダムなばらつきのみ）"
    return "<br>".join("`" + ", ".join(f"{k}={v}" for k, v in e.items()) + "`" for e in effs)


def main(src: str, dst: str) -> None:
    report = json.loads(Path(src).read_text(encoding="utf-8"))
    n_ok = sum(r["passed"] for r in report)
    n_chk = sum(len(r["results"]) for r in report)
    n_chk_ok = sum(c["ok"] for r in report for c in r["results"])
    lines = [
        "# 検証シナリオ：入力 → 期待結果 → 実際の結果",
        "",
        "ダミーデータ生成器（`generator/`）で異常を注入し、パイプラインの出力が期待どおりかを自動で照合した結果です。",
        "正解値（truth）は、生成元の業務データから**パイプラインとは別の実装**（pandas）で計算しています。",
        "",
        f"- シナリオ：{n_ok} / {len(report)} 件合格",
        f"- 照合項目：{n_chk_ok} / {n_chk} 件合格",
        "- 再現性：シナリオごとに seed を固定（同じ seed なら出力ファイルはバイト単位で同一。`tests/test_scenarios.py`）",
        "- 再実行：`PYTHONPATH=src:. python -m kpimonthly.verify --scenario all`（全件で約8分）",
        "",
        "| # | シナリオ | 結果 |", "|---|---|---|",
    ]
    lines += [f"| {r['id']} | [{r['name']}](#{r['id'].lower()}) | {'✅ 合格' if r['passed'] else '❌ 不合格'} |" for r in report]
    for r in report:
        lines += ["", f"## {r['id']}", "", f"### {r['name']}", "", f"**入力**：{r['description']}", "",
                  f"注入した効果：{effects_text(r['effects'])}", ""]
        runs = "、".join(f"{i}回目 取込日 {x['as_of']}（{x['status']}）" for i, x in enumerate(r["runs"], 1))
        lines += [f"取込：{runs}", "", "| 期待結果 | 判定 | 実際の出力 |", "|---|---|---|"]
        for c in r["results"]:
            detail = c["detail"].replace("|", "｜").replace("\n", " ")
            lines.append(f"| {describe(c)} | {'✅' if c['ok'] else '❌'} | {detail} |")
    Path(dst).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {dst}: {n_ok}/{len(report)} scenarios, {n_chk_ok}/{n_chk} checks")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
