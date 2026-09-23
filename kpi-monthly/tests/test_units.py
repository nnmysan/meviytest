"""手計算で確かめられる小さな入力での単体テスト。"""
import numpy as np
import pandas as pd
import pytest

from kpimonthly.alerts import eval_month
from kpimonthly.analysis import count_price, decompose
from kpimonthly.config import ConfigError, load_config
from kpimonthly.ingest import _parse


def _fine(rows):
    return pd.DataFrame(rows, columns=["kpi_id", "month", "product_code", "num", "den"])


def test_ratio_is_aggregated_from_numerator_and_denominator():
    # A: 90/100, B: 1/10。率の単純平均(0.5)ではなく、合算して 91/110 になること
    f = _fine([["r", "2026-08", "A", 90, 100], ["r", "2026-08", "B", 1, 10]])
    tot = f.groupby("month")[["num", "den"]].sum()
    assert tot.num.iloc[0] / tot.den.iloc[0] == pytest.approx(91 / 110)


def test_mix_and_rate_effects_sum_to_total_change():
    kpi = {"id": "r", "type": "ratio"}
    f = _fine([
        ["r", "2026-07", "A", 30, 100], ["r", "2026-07", "B", 20, 100],   # 前月: 50/200 = 25%
        ["r", "2026-08", "A", 30, 100], ["r", "2026-08", "B", 60, 300],   # 当月: 90/400 = 22.5%（各商品の率は不変）
    ])
    d = decompose(f, kpi, {}, "product_code", "2026-08", "2026-07")
    total = 90 / 400 - 50 / 200
    assert d.contribution.sum() == pytest.approx(total)
    assert d.rate.sum() == pytest.approx(0.0)          # 各商品の率は変わっていない
    assert d.mix.sum() == pytest.approx(total)         # 差はすべて構成の変化


def test_new_segment_keeps_identity():
    kpi = {"id": "r", "type": "ratio"}
    f = _fine([["r", "2026-07", "A", 30, 100],
               ["r", "2026-08", "A", 30, 100], ["r", "2026-08", "N", 5, 50]])  # N は当月から
    d = decompose(f, kpi, {}, "product_code", "2026-08", "2026-07")
    assert d.contribution.sum() == pytest.approx(35 / 150 - 0.3)


def test_count_price_decomposition():
    f = pd.DataFrame([
        ["amt", "2026-07", 1000.0, np.nan], ["amt", "2026-08", 1200.0, np.nan],
        ["cnt", "2026-07", 10.0, np.nan], ["cnt", "2026-08", 8.0, np.nan]], columns=["kpi_id", "month", "num", "den"])
    cp = count_price(f, "amt", "cnt", {}, "2026-08", "2026-07")
    assert cp["count_effect"] == pytest.approx(-200)   # 2件減 × 前月単価100
    assert cp["price_effect"] == pytest.approx(400)    # 8件 × 単価増50
    assert cp["count_effect"] + cp["price_effect"] == pytest.approx(200)


def test_parse_normalizes_fullwidth_and_quarantines_invalid():
    spec = {"columns": {
        "amount": {"type": "decimal", "required": True, "min": 0},
        "day": {"type": "date", "required": True},
        "status": {"type": "str", "required": True, "allowed": ["受注", "取消"]},
    }}
    raw = pd.DataFrame({
        "amount": ["１２，３４５", "100", "-5", "10"],
        "day": ["2026/08/03", "2026-08-04", "2026-08-05", "2026/13/01"],
        "status": ["受注", "受注", "受注", "保留"],
    })
    out, reasons = _parse(raw, spec)
    assert out.amount.iloc[0] == 12345
    assert out.day.iloc[0] == pd.Timestamp("2026-08-03")
    assert reasons.iloc[0] == "" and reasons.iloc[1] == ""
    assert "範囲外" in reasons.iloc[2]
    assert "形式不正: day" in reasons.iloc[3] and "許容値外: status" in reasons.iloc[3]


def test_eval_month_waits_for_maturity():
    assert eval_month({"maturity_days": 0}, "2026-08", "2026-09-08") == "2026-08"
    assert eval_month({"maturity_days": 30}, "2026-08", "2026-09-08") == "2026-07"


def test_prod_profile_refuses_provisional_settings():
    with pytest.raises(ConfigError) as e:
        load_config("prod")
    msg = str(e.value)
    assert "閾値が承認されていません" in msg
    assert "KPI定義 order_amount が承認されていません" in msg
    assert "列対応表に TODO" in msg


def test_dummy_profile_loads_kpis_in_business_order():
    cfg = load_config("dummy")
    assert [k["id"] for k in cfg.kpis][:6] == ["order_amount", "order_count", "quote_conversion", "gross_margin",
                                               "on_time_delivery", "defect_rate"]
