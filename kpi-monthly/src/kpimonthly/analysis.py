"""月次分析：内訳別の当月値・前月差・前年同月比・目標差・基準値からの乖離・データ状態。

基準値（季節性を考慮した「今月このくらいのはず」）
    合計KPI: 直近3か月の月平均 × 季節係数（過去1〜2年の「当月 ÷ 直前3か月平均」の平均）
    率KPI  : 直近3か月の率   ＋ 季節差  （過去1〜2年の「当月 − 直前3か月の率」の平均）
乖離 z = （実績−基準値）÷ 過去24か月の乖離のばらつき（MAD）。季節性と成長を織り込んだうえで「いつもと違う」かを測る。
"""
from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd

DIM_COLS = ["entity_code", "product_code", "division_code", "team_code", "section_code", "industry", "size_class", "customer_id"]
SERIES = ["kpi_id", "scope_type", "scope_key"]


def month_range(start: str, end: str) -> list[str]:
    return [str(p) for p in pd.period_range(start, end, freq="M")]


def scope_key_of(df: pd.DataFrame, dims: list[str]) -> pd.Series:
    if not dims:
        return pd.Series("ALL", index=df.index)
    key = dims[0] + "=" + df[dims[0]].astype(str)
    for d in dims[1:]:
        key = key + "|" + d + "=" + df[d].astype(str)
    return key


def rollup(fine: pd.DataFrame, scopes: list[dict], target_month: str) -> pd.DataFrame:
    fine = fine[fine.month <= target_month]
    months = month_range(fine.month.min(), target_month)
    frames = []
    for sc in scopes:
        dims = sc["dims"]
        g = fine.groupby(["kpi_id", "month"] + dims, dropna=False)[["num", "den", "actual_fx"]].sum(min_count=1).reset_index()
        g["scope_type"] = sc["type"]
        g["scope_key"] = scope_key_of(g, dims)
        frames.append(g)
    long = pd.concat(frames, ignore_index=True)
    for c in DIM_COLS:
        if c not in long.columns:
            long[c] = None
    # 系列ごとに初出月から対象月まで月を補完（実績のない月は 0）
    series = long.sort_values("month").groupby(SERIES, as_index=False).first()[SERIES + DIM_COLS + ["month"]]
    series = series.rename(columns={"month": "first_month"})
    grid = series.merge(pd.DataFrame({"month": months}), how="cross")
    grid = grid[grid.month >= grid.first_month]
    out = grid.merge(long[SERIES + ["month", "num", "den", "actual_fx"]], on=SERIES + ["month"], how="left")
    out["num"] = out["num"].fillna(0.0)
    out["den"] = out["den"].fillna(0.0)
    return out.sort_values(SERIES + ["month"]).reset_index(drop=True)


def _mad_scale(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    if len(x) == 0:
        return np.nan
    return 1.4826 * float(np.median(np.abs(x - np.median(x))))


def missing_entities(dq_frame: pd.DataFrame) -> dict[tuple[str, str], set]:
    miss = dq_frame[dq_frame.rule == "missing_file"]
    out: dict[tuple[str, str], set] = {}
    for r in miss.itertuples():
        out.setdefault((r.source, r.month), set()).add(r.entity)
    return out


def analyze(fine: pd.DataFrame, cfg, masters: dict, target_month: str, as_of: str, dq_frame: pd.DataFrame,
            big_orders: pd.DataFrame) -> pd.DataFrame:
    scopes = cfg.analysis["scopes"]
    df = rollup(fine, scopes, target_month)
    kinfo = {k["id"]: k for k in cfg.kpis}
    g = df.groupby(SERIES, sort=False)
    is_ratio = df.kpi_id.map(lambda k: kinfo[k]["type"] == "ratio")
    is_pct = df.kpi_id.map(lambda k: kinfo[k]["display"] == "percent")
    sign = df.kpi_id.map(lambda k: 1.0 if kinfo[k]["direction"] == "higher_is_better" else -1.0)

    with np.errstate(divide="ignore", invalid="ignore"):
        df["value"] = np.where(is_ratio, np.where(df.den > 0, df.num / df.den, np.nan), df.num)
    df["value_actual_fx"] = df["actual_fx"]
    df["fx_effect"] = df["actual_fx"] - df["value"]

    # データ状態（未着・成熟待ち）
    miss = missing_entities(dq_frame)
    as_of_ts = pd.Timestamp(as_of)
    status = pd.Series("OK", index=df.index)
    for (kid, month), idx in df.groupby(["kpi_id", "month"]).groups.items():
        k = kinfo[kid]
        horizon = math.ceil(k.get("maturity_days", 0) / 30)
        ents: set = set()
        for mm in month_range(month, str(pd.Period(month, "M") + horizon)):
            for s in k["sources"]:
                ents |= miss.get((s, mm), set())
        sub = df.loc[idx]
        if ents:
            has_e = sub.entity_code.notna()
            status.loc[sub.index[has_e & sub.entity_code.isin(ents)]] = "未着"
            status.loc[sub.index[~has_e]] = "一部未着"
        if pd.Period(month, "M").end_time.normalize() + pd.Timedelta(days=k.get("maturity_days", 0)) > as_of_ts:
            status.loc[idx] = status.loc[idx].where(status.loc[idx] != "OK", "成熟待ち")
    df["data_status"] = status
    df.loc[df.data_status == "未着", ["value", "value_actual_fx", "fx_effect"]] = np.nan

    # 前月・前年同月
    df["prev_value"] = g["value"].shift(1)
    prev_den = g["den"].shift(1)
    prev_num = g["num"].shift(1)
    prev_status_raw = g["data_status"].shift(1)
    first_row = g.cumcount() == 0
    df["prev_status"] = ""
    df.loc[first_row, "prev_status"] = "新規"
    df.loc[~first_row & (np.where(is_ratio, prev_den == 0, prev_num == 0)), "prev_status"] = "前月0"
    df.loc[prev_status_raw == "未着", "prev_status"] = "データなし"
    df.loc[df.prev_status != "", "prev_value"] = np.nan
    df["diff"] = df.value - df.prev_value
    with np.errstate(divide="ignore", invalid="ignore"):
        df["pct"] = np.where(~is_pct & (df.prev_value > 0), df.value / df.prev_value - 1, np.nan)
        df["yoy_value"] = g["value"].shift(12)
        df["yoy_diff"] = df.value - df.yoy_value
        df["yoy_pct"] = np.where(~is_pct & (df.yoy_value > 0), df.value / df.yoy_value - 1, np.nan)

    # 3か月移動・年度累計（分子・分母を合算してから割る）
    n3 = g["num"].transform(lambda s: s.rolling(3, min_periods=3).sum())
    d3 = g["den"].transform(lambda s: s.rolling(3, min_periods=3).sum())
    with np.errstate(divide="ignore", invalid="ignore"):
        df["ma3"] = np.where(is_ratio, n3 / d3, n3 / 3)
    fy_start = cfg.analysis.get("fiscal_year_start_month", 4)
    mnum = df.month.str[5:].astype(int)
    df["fiscal_year"] = df.month.str[:4].astype(int) - (mnum < fy_start).astype(int)
    ytd_n = df.groupby(SERIES + ["fiscal_year"])["num"].cumsum()
    ytd_d = df.groupby(SERIES + ["fiscal_year"])["den"].cumsum()
    with np.errstate(divide="ignore", invalid="ignore"):
        df["ytd_value"] = np.where(is_ratio, ytd_n / ytd_d, ytd_n)

    # 目標
    tg = masters["targets"][["kpi_id", "month", "scope_type", "scope_key", "target"]]
    df = df.merge(tg, on=["kpi_id", "month", "scope_type", "scope_key"], how="left")
    df["target_gap"] = df.value - df.target
    with np.errstate(divide="ignore", invalid="ignore"):
        df["achievement"] = np.where(~is_pct & (df.target > 0), df.value / df.target, np.nan)

    # 基準値と乖離：直近3か月の水準 × 季節の型（過去1〜2年の「直前3か月→当月」の動きの平均）
    g = df.groupby(SERIES, sort=False)
    shn = {k: g["num"].shift(k) for k in range(1, 28)}
    shd = {k: g["den"].shift(k) for k in range(1, 28)}
    with np.errstate(divide="ignore", invalid="ignore"):
        def level(o):  # o か月前を基点とした直前3か月の水準
            n = shn[o + 1] + shn[o + 2] + shn[o + 3]
            d = shd[o + 1] + shd[o + 2] + shd[o + 3]
            return n / 3, n / d

        lvl_sum, lvl_ratio = level(0)
        seas_sum, seas_ratio = [], []
        for y in (12, 24):
            ls, lr = level(y)
            v_sum = shn[y]
            v_ratio = shn[y] / shd[y]
            seas_sum.append(np.where(ls > 0, v_sum / ls, np.nan))
            seas_ratio.append(v_ratio - lr)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # 過去データがない系列は NaN のまま
            s_sum = np.nanmean(np.vstack(seas_sum), axis=0)
            s_ratio = np.nanmean(np.vstack(seas_ratio), axis=0)
        baseline = np.where(is_ratio, lvl_ratio + s_ratio, lvl_sum * s_sum)
        baseline = np.where(np.isfinite(baseline), baseline, np.nan)
        res = np.where(is_ratio, df.value - baseline, np.where(baseline > 0, df.value / baseline - 1, np.nan))
    df["baseline"] = baseline
    df["residual"] = res
    df["seasonal_factor"] = np.where(is_ratio, s_ratio, s_sum)  # 過去の同月の「直前3か月→当月」の動き
    alert_scopes = {s["type"] for s in cfg.analysis["scopes"] if s.get("alerts")}
    glb = cfg.g
    win, minp = glb.get("scale_window", 24), glb.get("scale_min_points", 8)
    mask = df.scope_type.isin(alert_scopes)
    mad = pd.Series(np.nan, index=df.index)
    sub = df.loc[mask]
    mad.loc[mask] = sub.groupby(SERIES, sort=False)["residual"].transform(
        lambda s: s.shift(1).rolling(win, min_periods=minp).apply(_mad_scale, raw=True))
    min_scale = df.kpi_id.map(lambda k: ((cfg.kpi_thresholds(k) or {}).get("abnormal") or {}).get("min_scale", np.nan))
    min_scale = np.where(is_pct, min_scale / 100.0, min_scale)
    binom = df.kpi_id.map(lambda k: bool(kinfo[k].get("binomial")))
    b = np.clip(df.baseline.fillna(0.5), 0.001, 0.999)
    with np.errstate(divide="ignore", invalid="ignore"):
        se = np.where(binom & (df.den > 0), np.sqrt(b * (1 - b) / df.den), 0.0)
    scale = np.fmax(np.fmax(mad.to_numpy(), min_scale), se)
    df["scale"] = np.where(mad.notna(), scale, np.nan)
    df["z"] = df.residual / df.scale

    # 母数・目標未達の連続月数・連続悪化
    min_den = df.kpi_id.map(lambda k: (cfg.kpi_thresholds(k) or {}).get("min_denominator", 0) or 0)
    df["small_sample"] = is_ratio & (df.den < min_den)
    df["consecutive_miss"] = _consecutive(df, _miss_condition(df, cfg, is_pct, sign))
    bad_step = (df["diff"] * sign) < 0
    df["consecutive_worse"] = _consecutive(df, bad_step.fillna(False))

    df["note"] = ""
    _large_order_notes(df, big_orders, cfg)
    df["scope_label"] = scope_labels(df, masters)
    df["kpi_name"] = df.kpi_id.map(lambda k: kinfo[k]["name"])
    return df.drop(columns=["first_month"])


def _miss_condition(df, cfg, is_pct, sign) -> pd.Series:
    cond = pd.Series(False, index=df.index)
    for kid, idx in df.groupby("kpi_id").groups.items():
        th = ((cfg.kpi_thresholds(kid) or {}).get("target_miss")) or {}
        if not th:
            continue
        sub = df.loc[idx]
        if is_pct.loc[idx].iloc[0]:
            cond.loc[idx] = (sub.target_gap * sign.loc[idx] * 100) <= -th["p2"]
        else:
            cond.loc[idx] = sub.achievement < th["p2"] if sign.loc[idx].iloc[0] > 0 else sub.achievement > 2 - th["p2"]
    return cond.fillna(False)


def _consecutive(df: pd.DataFrame, cond: pd.Series) -> pd.Series:
    c = cond.astype(int)
    breaks = (c == 0).groupby([df[k] for k in SERIES]).cumsum()
    return c.groupby([df[k] for k in SERIES] + [breaks]).cumsum()


def _large_order_notes(df: pd.DataFrame, big: pd.DataFrame, cfg) -> None:
    thr = cfg.g.get("large_order_share", 0.10)
    if big is None or big.empty:
        return
    big = big.assign(share=big.amount / big.total)
    big = big[big.share >= thr]
    idx_map = df[(df.kpi_id == "order_amount") & (df.scope_type == "entity_product")]
    key = dict(zip(zip(idx_map.entity_code, idx_map.product_code, idx_map.month), idx_map.index))
    for r in big.itertuples():
        cur = key.get((r.entity_code, r.product_code, r.month))
        if cur is not None:
            df.loc[cur, "note"] = (f"当月に大口案件あり（受注番号 {r.order_id}、{r.amount:,.0f}円、構成比 {r.share:.0%}）。")
        nxt = key.get((r.entity_code, r.product_code, str(pd.Period(r.month, "M") + 1)))
        if nxt is not None:
            df.loc[nxt, "note"] = df.loc[nxt, "note"] + (
                f"前月（{r.month}）に大口案件あり（受注番号 {r.order_id}、{r.amount:,.0f}円、構成比 {r.share:.0%}）。前月比の減少はその反動の可能性。")


def label_maps(masters: dict) -> dict[str, dict]:
    m = masters
    return {
        "entity_code": dict(zip(m["entities"].entity_code, m["entities"].entity_name)) | {"UNK": "未分類"},
        "product_code": dict(zip(m["products"].product_code, m["products"].product_name)) | {"UNK": "未分類"},
        "section_code": dict(zip(m["org"].section_code, m["org"].section_name)) | {"UNK": "未分類"},
        "team_code": dict(zip(m["org"].team_code, m["org"].team_name)) | {"UNK": "未分類"},
        "division_code": dict(zip(m["org"].division_code, m["org"].division_name)) | {"UNK": "未分類"},
        "customer_id": dict(zip(m["customers"].customer_id, m["customers"].customer_name)) | {"UNK": "未分類"},
    }


def scope_labels(df: pd.DataFrame, masters: dict) -> pd.Series:
    maps = label_maps(masters)
    keys = df.scope_key.drop_duplicates()
    lab = {}
    for k in keys:
        if k == "ALL":
            lab[k] = "全社"
            continue
        parts = []
        for kv in k.split("|"):
            d, v = kv.split("=", 1)
            parts.append(maps.get(d, {}).get(v, v))
        lab[k] = " × ".join(parts)
    return df.scope_key.map(lab)


# ---------------------------------------------------------------- 分解

def decompose(fine: pd.DataFrame, kpi: dict, filt: dict, by: str, month: str, prev: str) -> pd.DataFrame:
    """内訳 by ごとに、前月→当月の変化への寄与を計算する。
    合計KPI: 寄与 = 各内訳の差。率KPI: 寄与 = 構成効果(mix) + 率効果(rate)（合計は全体の差に一致）。"""
    f = fine[(fine.kpi_id == kpi["id"]) & fine.month.isin([month, prev])]
    f = f.assign(num=f.num.astype(float), den=f.den.astype(float))
    for d, v in filt.items():
        f = f[f[d] == v]
    p = f.pivot_table(index=by, columns="month", values=["num", "den"], aggfunc="sum", fill_value=0.0)
    out = pd.DataFrame(index=p.index)
    for c in ("num", "den"):
        for m, tag in ((prev, "0"), (month, "1")):
            out[c + tag] = p[(c, m)] if (c, m) in p.columns else 0.0
    if kpi["type"] == "sum":
        out["value0"], out["value1"] = out.num0, out.num1
        out["contribution"] = out.num1 - out.num0
        out["mix"], out["rate"] = np.nan, np.nan
    else:
        D0, D1 = out.den0.sum(), out.den1.sum()
        R0 = out.num0.sum() / D0 if D0 else np.nan
        with np.errstate(divide="ignore", invalid="ignore"):
            r0 = np.where(out.den0 > 0, out.num0 / out.den0, R0)
            r1 = np.where(out.den1 > 0, out.num1 / out.den1, r0)
            w0 = out.den0 / D0 if D0 else 0.0
            w1 = out.den1 / D1 if D1 else 0.0
        out["value0"] = np.where(out.den0 > 0, r0, np.nan)
        out["value1"] = np.where(out.den1 > 0, r1, np.nan)
        out["mix"] = (w1 - w0) * (r0 - R0)
        out["rate"] = w1 * (r1 - r0)
        out["contribution"] = out.mix + out.rate
    total = out.contribution.sum()
    out["share"] = out.contribution / total if total else np.nan
    return out.reset_index().rename(columns={by: "segment"}).sort_values("contribution")


def count_price(fine: pd.DataFrame, amount_kpi: str, count_kpi: str, filt: dict, month: str, prev: str) -> dict:
    def tot(k, m):
        f = fine[(fine.kpi_id == k) & (fine.month == m)]
        for d, v in filt.items():
            f = f[f[d] == v]
        return float(f.num.sum())
    a0, a1, n0, n1 = tot(amount_kpi, prev), tot(amount_kpi, month), tot(count_kpi, prev), tot(count_kpi, month)
    if not (n0 and n1):
        return {}
    p0, p1 = a0 / n0, a1 / n1
    return {"count_effect": (n1 - n0) * p0, "price_effect": n1 * (p1 - p0), "count0": n0, "count1": n1,
            "price0": p0, "price1": p1, "total": a1 - a0}
