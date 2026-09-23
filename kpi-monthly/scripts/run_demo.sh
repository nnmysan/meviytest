#!/usr/bin/env bash
# デモ：複数の事象を含む月のダミーデータを作り、パイプラインを実行してダッシュボードを出力する。
set -euo pipefail
cd "$(dirname "$0")/.."
python -m generator.generate --scenario DEMO --out data/dummy
rm -rf out/DEMO
PYTHONPATH=src python -m kpimonthly.pipeline \
  --landing data/dummy/DEMO/landing/batch_01 --target-month 2026-08 --as-of 2026-09-08 \
  --out out/DEMO --review-dir config/review/dummy
echo "ダッシュボード: out/DEMO/dashboard.html"
