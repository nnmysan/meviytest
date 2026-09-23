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


# ---------------------------------------------------------------- サプライヤ・当月進捗

def test_supplier_levels_and_weekdays():
    from kpimonthly.supplier import _level, weekdays
    th = {"levels": {"p2": 0.80, "p1": 0.95, "over": 1.00}}
    assert [_level(v, th) for v in (0.5, 0.8, 0.81, 0.96, 1.2)] == ["", "", "p2", "p1", "over"]
    assert weekdays("2026-09-01", "2026-09-30") == 22     # 2026年9月の平日
    assert weekdays("2026-09-05", "2026-09-06") == 0      # 土日


def test_supplier_notifications_only_on_new_escalated_or_resolved(tmp_path):
    from kpimonthly.supplier import _notifications, save_state
    sup = pd.DataFrame([{"supplier_code": "S1", "owner": "担当A", "manager": "課長B"}])
    a = {"scope_key": "supplier_code=S1|material=M", "level": "p2", "priority": "P2", "supplier_code": "S1",
         "scope_label": "S1×M", "rule_label": "80%超", "message": "m", "alert_id": "A1"}
    n1, st = _notifications([a], [], sup, tmp_path, "2026-09-08")
    assert [x["reason"] for x in n1] == ["新規"] and n1[0]["to"] == "担当A" and n1[0]["cc"] == "課長B"
    save_state(tmp_path, st)
    n2, st = _notifications([a], [], sup, tmp_path, "2026-09-09")          # 翌朝も同じ状態 → 通知しない
    assert n2 == []
    save_state(tmp_path, st)
    n3, st = _notifications([{**a, "level": "over", "priority": "P1"}], [], sup, tmp_path, "2026-09-10")  # 悪化
    assert [x["reason"] for x in n3] == ["悪化"]
    save_state(tmp_path, st)
    n4, _ = _notifications([], [], sup, tmp_path, "2026-09-11")             # 解消
    assert [x["reason"] for x in n4] == ["解消"]


def test_pacing_forecast_is_simple_run_rate():
    from types import SimpleNamespace
    from kpimonthly.pacing import compute_pacing
    # 2026-09-01〜09-07 は平日5日。1日100万円 → 当月累計500万円、9月の平日22日で着地見込み2,200万円
    days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"]
    daily = pd.DataFrame([{"kpi_id": "order_amount", "day": d, "entity_code": "JP", "product_code": "MSQ", "num": 1e6, "den": np.nan}
                          for d in days] + [{"kpi_id": "order_count", "day": d, "entity_code": "JP", "product_code": "MSQ", "num": 100, "den": np.nan}
                                            for d in days])
    kpis = [{"id": "order_amount", "name": "受注金額", "type": "sum", "display": "yen", "direction": "higher_is_better"},
            {"id": "order_count", "name": "受注件数", "type": "sum", "display": "count", "direction": "higher_is_better"}]
    cfg = SimpleNamespace(kpis=kpis, g={"pace": {"p2": 0.9, "p1": 0.8, "min_business_days": 5, "significance": 2.0}},
                          kpi=lambda k: next(x for x in kpis if x["id"] == k), kpi_thresholds=lambda k: {})
    targets = pd.DataFrame([{"kpi_id": "order_amount", "month": "2026-09", "scope_type": "total", "scope_key": "ALL", "target": 4e7}])
    pace, alerts = compute_pacing(daily, cfg, {"targets": targets}, "2026-09-07", None, {})
    r = pace[(pace.kpi_id == "order_amount") & (pace.scope_key == "ALL")].iloc[0]
    assert r.mtd == 5e6 and r.bdays_elapsed == 5 and r.bdays_total == 22
    assert r.forecast == pytest.approx(2.2e7) and r.achievement_forecast == pytest.approx(0.55)
    assert [a["priority"] for a in alerts if a["scope_key"] == "ALL"] == ["P1"]
