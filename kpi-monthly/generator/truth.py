"""正解値（truth）の計算。

パイプライン（DuckDB/SQL）とは別に、生成した業務データから直接 pandas で KPI を計算する。
パイプラインの出力をそのまま正解とする「自己参照」の検証を避けるため、意図的に独立した実装にしている。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import params as P


def _rate(df: pd.DataFrame, month_col: str, fx: pd.DataFrame) -> pd.Series:
    m = df[month_col].dt.strftime("%Y-%m")
    key = pd.DataFrame({"month": m, "currency": df["currency"]})
    return key.merge(fx[["month", "currency", "budget_rate"]], on=["month", "currency"], how="left")["budget_rate"].to_numpy()


def kpi_frames(world: dict, fx: pd.DataFrame, as_of: str) -> dict[str, pd.DataFrame]:
    """KPIごとに month, entity_code, product_code, num, den の明細を作る。"""
    o = world["orders"].copy()
    o["rate"] = _rate(o, "order_date", fx)
    live = o[o.status != "取消"].copy()
    live["month"] = live.order_date.dt.strftime("%Y-%m")
    live["amt"] = live.amount_local * live.rate
    live["gp"] = (live.amount_local - live.cost_local) * live.rate
    out = {
        "order_amount": live.assign(num=live.amt, den=np.nan),
        "order_count": live.drop_duplicates("order_id").assign(num=1.0, den=np.nan),
        "gross_margin": live.assign(num=live.gp, den=live.amt),
    }
    q = world["quotes"].copy()
    first = live.groupby("quote_id").order_date.min()
    lag = (q.quote_id.map(first) - q.quote_date).dt.days
    q["num"] = ((lag >= 0) & (lag <= 30)).astype(float)
    q["den"] = 1.0
    q["month"] = q.quote_date.dt.strftime("%Y-%m")
    out["quote_conversion"] = q

    s = world["shipments"].merge(live[["order_id", "line_no", "product_code"]], on=["order_id", "line_no"])
    due = s[s.due_date <= pd.Timestamp(as_of)].copy()
    due["month"] = due.due_date.dt.strftime("%Y-%m")
    due["num"] = (due.ship_date.notna() & (due.ship_date <= due.due_date)).astype(float)
    due["den"] = 1.0
    out["on_time_delivery"] = due

    shp = s[s.ship_date.notna()].copy()
    dc = world["defects"].groupby(["order_id", "line_no"]).size().rename("dc").reset_index()
    shp = shp.merge(dc, on=["order_id", "line_no"], how="left")
    shp["month"] = shp.ship_date.dt.strftime("%Y-%m")
    shp["num"] = shp.dc.fillna(0).astype(float)
    shp["den"] = 1.0
    out["defect_rate"] = shp
    return out


RATIO = {"quote_conversion", "gross_margin", "on_time_delivery", "defect_rate"}


def compute_truth(world: dict, fx: pd.DataFrame, as_of: str, months: list[str]) -> pd.DataFrame:
    rows = []
    for kpi, df in kpi_frames(world, fx, as_of).items():
        df = df[df.month.isin(months)]
        for scope_type, dims in (("total", []), ("entity", ["entity_code"]), ("product", ["product_code"]),
                                 ("entity_product", ["entity_code", "product_code"])):
            g = df.groupby(["month"] + dims)[["num", "den"]].sum(min_count=1).reset_index()
            for r in g.itertuples(index=False):
                rd = r._asdict()
                key = "|".join(f"{d}={rd[d]}" for d in dims) or "ALL"
                value = rd["num"] / rd["den"] if kpi in RATIO else rd["num"]
                if kpi in RATIO and not rd["den"]:
                    value = np.nan
                rows.append(dict(kpi_id=kpi, scope_type=scope_type, scope_key=key, month=rd["month"],
                                 num=rd["num"], den=rd["den"], value=value))
    return pd.DataFrame(rows)


def _expected_lines() -> float:
    from math import exp, factorial
    lam = P.LINES_POISSON
    ev = sum(min(k, P.MAX_EXTRA_LINES) * exp(-lam) * lam ** k / factorial(k) for k in range(0, 60))
    return 1 + ev


def build_targets(months: list[str], volume_scale: float = 1.0) -> pd.DataFrame:
    """計画値（目標）のダミー。シナリオ効果は入れない＝期初に決めた「計画」として固定。"""
    e_lines = _expected_lines()
    size_share = {s: P.SIZES[s] * P.SIZE_WEIGHT[s] for s in P.SIZES}
    tot = sum(size_share.values())
    size_adj = sum(size_share[s] / tot * P.SIZE_MARGIN[s] for s in P.SIZES)
    rows = []
    for m in months:
        t = (pd.Period(m, "M") - pd.Period(P.START_MONTH, "M")).n  # 成長の起点は生成器と同じ
        cell = {}
        for e in P.ENTITIES:
            for p, prm in P.PRODUCTS.items():
                n = P.BASE_ORDERS[e][p] * (1 + P.GROWTH_PER_YEAR) ** (t / 12) * P.SEASON.get(e, {}).get(int(m[5:]), 1.0)
                n *= volume_scale
                live = n * (1 - P.CANCEL_RATE)
                amt = live * e_lines * prm["line_mean_jpy"]
                cell[(e, p)] = dict(count=live, amount=amt, gp=amt * (prm["margin"] + size_adj),
                                    quotes=n / prm["conv"], conv=n * (1 - P.CANCEL_RATE))
        scopes = [("total", "ALL", lambda e, p: True)]
        scopes += [("entity", f"entity_code={e0}", (lambda e0: lambda e, p: e == e0)(e0)) for e0 in P.ENTITIES]
        scopes += [("product", f"product_code={p0}", (lambda p0: lambda e, p: p == p0)(p0)) for p0 in P.PRODUCTS]
        for st, key, f in scopes:
            sel = [v for (e, p), v in cell.items() if f(e, p)]
            amount = sum(v["amount"] for v in sel)
            count = sum(v["count"] for v in sel)
            gm = sum(v["gp"] for v in sel) / amount
            conv = sum(v["conv"] for v in sel) / sum(v["quotes"] for v in sel)
            if st == "entity":
                otd = 0.96 if key.endswith("JP") else 0.93
            else:
                otd = 0.95
            dr = 0.018 if key.endswith("SWD") else 0.010
            rows += [
                dict(kpi_id="order_amount", month=m, scope_type=st, scope_key=key, target=round(amount * 0.98, -5)),
                dict(kpi_id="order_count", month=m, scope_type=st, scope_key=key, target=round(count * 0.98)),
                dict(kpi_id="gross_margin", month=m, scope_type=st, scope_key=key, target=round(gm - 0.003, 3)),
                dict(kpi_id="quote_conversion", month=m, scope_type=st, scope_key=key, target=round(conv - 0.01, 3)),
                dict(kpi_id="on_time_delivery", month=m, scope_type=st, scope_key=key, target=otd),
                dict(kpi_id="defect_rate", month=m, scope_type=st, scope_key=key, target=dr),
            ]
    return pd.DataFrame(rows)


def supplier_truth(world: dict, capacity: pd.DataFrame, months: list[str]) -> pd.DataFrame:
    """サプライヤ×能力区分の月次充足率（出荷日ベース）。パイプラインとは独立に計算する。
    充足率 = 出荷した明細数（数量）÷（日あたり供給能力 × 当月の平日数）"""
    o = world["orders"]
    s = world["shipments"].merge(o[o.status != "取消"], on=["order_id", "line_no"], suffixes=("", "_o"))
    s = s[s.ship_date.notna()].copy()
    s["month"] = s.ship_date.dt.strftime("%Y-%m")
    keys = ["supplier_code", "material", "part_size", "surface", "heat"]
    rows = []
    for m in months:
        p = pd.Period(m, "M")
        wd = int(np.busday_count(p.start_time.date(), (p.end_time + pd.Timedelta(days=1)).date()))
        g = s[s.month == m].groupby(keys).agg(lines=("line_no", "size"), qty=("quantity", "sum")).reset_index()
        c = capacity.merge(g, on=keys, how="left").fillna({"lines": 0, "qty": 0})
        c["month"] = m
        c["load_count"] = c.lines / (c.cap_count_day * wd)
        c["load_qty"] = c.qty / (c.cap_qty_day * wd)
        rows.append(c)
        sup = c.groupby("supplier_code").agg(lines=("lines", "sum"), qty=("qty", "sum"), cc=("cap_count_day", "sum"),
                                             cq=("cap_qty_day", "sum")).reset_index()
        sup["month"] = m
        sup["load_count"] = sup.lines / (sup.cc * wd)
        sup["load_qty"] = sup.qty / (sup.cq * wd)
        rows.append(sup.assign(material="*", part_size="*", surface="*", heat="*"))
    out = pd.concat(rows, ignore_index=True)
    return out[["month"] + keys + ["lines", "qty", "load_count", "load_qty"]]
