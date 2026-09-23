"""検証シナリオ：ダミーデータ生成 → パイプライン → 期待結果との照合（generator/scenarios/*.yaml）。

    pytest -m slow          # 全シナリオ（数分）
    pytest -m "not slow"    # 単体テストのみ
"""
import json
from pathlib import Path

import pytest

from generator.generate import generate, load_scenario
from kpimonthly.verify import SCENARIO_DIR, run_scenario

IDS = sorted((p.stem for p in SCENARIO_DIR.glob("S*.yaml")), key=lambda s: int(s[1:]))


@pytest.mark.slow
@pytest.mark.parametrize("sid", IDS)
def test_scenario(sid, tmp_path):
    r = run_scenario(sid, tmp_path / "data", tmp_path / "out", write_dashboard=False)
    failed = [f"{c['check']}: {c['detail']}" for c in r["results"] if not c["ok"]]
    assert not failed, "\n".join(failed)


@pytest.mark.slow
def test_generator_is_reproducible(tmp_path):
    """同じ seed なら、生成されるファイルはバイト単位で同一。"""
    scn = load_scenario("S0")
    a = json.loads((generate(scn, tmp_path / "a") / "manifest.json").read_text(encoding="utf-8"))
    b = json.loads((generate(scn, tmp_path / "b") / "manifest.json").read_text(encoding="utf-8"))
    assert a["file_hashes"] == b["file_hashes"]
    assert len(a["file_hashes"]) > 500
