"""中核データ（DuckDB のビュー）と、KPI定義（YAML）からの集計。

KPIは「分子・分母」を最も細かい粒度（月×法人×商品×課×顧客）で持ち、
上位の内訳は分子・分母を合計してから割る（率の平均は取らない）。
"""
from __future__ import annotations

import duckdb
import pandas as pd

from .dq import DQ

GRAIN = ["month", "entity_code", "product_code", "section_code", "customer_id"]

VIEWS_SQL = """
CREATE OR REPLACE VIEW v_order_lines AS
SELECT o.*, strftime(o.order_date, '%Y-%m') AS month,
       o.amount_local * f.budget_rate AS amount_jpy,
       o.amount_local * f.actual_rate AS amount_jpy_actual,
       (o.amount_local - o.cost_local) * f.budget_rate AS gross_profit_jpy
FROM orders o
LEFT JOIN fx_rates f ON f.currency = o.currency AND f.month = strftime(o.order_date, '%Y-%m');

CREATE OR REPLACE VIEW v_quotes AS
WITH first_order AS (
  SELECT quote_id, MIN(order_date) AS first_order_date
  FROM orders WHERE status <> '取消' AND quote_id IS NOT NULL GROUP BY quote_id)
SELECT q.*, strftime(q.quote_date, '%Y-%m') AS month,
       CASE WHEN date_diff('day', q.quote_date, fo.first_order_date) BETWEEN 0 AND 30 THEN 1 ELSE 0 END AS converted_30d
FROM quotes q LEFT JOIN first_order fo USING (quote_id);

CREATE OR REPLACE VIEW v_due_lines AS
SELECT s.order_id, s.line_no, s.due_date, s.ship_date, o.entity_code, o.product_code, o.section_code, o.customer_id,
       strftime(s.due_date, '%Y-%m') AS month,
       CASE WHEN s.ship_date IS NOT NULL AND s.ship_date <= s.due_date THEN 1 ELSE 0 END AS on_time
FROM shipments s JOIN orders o USING (order_id, line_no)
WHERE o.status <> '取消' AND s.due_date <= DATE '{as_of}';

CREATE OR REPLACE VIEW v_shipped_lines AS
SELECT s.order_id, s.line_no, s.ship_date, o.entity_code, o.product_code, o.section_code, o.customer_id,
       strftime(s.ship_date, '%Y-%m') AS month, COALESCE(d.cnt, 0) AS defect_count
FROM shipments s JOIN orders o USING (order_id, line_no)
LEFT JOIN (SELECT order_id, line_no, COUNT(*) AS cnt FROM defects GROUP BY 1, 2) d USING (order_id, line_no)
WHERE s.ship_date IS NOT NULL AND o.status <> '取消';
"""


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    return df[[c for c in df.columns if not c.startswith("_")]]


def build_db(data: dict, masters: dict, as_of: str, dq: DQ) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads TO 1")  # 並列集計は浮動小数の加算順が変わるため、再実行で1円未満の差が出ないよう単一スレッドに固定
    for name, df in data.items():
        con.register(f"{name}_df", _clean(df))
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM {name}_df")
    con.register("fx_df", masters["fx_rates"])
    con.execute("CREATE TABLE fx_rates AS SELECT * FROM fx_df")
    con.execute(VIEWS_SQL.format(as_of=as_of))

    # 為替レートの欠落は金額を誤るため取込停止
    miss = con.execute("""
        SELECT month, currency, COUNT(*) n FROM v_order_lines WHERE amount_jpy IS NULL GROUP BY 1, 2 ORDER BY 1, 2""").df()
    for r in miss.itertuples():
        dq.add("fx_missing", "P1", f"{r.month} の {r.currency} の為替レートがありません（{r.n} 行）", month=r.month, blocking=True)
    # 受注に紐付かない出荷・不良
    for src in ("shipments", "defects"):
        n = con.execute(f"SELECT COUNT(*) FROM {src} s ANTI JOIN orders o USING (order_id, line_no)").fetchone()[0]
        if n:
            dq.add("orphan_rows", "P3", f"{src} に受注明細が見つからない行が {n} 行（受注ファイル未着・抽出範囲の違いの可能性）。集計対象外", source=src, count=int(n))
    return con


def compute_fine(con: duckdb.DuckDBPyConnection, kpis: list[dict]) -> pd.DataFrame:
    parts = []
    for k in kpis:
        den = k.get("denominator") or "NULL"
        extras = "".join(f", {expr} AS {name}" for name, expr in (k.get("extra_measures") or {}).items())
        where = f"WHERE {k['where']}" if k.get("where") else ""
        sql = f"""
            SELECT '{k['id']}' AS kpi_id, {', '.join(GRAIN)},
                   CAST({k['numerator']} AS DOUBLE) AS num, CAST({den} AS DOUBLE) AS den {extras}
            FROM {k['view']} {where}
            GROUP BY ALL ORDER BY ALL"""
        parts.append(con.execute(sql).df())
    fine = pd.concat(parts, ignore_index=True)
    if "actual_fx" not in fine.columns:
        fine["actual_fx"] = float("nan")
    return fine


def add_dims(fine: pd.DataFrame, masters: dict) -> pd.DataFrame:
    org = masters["org"][["section_code", "team_code", "division_code"]]
    cust = masters["customers"][["customer_id", "industry", "size_class"]]
    out = fine.merge(org, on="section_code", how="left").merge(cust, on="customer_id", how="left")
    for c in ("team_code", "division_code", "industry", "size_class"):
        out[c] = out[c].fillna("UNK")
    out["customer_id"] = out["customer_id"].fillna("UNK")
    return out


def largest_orders(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """法人×商品×月ごとの最大受注（大口案件の検知用）。"""
    return con.execute("""
        WITH o AS (
          SELECT month, entity_code, product_code, order_id, customer_id, SUM(amount_jpy) amt
          FROM v_order_lines WHERE status <> '取消' GROUP BY ALL),
        t AS (SELECT month, entity_code, product_code, SUM(amt) total FROM o GROUP BY ALL)
        SELECT o.month, o.entity_code, o.product_code, arg_max(o.order_id, o.amt) AS order_id,
               arg_max(o.customer_id, o.amt) AS customer_id, MAX(o.amt) AS amount, ANY_VALUE(t.total) AS total
        FROM o JOIN t USING (month, entity_code, product_code) GROUP BY ALL""").df()
