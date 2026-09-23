"""ダミーデータ生成の基本パラメータ。

ここにある値はすべて検証用の架空の設定であり、会社の実績・計画・組織を表すものではない。
"""

START_MONTH = "2023-09"
END_MONTH = "2026-08"  # 分析対象月（最新の締め月）
AS_OF_BATCH1 = "2026-09-08"  # 1回目の取込日（翌月6営業日目）
AS_OF_BATCH2 = "2026-09-15"  # 遅延・修正ファイル到着後の再取込日

ENTITIES = {
    "JP": {"name": "日本", "currency": "JPY", "customers": 110},
    "KR": {"name": "韓国", "currency": "KRW", "customers": 30},
    "EU": {"name": "欧州", "currency": "EUR", "customers": 25},
    "CN": {"name": "中国", "currency": "CNY", "customers": 35},
}

# 商品・サービス軸。組織（事業部・チーム）への対応は仮置き。
PRODUCTS = {
    "MSQ": {"name": "切削角物", "line_mean_jpy": 40_000, "margin": 0.36, "conv": 0.42, "lead": 8, "defect": 0.006,
            "division": ("D1", "切削事業部"), "team": ("T11", "角物チーム")},
    "MRD": {"name": "切削丸物", "line_mean_jpy": 30_000, "margin": 0.33, "conv": 0.40, "lead": 7, "defect": 0.006,
            "division": ("D1", "切削事業部"), "team": ("T12", "丸物チーム")},
    "SHM": {"name": "板金", "line_mean_jpy": 55_000, "margin": 0.29, "conv": 0.38, "lead": 10, "defect": 0.008,
            "division": ("D2", "板金事業部"), "team": ("T21", "板金チーム")},
    "SWD": {"name": "板金溶接", "line_mean_jpy": 140_000, "margin": 0.25, "conv": 0.33, "lead": 14, "defect": 0.015,
            "division": ("D2", "板金事業部"), "team": ("T22", "溶接チーム")},
}

# 月平均の受注件数（成長・季節性をかける前）
BASE_ORDERS = {
    "JP": {"MSQ": 260, "MRD": 170, "SHM": 150, "SWD": 60},
    "KR": {"MSQ": 60, "MRD": 40, "SHM": 35, "SWD": 16},
    "EU": {"MSQ": 50, "MRD": 35, "SHM": 30, "SWD": 40},
    "CN": {"MSQ": 80, "MRD": 60, "SHM": 50, "SWD": 24},
}

GROWTH_PER_YEAR = 0.06

# 法人ごとの季節係数（月 -> 係数）。欧州の8月休暇、中国の春節などを模したもの。
SEASON = {
    "JP": {1: 0.92, 3: 1.08, 5: 0.92, 8: 0.90, 12: 0.97},
    "KR": {2: 0.92, 9: 0.93},
    "EU": {8: 0.70, 12: 0.85},
    "CN": {2: 0.70, 10: 0.92},
}

LATE_PROB = {"JP": 0.03, "KR": 0.04, "EU": 0.04, "CN": 0.05}
LATE_PROB_SWD_ADD = 0.01

BUDGET_RATE = {"JPY": 1.0, "KRW": 0.11, "EUR": 160.0, "CNY": 20.5}  # 円 / 現地通貨1単位（仮）
DECIMALS = {"JPY": 0, "KRW": 0, "EUR": 2, "CNY": 2}
FX_MONTHLY_SD = 0.012
FX_CLIP = 0.08

INDUSTRIES = ["自動車", "半導体製造装置", "産業用ロボット", "医療機器", "食品機械"]
INDUSTRY_WEIGHTS = [0.30, 0.25, 0.20, 0.15, 0.10]
SIZES = {"大": 0.2, "中": 0.4, "小": 0.4}
SIZE_WEIGHT = {"大": 5.0, "中": 2.0, "小": 1.0}
SIZE_MARGIN = {"大": -0.02, "中": 0.0, "小": 0.02}

CANCEL_RATE = 0.02
LINE_SIGMA = 0.7
MARGIN_NOISE = 0.04
LINES_POISSON = 1.5
MAX_EXTRA_LINES = 5

# 「自社システム」CSV の列名（仮）。実データの列名は不明のため、取込側で列対応表を通して標準列に変換する。
EXPORT_COLUMNS = {
    "quotes": {
        "quote_id": "見積番号", "quote_date": "見積日", "customer_id": "得意先コード", "product_code": "品目区分",
        "entity_code": "法人コード", "section_code": "課コード", "amount_local": "見積金額", "currency": "通貨",
        "updated_at": "更新日時",
    },
    "orders": {
        "order_id": "受注番号", "line_no": "行番号", "order_date": "受注日", "quote_id": "見積番号",
        "customer_id": "得意先コード", "product_code": "品目区分", "entity_code": "法人コード", "section_code": "課コード",
        "amount_local": "受注金額", "cost_local": "原価", "currency": "通貨", "status": "状態", "updated_at": "更新日時",
    },
    "shipments": {
        "order_id": "受注番号", "line_no": "行番号", "due_date": "回答納期", "ship_date": "出荷日", "updated_at": "更新日時",
    },
    "defects": {
        "defect_id": "不良番号", "order_id": "受注番号", "line_no": "行番号", "found_date": "発見日",
        "category": "不良区分", "updated_at": "更新日時",
    },
}
AMOUNT_COLUMN = {"quotes": "見積金額", "orders": "受注金額"}
DEFECT_CATEGORIES = ["寸法不良", "外観不良", "材質不良", "員数違い"]
