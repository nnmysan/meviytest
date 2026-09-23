"""ダミーの「業務の実態（world）」を生成する。

シナリオの効果（需要減・粗利率低下・納期遅延など）はここで注入する。
乱数は (seed, 月, 法人, 商品) ごとに独立したストリームを使うため、
ある内訳への効果注入が他の内訳の値を変えない。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import params as P

DAY = np.timedelta64(1, "D")


def month_list(start: str, end: str) -> list[str]:
    return [str(p) for p in pd.period_range(start, end, freq="M")]


def business_days(month: str) -> np.ndarray:
    p = pd.Period(month, "M")
    return pd.date_range(p.start_time, p.end_time.normalize(), freq="B").values


def _matches(eff: dict, month: str | None, entity: str | None, product: str | None) -> bool:
    if month is not None and "months" in eff and month not in eff["months"]:
        return False
    if eff.get("entity") not in (None, entity):
        return False
    if eff.get("product") not in (None, product):
        return False
    return True


def _effects(effects: list[dict], etype: str) -> list[dict]:
    return [e for e in effects if e["type"] == etype]


def gen_customers(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng([seed, 10_000])
    rows = []
    idx = 1
    for e, info in P.ENTITIES.items():
        for _ in range(info["customers"]):
            size = str(rng.choice(list(P.SIZES), p=list(P.SIZES.values())))
            industry = str(rng.choice(P.INDUSTRIES, p=P.INDUSTRY_WEIGHTS))
            weight = P.SIZE_WEIGHT[size] * rng.lognormal(0, 0.5)
            rows.append(dict(customer_id=f"C{idx:04d}", customer_name=f"ダミー顧客{idx:03d}", entity_code=e,
                             industry=industry, size_class=size, weight=weight))
            idx += 1
    return pd.DataFrame(rows)


def gen_fx(seed: int, months: list[str], effects: list[dict]) -> pd.DataFrame:
    rows = []
    for ci, (cur, budget) in enumerate(P.BUDGET_RATE.items()):
        rng = np.random.default_rng([seed, 20_000 + ci])
        walk = np.clip(np.cumsum(rng.normal(0, P.FX_MONTHLY_SD, len(months))), -P.FX_CLIP, P.FX_CLIP)
        actual = budget * (1 + walk) if cur != "JPY" else np.full(len(months), 1.0)
        for eff in _effects(effects, "fx_shock"):
            if eff["currency"] == cur:
                i = months.index(eff["month"])
                actual[i] = actual[i - 1] * (1 + eff["change_vs_prev"])
        for m, a in zip(months, actual):
            rows.append(dict(month=m, currency=cur, budget_rate=budget, actual_rate=round(float(a), 6)))
    return pd.DataFrame(rows)


def section_of(entity: str, product: str) -> str:
    team = P.PRODUCTS[product]["team"][0]  # T11 など
    return f"SEC{team[1:]}{1 if entity == 'JP' else 2}"


def gen_cell(seed, t, month, ei, e, pi, p, cust_e: pd.DataFrame, effects, as_of) -> dict:
    rng = np.random.default_rng([seed, t, ei, pi])
    prm = P.PRODUCTS[p]
    cur = P.ENTITIES[e]["currency"]
    rate, dec = P.BUDGET_RATE[cur], P.DECIMALS[cur]
    yymm = month[2:4] + month[5:7]

    lam = P.BASE_ORDERS[e][p] * (1 + P.GROWTH_PER_YEAR) ** (t / 12) * P.SEASON.get(e, {}).get(int(month[5:]), 1.0)
    for eff in _effects(effects, "demand"):
        if _matches(eff, month, e, p):
            lam *= eff["factor"]
    n = int(rng.poisson(lam))
    override = next((x for x in _effects(effects, "volume_override") if _matches(x, month, e, p)), None)
    if override:
        n = override["orders"]
    for eff in _effects(effects, "product_start"):
        if _matches(eff, None, e, p) and month < eff["start_month"]:
            n = 0

    bdays = business_days(month)
    weights = cust_e["weight"].to_numpy() / cust_e["weight"].sum()
    cust_ids = cust_e["customer_id"].to_numpy()
    cust_size = dict(zip(cust_e["customer_id"], cust_e["size_class"]))

    order_day = rng.choice(bdays, n)
    cust = rng.choice(cust_ids, n, p=weights)
    n_lines = 1 + np.minimum(rng.poisson(P.LINES_POISSON, n), P.MAX_EXTRA_LINES)
    if override and "lines" in override:
        n_lines[:] = override["lines"]
    cancel = rng.random(n) < P.CANCEL_RATE
    qlag = rng.integers(0, 11, n)
    upd_min = rng.integers(0, 600, n)
    L = int(n_lines.sum())
    oi = np.repeat(np.arange(n), n_lines)
    line_no = np.arange(L) - np.repeat(np.cumsum(n_lines) - n_lines, n_lines) + 1
    mu = math.log(prm["line_mean_jpy"]) - P.LINE_SIGMA ** 2 / 2
    amt_jpy = rng.lognormal(mu, P.LINE_SIGMA, L)
    mnoise = rng.normal(0, P.MARGIN_NOISE, L)
    lead = prm["lead"] + rng.integers(-2, 4, L)
    u_late = rng.random(L)
    late_d = rng.integers(1, 11, L)
    early_d = rng.integers(0, 4, L)
    u_def = rng.random(L)
    found_lag = rng.integers(1, 31, L)
    def_cat = rng.integers(0, len(P.DEFECT_CATEGORIES), L)
    p_conv = prm["conv"]
    n_u = int(rng.poisson(n * (1 - p_conv) / p_conv)) if n > 0 else 0
    u_day = rng.choice(bdays, n_u)
    u_cust = rng.choice(cust_ids, n_u, p=weights)
    u_amt = rng.lognormal(mu, P.LINE_SIGMA, n_u) * 2.5

    order_ids = np.array([f"SO{e}{yymm}{pi}{i + 1:04d}" for i in range(n)], dtype=object)
    quote_ids = np.array([f"Q{e}{yymm}{pi}{i + 1:04d}" for i in range(n)], dtype=object)

    local = np.round(amt_jpy / rate, dec)
    size_adj = np.array([P.SIZE_MARGIN[cust_size[c]] for c in cust], dtype=float)[oi] if n else np.zeros(0)
    shift = np.zeros(L)
    for eff in _effects(effects, "margin_shift"):
        if _matches(eff, month, e, p):
            shift += eff["delta"]
    margin = np.clip(prm["margin"] + size_adj + mnoise + shift, 0.02, 0.8)
    cost = np.round(local * (1 - margin), dec)

    order_date = order_day[oi]
    upd = (order_day + DAY + np.timedelta64(9, "h") + upd_min.astype("timedelta64[m]"))[oi]
    orders = pd.DataFrame({
        "order_id": order_ids[oi], "line_no": line_no, "order_date": order_date, "quote_id": quote_ids[oi],
        "customer_id": cust[oi], "product_code": p, "entity_code": e, "section_code": section_of(e, p),
        "amount_local": local, "cost_local": cost, "currency": cur,
        "status": np.where(cancel[oi], "取消", "受注"), "updated_at": upd,
    })

    # 見積：受注に至った見積（受注日の0〜10日前）＋受注に至らなかった見積
    q_amt = pd.Series(local).groupby(oi).sum().reindex(range(n), fill_value=0).to_numpy()
    quotes = pd.DataFrame({
        "quote_id": np.concatenate([quote_ids, np.array([f"Q{e}{yymm}{pi}U{i + 1:04d}" for i in range(n_u)], dtype=object)]),
        "quote_date": np.concatenate([order_day - qlag * DAY, u_day]),
        "customer_id": np.concatenate([cust, u_cust]),
        "product_code": p, "entity_code": e, "section_code": section_of(e, p),
        "amount_local": np.concatenate([np.round(q_amt, dec), np.round(u_amt / rate, dec)]),
        "currency": cur,
    })
    quotes["updated_at"] = quotes["quote_date"] + np.timedelta64(17, "h")

    # 納期・出荷
    due = order_date + lead * DAY
    due_month = due.astype("datetime64[M]").astype(str)
    p_late = np.full(L, P.LATE_PROB[e] + (P.LATE_PROB_SWD_ADD if p == "SWD" else 0.0))
    for eff in _effects(effects, "late_prob"):
        if _matches(eff, None, e, p):
            p_late[np.isin(due_month, eff["months"])] = eff["value"]
    is_late = u_late < p_late
    ship = np.where(is_late, due + late_d * DAY, np.maximum(order_date, due - early_d * DAY))
    as_of64 = np.datetime64(as_of)
    live = ~cancel[oi]
    ship_known = ship <= as_of64
    shipments = pd.DataFrame({
        "order_id": order_ids[oi], "line_no": line_no, "due_date": due,
        "ship_date": np.where(ship_known, ship, np.datetime64("NaT")),
        "entity_code": e,
    })[live].copy()
    shipments["updated_at"] = shipments["ship_date"].fillna(shipments["due_date"]) + pd.Timedelta(hours=18)

    # 品質不良：出荷済み明細に対して発生
    ship_month = ship.astype("datetime64[M]").astype(str)
    q_def = np.full(L, prm["defect"])
    for eff in _effects(effects, "defect_prob"):
        if _matches(eff, None, e, p):
            q_def[np.isin(ship_month, eff["months"])] = eff["value"]
    found = ship + found_lag * DAY
    has_def = live & ship_known & (u_def < q_def) & (found <= as_of64)
    defects = pd.DataFrame({
        "defect_id": [f"D{o}-{ln}" for o, ln in zip(order_ids[oi][has_def], line_no[has_def])],
        "order_id": order_ids[oi][has_def], "line_no": line_no[has_def], "found_date": found[has_def],
        "category": np.array(P.DEFECT_CATEGORIES, dtype=object)[def_cat[has_def]], "entity_code": e,
    })
    defects["updated_at"] = defects["found_date"] + pd.Timedelta(hours=12)
    return {"orders": orders, "quotes": quotes, "shipments": shipments, "defects": defects}


def add_large_order(world: dict, eff: dict, customers: pd.DataFrame, seed: int, facts: dict) -> None:
    month, e, p = eff["month"], eff["entity"], eff["product"]
    o = world["orders"]
    cur = P.ENTITIES[e]["currency"]
    rate, dec = P.BUDGET_RATE[cur], P.DECIMALS[cur]
    cell = o[(o.entity_code == e) & (o.product_code == p) & (o.order_date.dt.strftime("%Y-%m") == month) & (o.status != "取消")]
    s = float((cell.amount_local * rate).sum())
    x = eff["share"] / (1 - eff["share"]) * s
    local = round(x / rate, dec)
    cust = customers[customers.entity_code == e].sort_values("weight", ascending=False).iloc[0]
    bd = business_days(month)
    day = pd.Timestamp(bd[len(bd) // 2])
    pi = list(P.PRODUCTS).index(p)
    oid = f"SO{e}{month[2:4]}{month[5:7]}{pi}L001"
    qid = f"Q{e}{month[2:4]}{month[5:7]}{pi}L001"
    due = day + pd.Timedelta(days=P.PRODUCTS[p]["lead"])
    world["orders"] = pd.concat([o, pd.DataFrame([{
        "order_id": oid, "line_no": 1, "order_date": day, "quote_id": qid, "customer_id": cust.customer_id,
        "product_code": p, "entity_code": e, "section_code": section_of(e, p), "amount_local": local,
        "cost_local": round(local * (1 - P.PRODUCTS[p]["margin"]), dec), "currency": cur, "status": "受注",
        "updated_at": day + pd.Timedelta(days=1, hours=10)}])], ignore_index=True)
    world["quotes"] = pd.concat([world["quotes"], pd.DataFrame([{
        "quote_id": qid, "quote_date": day - pd.Timedelta(days=5), "customer_id": cust.customer_id, "product_code": p,
        "entity_code": e, "section_code": section_of(e, p), "amount_local": local, "currency": cur,
        "updated_at": day - pd.Timedelta(days=5) + pd.Timedelta(hours=17)}])], ignore_index=True)
    world["shipments"] = pd.concat([world["shipments"], pd.DataFrame([{
        "order_id": oid, "line_no": 1, "due_date": due, "ship_date": due - pd.Timedelta(days=1), "entity_code": e,
        "updated_at": due - pd.Timedelta(days=1) + pd.Timedelta(hours=18)}])], ignore_index=True)
    facts["large_order"] = {"order_id": oid, "month": month, "entity": e, "product": p,
                            "amount_jpy": local * rate, "segment_amount_before_jpy": s,
                            "share": local * rate / (s + local * rate)}


def force_defects(world: dict, eff: dict, facts: dict) -> None:
    o, s, d = world["orders"], world["shipments"], world["defects"]
    lines = s.merge(o[["order_id", "line_no", "product_code", "status"]], on=["order_id", "line_no"])
    lines = lines[(lines.entity_code == eff["entity"]) & (lines.product_code == eff["product"]) & (lines.status != "取消")
                  & (lines.ship_date.dt.strftime("%Y-%m") == eff["month"])]
    have = set(zip(d.order_id, d.line_no))
    keep = np.array([(a, b) not in have for a, b in zip(lines.order_id, lines.line_no)], dtype=bool)
    lines = lines[keep].sort_values(["order_id", "line_no"])
    add = lines.head(eff["count"])
    new = pd.DataFrame({
        "defect_id": [f"D{a}-{b}" for a, b in zip(add.order_id, add.line_no)], "order_id": add.order_id.values,
        "line_no": add.line_no.values, "found_date": (add.ship_date + pd.Timedelta(days=1)).values,
        "category": "寸法不良", "entity_code": eff["entity"]})
    new["updated_at"] = new["found_date"] + pd.Timedelta(hours=12)
    world["defects"] = pd.concat([d, new], ignore_index=True)
    facts["forced_defects"] = {"count": len(new), **{k: eff[k] for k in ("month", "entity", "product")}}


def apply_correction(world: dict, eff: dict, seed: int, facts: dict) -> set[str]:
    """過去月の受注を後から取消に変更する（返品・キャンセルの計上漏れ修正を模擬）。変更後を world に反映する。"""
    o = world["orders"]
    e, month = eff["entity"], eff["month"]
    rate = P.BUDGET_RATE[P.ENTITIES[e]["currency"]]
    tgt = o[(o.entity_code == e) & (o.order_date.dt.strftime("%Y-%m") == month) & (o.status != "取消")]
    per_order = (tgt.amount_local * rate).groupby(tgt.order_id).sum()
    total = float(per_order.sum())
    rng = np.random.default_rng([seed, 40_000])
    ids = per_order.index.to_numpy()[rng.permutation(len(per_order))]
    chosen, acc = [], 0.0
    for oid in ids:
        if acc >= eff["cancel_share"] * total:
            break
        chosen.append(oid)
        acc += float(per_order[oid])
    mask = o.order_id.isin(chosen)
    o.loc[mask, "status"] = "取消"
    o.loc[mask, "updated_at"] = pd.Timestamp(eff["updated_at"])
    facts["correction"] = {"entity": e, "month": month, "orders": len(chosen), "cancelled_amount_jpy": acc,
                           "amount_before_jpy": total, "amount_after_jpy": total - acc, "rel_change": -acc / total}
    return set(chosen)


def build_world(seed: int, effects: list[dict], as_of: str) -> tuple[dict, pd.DataFrame, pd.DataFrame, dict]:
    months = month_list(P.START_MONTH, P.END_MONTH)
    customers = gen_customers(seed)
    fx = gen_fx(seed, months, effects)
    parts: dict[str, list] = {k: [] for k in ("orders", "quotes", "shipments", "defects")}
    for t, month in enumerate(months):
        for ei, e in enumerate(P.ENTITIES):
            cust_e = customers[customers.entity_code == e]
            for pi, p in enumerate(P.PRODUCTS):
                cell = gen_cell(seed, t, month, ei, e, pi, p, cust_e, effects, as_of)
                for k, v in cell.items():
                    if len(v):
                        parts[k].append(v)
    world = {k: pd.concat(v, ignore_index=True) for k, v in parts.items()}
    # 分析開始月より前の見積は出力対象外（抽出期間外）
    world["quotes"] = world["quotes"][world["quotes"].quote_date >= pd.Timestamp(P.START_MONTH + "-01")].reset_index(drop=True)
    facts: dict = {}
    for eff in _effects(effects, "large_order"):
        add_large_order(world, eff, customers, seed, facts)
    for eff in _effects(effects, "force_defects"):
        force_defects(world, eff, facts)
    for k in world:
        world[k] = world[k].sort_values(world[k].columns[0], kind="stable").reset_index(drop=True)
    return world, customers, fx, facts
