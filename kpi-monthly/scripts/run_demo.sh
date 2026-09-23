#!/usr/bin/env bash
# デモ：月20万行×25か月のダミーデータを作り、毎朝の更新と同じ条件でパイプラインを実行してダッシュボードを出力する。
# 生成に数分、集計に数分かかります（約500万行）。小さく試す場合は DEMO を指定: ./scripts/run_demo.sh DEMO
set -euo pipefail
cd "$(dirname "$0")/.."
SCN="${1:-DEMO_LARGE}"
AS_OF=$(python -c "import yaml;print(yaml.safe_load(open('generator/scenarios/$SCN.yaml')).get('as_of','2026-09-08'))")
python -m generator.generate --scenario "$SCN" --out data/dummy
rm -rf "out/$SCN"
PYTHONPATH=src python -m kpimonthly.pipeline \
  --landing "data/dummy/$SCN/landing/batch_01" --as-of "$AS_OF" \
  --out "out/$SCN" --review-dir config/review/dummy
echo "ダッシュボード: out/$SCN/dashboard.html"
