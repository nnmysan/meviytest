"""ダミーデータ生成 CLI。

    python -m generator.generate --scenario S1 --out data/dummy

出力（シナリオごと）:
    landing/batch_01/...   1回目に届くファイル（「自社システム」CSV を模した形式）
    landing/batch_02/...   遅れて届くファイル・過去月の再抽出（該当シナリオのみ）
    truth.csv              生成元データから独立に計算した KPI の正解値
    facts.json             注入した効果の正解情報（件数・金額など）
    manifest.json          seed・ファイルハッシュ・実行手順（runs）
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import params as P
from .truth import build_targets, compute_truth
from .world import apply_correction, build_world, month_list, section_of

GENERATOR_VERSION = "0.1.0"
SCENARIO_DIR = Path(__file__).parent / "scenarios"
SOURCES = ("quotes", "orders", "shipments", "defects")
PRIMARY_DATE = {"quotes": "quote_date", "orders": "order_date", "shipments": None, "defects": "found_date"}


def load_scenario(sid: str) -> dict:
    path = SCENARIO_DIR / f"{sid}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 出力形式

def _fmt_date(s: pd.Series, fmt: str = "%Y-%m-%d") -> pd.Series:
    return s.dt.strftime(fmt).fillna("")


def _fmt_num(s: pd.Series, dec: int) -> pd.Series:
    if dec == 0:
        return s.round(0).astype("int64").astype(str)
    return s.map(lambda v: f"{v:.{dec}f}")


def to_export(source: str, df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for col, jp in P.EXPORT_COLUMNS[source].items():
        v = df[col]
        if col.endswith("_date"):
            out[jp] = _fmt_date(v)
        elif col == "updated_at":
            out[jp] = _fmt_date(v, "%Y-%m-%d %H:%M:%S")
        elif col in ("amount_local", "cost_local"):
            dec = df["currency"].map(P.DECIMALS)
            out[jp] = [f"{a:.0f}" if d == 0 else f"{a:.2f}" for a, d in zip(v, dec)]
        elif col == "line_no":
            out[jp] = v.astype("int64").astype(str)
        else:
            out[jp] = v.fillna("").astype(str)
    return out.reset_index(drop=True)


def build_files(world: dict, months: list[str]) -> dict:
    """(batch, source, entity, yyyymm, suffix) -> {df, encoding}"""
    files = {}
    o = world["orders"]
    order_month = dict(zip(o.order_id, o.order_date.values.astype("datetime64[M]").astype(str)))
    for source in SOURCES:
        df = world[source].reset_index(drop=True)
        if source == "shipments":
            m = df.order_id.map(order_month)  # 出荷・納期明細は受注月単位で出力（仮の仕様）
        else:
            m = pd.Series(df[PRIMARY_DATE[source]].values.astype("datetime64[M]").astype(str))
        exp = to_export(source, df)
        groups = {k: idx for k, idx in exp.groupby([df.entity_code, m]).groups.items()}
        all_months = sorted(set(months) | set(m.dropna().unique()))
        for e in P.ENTITIES:
            for month in all_months:
                idx = groups.get((e, month))
                if idx is None and month not in months:
                    continue
                part = exp.loc[idx].reset_index(drop=True) if idx is not None else exp.iloc[0:0]
                files[(1, source, e, month.replace("-", ""), "")] = {"df": part, "encoding": "cp932"}
    return files


def _nfkc_float(x: str) -> float | None:
    try:
        return float(unicodedata.normalize("NFKC", x).replace(",", ""))
    except ValueError:
        return None


def write_files(files: dict, landing: Path) -> list[dict]:
    written = []
    for (batch, source, e, yyyymm, suffix), f in sorted(files.items()):
        d = landing / f"batch_{batch:02d}" / source / e
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{source}_{e}_{yyyymm}{suffix}.csv"
        f["df"].to_csv(path, index=False, encoding=f["encoding"], lineterminator="\n")
        ctl = f.get("control") or _control(source, f["df"])
        (d / (path.name + ".ctl")).write_text(json.dumps(ctl, ensure_ascii=False), encoding="utf-8")
        written.append({"path": str(path.relative_to(landing)), "rows": len(f["df"])})
    return written


def _control(source: str, df: pd.DataFrame) -> dict:
    ctl = {"rows": int(len(df))}
    col = P.AMOUNT_COLUMN.get(source)
    if col:
        vals = [_nfkc_float(x) for x in df[col]]
        ctl["amount_sum"] = round(sum(v for v in vals if v is not None), 2)
    return ctl


# ---------------------------------------------------------------- ファイル単位の効果

def _key(eff: dict, batch: int = 1) -> tuple:
    return (batch, eff["source"], eff["entity"], eff["month"].replace("-", ""), "")


def fe_drop_files(files, eff, facts, **_):
    """指定法人の指定月以降のファイルを「未着」にする。"""
    frm = eff["from_month"].replace("-", "")
    drop = [k for k in files if k[0] == 1 and k[2] == eff["entity"] and k[3] >= frm]
    for k in drop:
        del files[k]
    facts.setdefault("dropped_files", []).extend("_".join(map(str, k[1:4])) for k in drop)


def fe_delay_files(files, eff, facts, **_):
    """指定法人の指定月以降のファイルを2回目の到着（batch_02）に回す。"""
    frm = eff["from_month"].replace("-", "")
    for k in [k for k in files if k[0] == 1 and k[2] == eff["entity"] and k[3] >= frm]:
        files[(2,) + k[1:]] = files.pop(k)
    facts["delayed_entity"] = eff["entity"]


def fe_correction(files, eff, facts, world_final, **_):
    """過去月を再抽出したファイル（_r1）を batch_02 として届ける。"""
    o = world_final["orders"]
    sel = o[(o.entity_code == eff["entity"]) & (o.order_date.dt.strftime("%Y-%m") == eff["month"])]
    files[(2, "orders", eff["entity"], eff["month"].replace("-", ""), "_r1")] = {"df": to_export("orders", sel), "encoding": "cp932"}


def fe_invalid_rows(files, eff, facts, **_):
    """必須欠損・負の金額・存在しない日付の行を追加する（隔離されるべき行）。"""
    f = files[_key(eff)]
    df = f["df"]
    tmpl = df.iloc[0]
    rows = []
    specs = [("null_customer", eff["null_customer"]), ("negative_amount", eff["negative_amount"]), ("bad_date", eff["bad_date"])]
    seq = 1
    for kind, n in specs:
        for _ in range(n):
            r = tmpl.copy()
            r["受注番号"] = f"SO{eff['entity']}X{seq:05d}"
            r["行番号"] = "1"
            r["見積番号"] = ""
            if kind == "null_customer":
                r["得意先コード"] = ""
            elif kind == "negative_amount":
                r["受注金額"] = "-5000"
            else:
                r["受注日"] = "2026/13/01"
            rows.append(r)
            seq += 1
    f["df"] = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    facts["invalid_rows"] = {k: n for k, n in specs} | {"total": sum(n for _, n in specs)}


def fe_reformat(files, eff, facts, **_):
    """全角数字・桁区切り・スラッシュ日付に書式を崩す（値は同じ）。"""
    f = files[_key(eff)]
    df = f["df"].copy()
    idx = df.index[: eff["rows"]]
    to_full = str.maketrans("0123456789", "０１２３４５６７８９")
    df.loc[idx, "受注金額"] = [f"{float(v):,.0f}".translate(to_full) if "." not in v else v for v in df.loc[idx, "受注金額"]]
    df.loc[idx, "受注日"] = [v.replace("-", "/") for v in df.loc[idx, "受注日"]]
    f["df"] = df
    facts["reformatted_rows"] = len(idx)


def fe_encoding(files, eff, facts, **_):
    files[_key(eff)]["encoding"] = eff["encoding"]
    facts["encoding_changed"] = f"{eff['source']}_{eff['entity']}_{eff['month']}"


def fe_duplicates(files, eff, facts, **_):
    """同一ファイルの再送、ファイル内の完全重複行、同一キーで値が異なる行（古い版）を注入する。"""
    if eff.get("resend"):
        k = _key(eff["resend"])
        files[k[:4] + ("_resend",)] = copy.deepcopy(files[k])
        facts["resent_file"] = "_".join(map(str, k[1:4]))
    if eff.get("exact"):
        ex = eff["exact"]
        f = files[_key(ex)]
        df = f["df"]
        n = int(round(len(df) * ex["share"]))
        f["df"] = pd.concat([df, df.iloc[:n]], ignore_index=True)
        facts["exact_duplicates"] = n
    if eff.get("conflict"):
        cf = eff["conflict"]
        f = files[_key(cf)]
        df = f["df"]
        old = df.iloc[-cf["n"]:].copy()
        old["受注金額"] = [f"{float(v) * 1.2:.0f}" for v in old["受注金額"]]
        old["更新日時"] = [(pd.Timestamp(v) - pd.Timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S") for v in old["更新日時"]]
        f["df"] = pd.concat([df, old], ignore_index=True)
        facts["conflicting_keys"] = cf["n"]


def fe_rename_column(files, eff, facts, **_):
    f = files[_key(eff)]
    f["control"] = _control(eff["source"], f["df"])  # 管理ファイルは出力元システムが元の金額列から作る
    f["df"] = f["df"].rename(columns=eff["rename"])
    facts["renamed"] = eff["rename"]


def fe_unknown_code(files, eff, facts, world_final, **_):
    """既存の受注（n件分の全明細）の品目区分を未登録コードに置き換える。"""
    f = files[_key(eff)]
    df = f["df"].copy()
    ids = [i for i in dict.fromkeys(df["受注番号"]) if not i.endswith("L001")][: eff["orders"]]
    mask = df["受注番号"].isin(ids)
    df.loc[mask, "品目区分"] = eff["code"]
    o = world_final["orders"]
    sel = o[o.order_id.isin(ids) & (o.status != "取消")]
    facts["unknown_code"] = {"code": eff["code"], "orders": len(ids), "rows": int(mask.sum()),
                             "amount_jpy": float((sel.amount_local * P.BUDGET_RATE[P.ENTITIES[eff['entity']]['currency']]).sum())}
    f["df"] = df


def fe_truncate(files, eff, facts, **_):
    """管理ファイル（件数・金額合計）は元のまま、データの末尾行が欠落した状態を作る。"""
    f = files[_key(eff)]
    f["control"] = _control(eff["source"], f["df"])
    f["df"] = f["df"].iloc[: -eff["drop_last_rows"]].reset_index(drop=True)
    facts["truncated_rows"] = eff["drop_last_rows"]


FILE_EFFECTS = {
    "drop_files": fe_drop_files, "delay_files": fe_delay_files, "correction": fe_correction,
    "invalid_rows": fe_invalid_rows, "reformat": fe_reformat, "encoding": fe_encoding, "duplicates": fe_duplicates,
    "rename_column": fe_rename_column, "unknown_code": fe_unknown_code, "truncate": fe_truncate,
}


# ---------------------------------------------------------------- マスタ

def build_masters(customers: pd.DataFrame, fx: pd.DataFrame, months: list[str]) -> dict[str, pd.DataFrame]:
    entities = pd.DataFrame([dict(entity_code=e, entity_name=v["name"], currency=v["currency"]) for e, v in P.ENTITIES.items()])
    products = pd.DataFrame([dict(product_code=p, product_name=v["name"]) for p, v in P.PRODUCTS.items()])
    org = []
    for p, v in P.PRODUCTS.items():
        for jp in (True, False):
            e = "JP" if jp else "KR"
            org.append(dict(section_code=section_of(e, p), section_name=f"{v['name'].replace('切削', '')}{'国内' if jp else '海外'}課",
                            team_code=v["team"][0], team_name=v["team"][1], division_code=v["division"][0],
                            division_name=v["division"][1]))
    owners = [dict(kpi_id="*", entity_code="*", product_code="*", owner="経営企画 担当（仮）", manager="経営企画部長（仮）"),
              dict(kpi_id="on_time_delivery", entity_code="*", product_code="*", owner="生産管理 担当（仮）", manager="生産管理部長（仮）"),
              dict(kpi_id="defect_rate", entity_code="*", product_code="*", owner="品質保証 担当（仮）", manager="品質保証部長（仮）")]
    for e, ev in P.ENTITIES.items():
        owners.append(dict(kpi_id="*", entity_code=e, product_code="*", owner=f"{ev['name']}法人 営業管理（仮）",
                           manager=f"{ev['name']}法人長（仮）"))
        for p, pv in P.PRODUCTS.items():
            owners.append(dict(kpi_id="*", entity_code=e, product_code=p, owner=f"{ev['name']}・{pv['name']} 担当（仮）",
                               manager=f"{pv['division'][1]}長（仮）"))
    return {
        "entities": entities, "products": products, "org": pd.DataFrame(org),
        "customers": customers.drop(columns=["weight"]), "fx_rates": fx,
        "targets": build_targets(months), "owners": pd.DataFrame(owners),
    }


# ---------------------------------------------------------------- メイン

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(scenario: dict, out_root: Path) -> Path:
    sid = scenario["id"]
    seed = int(scenario.get("seed", 20260901))
    world_effects = [e for e in scenario.get("effects", []) if e["type"] not in FILE_EFFECTS]
    file_effects = [e for e in scenario.get("effects", []) if e["type"] in FILE_EFFECTS]
    months = month_list(P.START_MONTH, P.END_MONTH)

    world, customers, fx, facts = build_world(seed, world_effects, P.AS_OF_BATCH1)
    for eff in world_effects:
        if eff["type"] == "fx_shock":
            r = fx[(fx.currency == eff["currency"]) & (fx.month == eff["month"])].iloc[0]
            facts["fx_shock"] = {"currency": eff["currency"], "month": eff["month"],
                                 "actual_rate": float(r.actual_rate), "budget_rate": float(r.budget_rate)}
    world_final = copy.deepcopy(world)
    for eff in file_effects:
        if eff["type"] == "correction":
            eff = {**eff, "updated_at": eff.get("updated_at", "2026-09-10 10:00:00")}
            apply_correction(world_final, eff, seed, facts)

    files = build_files(world, months)
    for eff in file_effects:
        FILE_EFFECTS[eff["type"]](files, eff, facts, world_final=world_final)

    out = out_root / sid
    if out.exists():
        shutil.rmtree(out)
    landing = out / "landing"
    written = write_files(files, landing)
    masters = build_masters(customers, fx, months)
    mdir = landing / "batch_01" / "master"
    mdir.mkdir(parents=True, exist_ok=True)
    for name, df in masters.items():
        df.to_csv(mdir / f"{name}.csv", index=False, encoding="utf-8", lineterminator="\n")

    truth = compute_truth(world_final, fx, P.AS_OF_BATCH1, months)
    truth.to_csv(out / "truth.csv", index=False, lineterminator="\n", float_format="%.10g")
    (out / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    batches = sorted({p.name for p in landing.iterdir() if p.is_dir()})
    runs = [{"landing": ["landing/batch_01"], "as_of": P.AS_OF_BATCH1}]
    if "batch_02" in batches:
        runs.append({"landing": ["landing/batch_01", "landing/batch_02"], "as_of": P.AS_OF_BATCH2})
    hashes = {str(p.relative_to(out)): _sha256(p) for p in sorted(landing.rglob("*")) if p.is_file()}
    manifest = {
        "scenario": sid, "name": scenario.get("name"), "seed": seed, "generator_version": GENERATOR_VERSION,
        "target_month": P.END_MONTH, "runs": runs, "files": len(written), "file_hashes": hashes,
        "note": "ダミーデータ。値・列名・組織・顧客・目標はすべて架空。",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="ダミーデータ生成")
    ap.add_argument("--scenario", nargs="+", default=["S0"], help="シナリオID（all で全件）")
    ap.add_argument("--out", default="data/dummy")
    args = ap.parse_args(argv)
    ids = sorted(p.stem for p in SCENARIO_DIR.glob("S*.yaml")) if args.scenario == ["all"] else args.scenario
    for sid in ids:
        path = generate(load_scenario(sid), Path(args.out))
        print(f"generated {sid} -> {path}")


if __name__ == "__main__":
    main()
