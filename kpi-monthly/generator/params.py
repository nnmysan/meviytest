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
        "order_ts": "受注日時", "quantity": "受注数量", "supplier_code": "仕入先コード", "material": "材質",
        "part_size": "サイズ区分", "surface": "表面処理", "heat": "熱処理",
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

# ---------------------------------------------------------------- 仕様・サプライヤ（すべて架空）
# 受注明細の時刻分布（0〜23時の相対的な重み）。オンライン受注を想定し夜間も少し入る。
HOUR_WEIGHTS = [1, 0.5, 0.3, 0.2, 0.2, 0.4, 1, 3, 7, 11, 13, 12, 7, 9, 12, 12, 11, 9, 6, 4, 3, 2.5, 2, 1.5]

# 数量（個）の分布：試作1個が多く、たまにロット
QTY_VALUES = [1, 2, 3, 4, 5, 10, 20, 50, 100]
QTY_WEIGHTS = [0.34, 0.18, 0.1, 0.08, 0.1, 0.12, 0.05, 0.02, 0.01]

PART_SIZES = ["S", "M", "L", "XL"]          # S:〜50mm, M:〜200mm, L:〜500mm, XL:500mm超（仮）
SURFACES = ["なし", "黒染め", "無電解ニッケル", "アルマイト", "亜鉛めっき", "塗装"]
HEATS = ["なし", "焼入れ焼戻し", "高周波焼入れ", "調質"]

# 材質ごとに可能な表面処理・熱処理
MATERIAL_SPEC = {
    "SS400":  {"surface": ["なし", "黒染め", "亜鉛めっき", "塗装"], "heat": ["なし"]},
    "S45C":   {"surface": ["なし", "黒染め", "無電解ニッケル"], "heat": ["なし", "焼入れ焼戻し", "高周波焼入れ", "調質"]},
    "SCM435": {"surface": ["なし", "黒染め"], "heat": ["焼入れ焼戻し", "高周波焼入れ", "調質"]},
    "SUS304": {"surface": ["なし"], "heat": ["なし"]},
    "A5052":  {"surface": ["なし", "アルマイト"], "heat": ["なし"]},
    "A7075":  {"surface": ["なし", "アルマイト"], "heat": ["なし"]},
    "C3604":  {"surface": ["なし", "無電解ニッケル"], "heat": ["なし"]},
    "SPCC":   {"surface": ["なし", "亜鉛めっき", "塗装"], "heat": ["なし"]},
}

# サプライヤ（架空）: 法人、得意な商品、材質、サイズ。能力の組み合わせは材質ごとに可能な処理から作る。
SUPPLIERS = [
    # code,       entity, products,          materials,                           sizes
    ("SUP-JP01", "JP", ["MSQ"],            ["SS400", "S45C", "A5052"],           ["S", "M"]),
    ("SUP-JP02", "JP", ["MSQ"],            ["S45C", "SUS304", "A7075"],          ["M", "L"]),
    ("SUP-JP03", "JP", ["MSQ", "MRD"],     ["SUS304", "A5052", "C3604"],         ["S", "M"]),
    ("SUP-JP04", "JP", ["MRD"],            ["S45C", "SCM435", "SUS304"],         ["S", "M", "L"]),
    ("SUP-JP05", "JP", ["MRD"],            ["C3604", "A5052", "S45C"],           ["S", "M"]),
    ("SUP-JP06", "JP", ["SHM"],            ["SPCC", "SUS304", "A5052"],          ["M", "L", "XL"]),
    ("SUP-JP07", "JP", ["SHM", "SWD"],     ["SPCC", "SS400", "SUS304"],          ["L", "XL"]),
    ("SUP-JP08", "JP", ["SWD"],            ["SS400", "SUS304", "A5052"],         ["M", "L", "XL"]),
    ("SUP-KR01", "KR", ["MSQ", "MRD"],     ["S45C", "SUS304", "A5052"],          ["S", "M", "L"]),
    ("SUP-KR02", "KR", ["MRD", "MSQ"],     ["C3604", "SCM435", "S45C"],          ["S", "M"]),
    ("SUP-KR03", "KR", ["SHM", "SWD"],     ["SPCC", "SS400", "SUS304"],          ["M", "L", "XL"]),
    ("SUP-EU01", "EU", ["MSQ", "MRD"],     ["S45C", "SUS304", "A7075"],          ["S", "M", "L"]),
    ("SUP-EU02", "EU", ["MSQ", "MRD"],     ["A5052", "C3604", "SUS304"],         ["S", "M"]),
    ("SUP-EU03", "EU", ["SHM", "SWD"],     ["SPCC", "SS400", "SUS304"],          ["M", "L", "XL"]),
    ("SUP-CN01", "CN", ["MSQ"],            ["SS400", "S45C", "A5052"],           ["S", "M", "L"]),
    ("SUP-CN02", "CN", ["MRD", "MSQ"],     ["S45C", "SCM435", "C3604"],          ["S", "M"]),
    ("SUP-CN03", "CN", ["SHM"],            ["SPCC", "SUS304", "A5052"],          ["M", "L", "XL"]),
    ("SUP-CN04", "CN", ["SWD", "SHM"],     ["SS400", "SPCC", "SUS304"],          ["L", "XL"]),
]
# 供給能力＝基準の需要 ÷ 負荷率の設定値。平常時は 40〜72% 程度に収まるようにする（仮）。
CAPACITY_UTIL_RANGE = (0.40, 0.65)
REF_BUSINESS_DAYS = 21.5
