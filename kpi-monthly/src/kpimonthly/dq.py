"""データ品質（DQ）の記録と、営業日カレンダー。"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

PRIORITY_ORDER = {"P1": 1, "P2": 2, "P3": 3, "INFO": 4}

DQ_RULE_LABEL = {
    "missing_file": "ファイル未着",
    "late_arrival": "期限後の到着",
    "schema_error": "列構成の不一致",
    "encoding_mismatch": "文字コードの相違",
    "extra_columns": "想定外の列",
    "quarantine_rows": "不正な行（隔離）",
    "exact_duplicates": "完全重複行",
    "key_conflicts": "同一キーで値が異なる行",
    "duplicate_file": "同一内容のファイル再送",
    "updated_rows": "再抽出による行の更新",
    "unknown_code": "マスタ未登録コード",
    "orphan_rows": "対応する受注がない明細",
    "control_mismatch": "件数・金額の照合不一致",
    "control_missing": "管理ファイルなし",
    "row_count_anomaly": "件数の急増減",
    "fx_missing": "為替レート未登録",
    "restatement": "過去値の修正",
    "master_error": "マスタ不備",
}


@dataclass
class DQIssue:
    rule: str
    priority: str
    message: str
    source: str = ""
    entity: str = ""
    month: str = ""
    file: str = ""
    count: int = 0
    blocking: bool = False

    @property
    def issue_id(self) -> str:
        raw = f"{self.rule}|{self.source}|{self.entity}|{self.month}|{self.file}"
        return "Q" + hashlib.sha1(raw.encode()).hexdigest()[:10]


class DQ:
    def __init__(self):
        self.issues: list[DQIssue] = []

    def add(self, rule, priority, message, **kw) -> DQIssue:
        issue = DQIssue(rule=rule, priority=priority, message=message, **kw)
        self.issues.append(issue)
        return issue

    @property
    def blocking(self) -> bool:
        return any(i.blocking for i in self.issues)

    def frame(self) -> pd.DataFrame:
        cols = ["issue_id", "rule", "rule_label", "priority", "blocking", "source", "entity", "month", "file", "count", "message"]
        if not self.issues:
            return pd.DataFrame(columns=cols)
        df = pd.DataFrame([asdict(i) | {"issue_id": i.issue_id} for i in self.issues])
        df["rule_label"] = df["rule"].map(DQ_RULE_LABEL).fillna(df["rule"])
        df["_o"] = df["priority"].map(PRIORITY_ORDER)
        return df.sort_values(["_o", "rule", "source", "entity", "month"]).drop(columns="_o")[cols].reset_index(drop=True)


class BusinessCalendar:
    def __init__(self, holidays: set[str]):
        self.holidays = np.array(sorted(holidays), dtype="datetime64[D]")

    def nth_business_day(self, month: str, n: int) -> pd.Timestamp:
        start = np.datetime64(f"{month}-01")
        d = np.busday_offset(start, n - 1, roll="forward", holidays=self.holidays)
        return pd.Timestamp(d)

    def add_business_days(self, day: pd.Timestamp, n: int) -> pd.Timestamp:
        d = np.busday_offset(np.datetime64(day.date()), n, roll="backward", holidays=self.holidays)
        return pd.Timestamp(d)


def next_month(month: str, k: int = 1) -> str:
    return str(pd.Period(month, "M") + k)
