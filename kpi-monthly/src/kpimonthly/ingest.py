"""取込：着地したCSVを列対応表で標準列に変換し、検証・重複排除・隔離を行う。

設計方針
- 取り込み元のファイルは変更しない（raw はそのまま保管し、ハッシュで追跡）
- 必須列が見つからない等、黙って進めると数字を誤るものは「取込停止（blocking）」
- 行単位の不正は隔離して件数を記録し、正常な行だけで集計する
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .dq import DQ

DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]
DATETIME_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"]


@dataclass
class LandedFile:
    order: int
    path: Path
    source: str
    entity: str
    yyyymm: str
    suffix: str

    @property
    def month(self) -> str:
        return f"{self.yyyymm[:4]}-{self.yyyymm[4:]}"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _norm(s: pd.Series) -> pd.Series:
    """NFKC正規化（全角数字・全角英字・全角記号を半角へ）と前後空白の除去。"""
    return s.map(lambda x: unicodedata.normalize("NFKC", x).strip() if not x.isascii() else x.strip())


def _decode(raw: bytes, declared: str) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8-sig"
    for enc in [declared, "utf-8", "cp932"]:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    raise UnicodeDecodeError("unknown", raw, 0, 1, "文字コードを判別できません")


def _parse_dates(s: pd.Series, formats: list[str]) -> pd.Series:
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    todo = s != ""
    for fmt in formats:
        if not todo.any():
            break
        parsed = pd.to_datetime(s[todo], format=fmt, errors="coerce")
        ok = parsed.notna()
        out.loc[parsed.index[ok]] = parsed[ok]
        todo = todo & out.isna()
    return out


def _parse_number(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.str.replace(",", "", regex=False).where(s != ""), errors="coerce")


def discover(landing_dirs: list[Path], src_cfg: dict) -> list[LandedFile]:
    files = []
    for order, d in enumerate(landing_dirs):
        for source, spec in src_cfg["sources"].items():
            rx = re.compile(spec["file_regex"])
            base = Path(d) / spec["dir"]
            if not base.exists():
                continue
            for p in sorted(base.rglob("*.csv")):
                m = rx.match(p.name)
                if m:
                    files.append(LandedFile(order, p, source, m["entity"], m["yyyymm"], m["suffix"] or ""))
    return files


def load_masters(landing_dirs: list[Path], src_cfg: dict, dq: DQ) -> dict[str, pd.DataFrame]:
    masters = {}
    for name, spec in src_cfg["masters"].items():
        path = None
        for d in landing_dirs:  # 後から届いたマスタを優先
            cand = Path(d) / spec["file"]
            if cand.exists():
                path = cand
        if path is None:
            dq.add("master_error", "P1", f"マスタ {name} が見つかりません（{spec['file']}）", blocking=True)
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        missing = [c for c in spec["required"] if c not in df.columns]
        if missing:
            dq.add("master_error", "P1", f"マスタ {name} に列 {missing} がありません", file=str(path), blocking=True)
            continue
        if spec.get("key") and df[spec["key"]].duplicated().any():
            dq.add("master_error", "P1", f"マスタ {name} のキー {spec['key']} が重複しています", file=str(path), blocking=True)
        masters[name] = df
    for name, cols in {"fx_rates": ["budget_rate", "actual_rate"], "targets": ["target"]}.items():
        if name in masters:
            for c in cols:
                masters[name][c] = pd.to_numeric(masters[name][c])
    return masters


def _check_control(lf: LandedFile, raw: pd.DataFrame, spec: dict, dq: DQ) -> None:
    ctl_spec = spec.get("control")
    if not ctl_spec:
        return
    ctl_path = lf.path.with_name(lf.path.name + ctl_spec.get("suffix", ".ctl"))
    if not ctl_path.exists():
        dq.add("control_missing", "P3", "管理ファイル（件数・金額合計）がありません", source=lf.source, entity=lf.entity,
               month=lf.month, file=lf.path.name)
        return
    ctl = json.loads(ctl_path.read_text(encoding="utf-8"))
    problems = []
    if int(ctl.get("rows", -1)) != len(raw):
        problems.append(f"件数 管理={ctl.get('rows')} 実データ={len(raw)}")
    amt_col = ctl_spec.get("amount_column")
    if amt_col and "amount_sum" in ctl:
        total = float(_parse_number(_norm(raw[amt_col])).fillna(0).sum())
        if abs(total - float(ctl["amount_sum"])) > max(1.0, 1e-9 * abs(total)):
            problems.append(f"金額合計 管理={ctl['amount_sum']:,.2f} 実データ={total:,.2f}")
    if problems:
        dq.add("control_mismatch", "P1", "管理ファイルと一致しません（" + "、".join(problems) + "）。欠落・二重出力の可能性があるため取込を停止しました。",
               source=lf.source, entity=lf.entity, month=lf.month, file=lf.path.name, blocking=True)


def _read_one(lf: LandedFile, spec: dict, dq: DQ) -> tuple[pd.DataFrame | None, str]:
    text, enc = _decode(lf.path.read_bytes(), spec.get("encoding", "utf-8"))
    if enc.replace("-sig", "") != spec.get("encoding", "utf-8").replace("-sig", ""):
        dq.add("encoding_mismatch", "INFO", f"文字コードが想定（{spec.get('encoding')}）と異なります（{enc} として読み込み）",
               source=lf.source, entity=lf.entity, month=lf.month, file=lf.path.name)
    raw = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    headers = set(raw.columns)
    cols = spec["columns"]
    missing_required = [c["from"] for c in cols.values() if c.get("required") and c["from"] not in headers]
    if missing_required:
        dq.add("schema_error", "P1", f"必須列 {missing_required} が見つかりません。列名の変更・出力設定の変更の可能性があります。"
               f"実際の列: {list(raw.columns)}", source=lf.source, entity=lf.entity, month=lf.month, file=lf.path.name,
               blocking=True)
        return None, enc
    extra = sorted(headers - {c["from"] for c in cols.values()})
    if extra:
        dq.add("extra_columns", "INFO", f"想定外の列 {extra} は取り込みません", source=lf.source, entity=lf.entity,
               month=lf.month, file=lf.path.name)
    ctl_raw = raw.copy()
    raw = raw.rename(columns={c["from"]: name for name, c in cols.items() if c["from"] in headers})
    for name in cols:
        if name not in raw.columns:
            raw[name] = ""
    ctl_raw.columns = [next((n for n, c in cols.items() if c["from"] == h), h) for h in ctl_raw.columns]
    _check_control(lf, ctl_raw, spec, dq)
    raw = raw[list(cols)]
    raw["_file"] = lf.path.name
    raw["_order"] = lf.order
    raw["_row"] = np.arange(len(raw)) + 2  # ヘッダー行を1行目として数える
    raw["_entity_file"] = lf.entity
    raw["_month_file"] = lf.month
    return raw, enc


def _parse(raw: pd.DataFrame, spec: dict) -> tuple[pd.DataFrame, pd.Series]:
    out = pd.DataFrame(index=raw.index)
    reasons = np.full(len(raw), "", dtype=object)

    def flag(mask, text):
        nonlocal reasons
        m = np.asarray(mask, dtype=bool)
        reasons = np.where(m, np.where(reasons == "", text, reasons + "; " + text), reasons)

    for name, c in spec["columns"].items():
        s = _norm(raw[name].astype(str))
        empty = s == ""
        t = c["type"]
        if t == "date":
            v = _parse_dates(s, DATE_FORMATS)
        elif t == "datetime":
            v = _parse_dates(s, DATETIME_FORMATS)
        elif t in ("decimal", "int"):
            v = _parse_number(s)
            if t == "int":
                flag(v.notna() & (v != v.round()), f"整数でない: {name}")
        else:
            v = s.where(~empty, None)
        if c.get("required"):
            flag(empty, f"必須項目なし: {name}")
        if t in ("date", "datetime", "decimal", "int"):
            flag(~empty & v.isna(), f"形式不正: {name}")
        if "min" in c:
            flag(v.notna() & (v < c["min"]), f"範囲外（{c['min']}未満）: {name}")
        if "allowed" in c:
            flag(~empty & ~s.isin(c["allowed"]), f"許容値外: {name}")
        out[name] = v
    for name, c in spec["columns"].items():
        if c["type"] == "int":
            out[name] = out[name].astype("Int64")
    return out, pd.Series(reasons, index=raw.index)


def ingest(landing_dirs: list[Path], cfg, masters: dict, dq: DQ) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    src_cfg = cfg.sources
    files = discover(landing_dirs, src_cfg)
    seen: dict[str, str] = {}
    log, data, quarantine = [], {}, []
    by_source: dict[str, list] = {s: [] for s in src_cfg["sources"]}
    for lf in files:
        raw_bytes = lf.path.read_bytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()
        has_rows = raw_bytes.strip().count(b"\n") >= 1  # ヘッダーのみの空ファイルは内容が同じでも再送扱いしない
        entry = dict(path=str(lf.path), file=lf.path.name, source=lf.source, entity=lf.entity, month=lf.month,
                     suffix=lf.suffix, batch=lf.order + 1, sha256=digest, rows=0, encoding="", status="loaded")
        if digest in seen and has_rows:
            entry["status"] = "skipped_duplicate"
            dq.add("duplicate_file", "INFO", f"{seen[digest]} と同一内容のため読み飛ばしました", source=lf.source,
                   entity=lf.entity, month=lf.month, file=lf.path.name)
            log.append(entry)
            continue
        seen[digest] = lf.path.name
        spec = src_cfg["sources"][lf.source]
        raw, enc = _read_one(lf, spec, dq)
        entry["encoding"] = enc
        if raw is None:
            entry["status"] = "schema_error"
        else:
            entry["rows"] = len(raw)
            by_source[lf.source].append(raw)
        log.append(entry)

    for source, parts in by_source.items():
        spec = src_cfg["sources"][source]
        cols = list(spec["columns"])
        if not parts:
            data[source] = pd.DataFrame(columns=cols + ["_file", "_order", "_entity_file", "_month_file"])
            continue
        raw = pd.concat(parts, ignore_index=True)
        parsed, reasons = _parse(raw, spec)
        bad = reasons != ""
        if bad.any():
            q = raw.loc[bad, ["_file", "_row"] + cols].copy()
            q.insert(0, "source", source)
            q.insert(1, "reason", reasons[bad])
            quarantine.append(q)
            for (f, e, m), n in raw.loc[bad].groupby(["_file", "_entity_file", "_month_file"]).size().items():
                examples = "; ".join(sorted(set(reasons[bad & (raw["_file"] == f)]))[:3])
                dq.add("quarantine_rows", "P2", f"{n} 行を隔離しました（例: {examples}）", source=source, entity=e,
                       month=m, file=f, count=int(n))
        df = parsed[~bad].copy()
        for c in ("_file", "_order", "_entity_file", "_month_file"):
            df[c] = raw.loc[~bad, c]

        # 完全重複（全列一致）。同一ファイル内の重複は品質問題、別ファイル（再抽出・再送）との重複は想定内。
        in_file = df.duplicated(subset=cols + ["_file"], keep="first")
        if in_file.any():
            for (e, m), n in df[in_file].groupby(["_entity_file", "_month_file"]).size().items():
                dq.add("exact_duplicates", "P3", f"同じファイル内に完全に同じ行が {n} 行あり、1行にまとめました", source=source,
                       entity=e, month=m, count=int(n))
        df = df[~df.duplicated(subset=cols, keep="last")]
        # 同一キーで値が異なる行：更新日時が新しい行を採用（同時刻なら後から届いたファイル）
        key = spec["key"]
        df = df.sort_values(["updated_at", "_order", "_file"], kind="stable")
        kdup = df.duplicated(subset=key, keep=False)
        if kdup.any():
            conf = df[kdup]
            nfiles = conf.groupby(key)["_file"].transform("nunique")
            within = conf[nfiles == 1]
            across = conf[nfiles > 1]
            for (e, m), g in within.groupby(["_entity_file", "_month_file"]):
                n = g.drop_duplicates(subset=key).shape[0]
                dq.add("key_conflicts", "P2", f"同じファイル内で同じキー（{'+'.join(key)}）なのに内容が異なる行が {n} 件。"
                       "更新日時が新しい行を採用しました。出力元での二重登録がないか確認してください",
                       source=source, entity=e, month=m, count=int(n))
            for (e, m), g in across.groupby(["_entity_file", "_month_file"]):
                n = g.drop_duplicates(subset=key).shape[0]
                dq.add("updated_rows", "INFO", f"後から届いたファイルで {n} 件が更新されていました（更新日時が新しい行を採用）",
                       source=source, entity=e, month=m, count=int(n))
            df = df.drop_duplicates(subset=key, keep="last")
        data[source] = df.reset_index(drop=True)

    _map_codes(data, src_cfg, masters, dq)
    qdf = pd.concat(quarantine, ignore_index=True) if quarantine else pd.DataFrame(columns=["source", "reason", "_file", "_row"])
    return data, qdf, pd.DataFrame(log)


def _map_codes(data: dict, src_cfg: dict, masters: dict, dq: DQ) -> None:
    keys = {"products": "product_code", "entities": "entity_code", "org": "section_code"}
    for source, spec in src_cfg["sources"].items():
        df = data[source]
        for name, c in spec["columns"].items():
            m = c.get("master")
            if not m or m not in masters or df.empty:
                continue
            valid = set(masters[m][keys[m]])
            unknown = ~df[name].isin(valid)
            if unknown.any():
                for (e, mo), g in df[unknown].groupby(["_entity_file", "_month_file"]):
                    codes = sorted(set(g[name]))
                    dq.add("unknown_code", "P2", f"{name} に未登録コード {codes} が {len(g)} 行。『未分類(UNK)』として集計しました（合計は一致）",
                           source=source, entity=e, month=mo, count=int(len(g)))
                df.loc[unknown, name] = "UNK"
        if "customer_id" in df.columns and "customers" in masters and not df.empty:
            unknown = ~df["customer_id"].isin(set(masters["customers"]["customer_id"]))
            if unknown.any():
                dq.add("unknown_code", "P3", f"顧客マスタにない得意先コードが {int(unknown.sum())} 行（業種・規模は未分類）",
                       source=source, count=int(unknown.sum()))
