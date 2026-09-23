"""設定の読み込みと検証。

プロファイル（dummy / prod）ごとに、列対応・閾値・レビュー置き場を切り替える。
取込・集計・分析・表示のコードは共通。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
VIEWS = {"v_order_lines", "v_quotes", "v_due_lines", "v_shipped_lines"}
DISPLAYS = {"yen", "count", "percent"}


class ConfigError(Exception):
    pass


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@dataclass
class Config:
    root: Path
    profile: dict
    kpis: list[dict]
    sources: dict
    thresholds: dict
    analysis: dict
    templates: dict
    holidays: set = field(default_factory=set)

    @property
    def is_dummy(self) -> bool:
        return bool(self.profile.get("is_dummy"))

    def kpi(self, kid: str) -> dict:
        return next(k for k in self.kpis if k["id"] == kid)

    def kpi_thresholds(self, kid: str) -> dict | None:
        return (self.thresholds.get("kpis") or {}).get(kid)

    @property
    def g(self) -> dict:
        return self.thresholds.get("global") or {}


def validate_kpi(k: dict) -> list[str]:
    errs = []
    for key in ("id", "name", "type", "display", "direction", "view", "numerator", "sources"):
        if key not in k:
            errs.append(f"KPI {k.get('id', '?')}: {key} がありません")
    if k.get("type") not in ("sum", "ratio"):
        errs.append(f"KPI {k.get('id')}: type は sum か ratio")
    if k.get("type") == "ratio" and not k.get("denominator"):
        errs.append(f"KPI {k.get('id')}: ratio には denominator が必要")
    if k.get("view") not in VIEWS:
        errs.append(f"KPI {k.get('id')}: view は {sorted(VIEWS)} のいずれか")
    if k.get("display") not in DISPLAYS:
        errs.append(f"KPI {k.get('id')}: display は {sorted(DISPLAYS)} のいずれか")
    if k.get("direction") not in ("higher_is_better", "lower_is_better"):
        errs.append(f"KPI {k.get('id')}: direction が不正")
    return errs


def load_config(profile: str | Path = "dummy", root: Path = ROOT, extra_kpi_files: list[Path] | None = None) -> Config:
    ppath = Path(profile) if str(profile).endswith(".yaml") else root / "config" / "profiles" / f"{profile}.yaml"
    prof = _yaml(ppath)
    kpis = [_yaml(p) for p in sorted((root / prof["kpi_dir"]).glob("*.yaml"))]
    for p in extra_kpi_files or []:
        kpis.append(_yaml(Path(p)))
    kpis.sort(key=lambda k: (k.get("order", 999), k.get("id", "")))
    errs = [e for k in kpis for e in validate_kpi(k)]
    ids = [k["id"] for k in kpis]
    if len(ids) != len(set(ids)):
        errs.append(f"KPI id が重複しています: {ids}")
    if errs:
        raise ConfigError("\n".join(errs))
    hol = set()
    if prof.get("holidays") and (root / prof["holidays"]).exists():
        hol = set(pd.read_csv(root / prof["holidays"])["date"].astype(str))
    cfg = Config(
        root=root, profile=prof, kpis=kpis, sources=_yaml(root / prof["sources"]),
        thresholds=_yaml(root / prof["thresholds"]), analysis=_yaml(root / prof["analysis"]),
        templates=_yaml(root / prof["action_templates"]), holidays=hol,
    )
    if prof.get("require_approved"):
        check_production_ready(cfg)
    return cfg


def check_production_ready(cfg: Config) -> None:
    """本番プロファイルの安全装置：仮の定義・閾値・列対応のままでは実行させない。"""
    problems = []
    th = cfg.thresholds
    if th.get("status") != "approved" or not th.get("approved_by"):
        problems.append("閾値が承認されていません（thresholds.status=approved と approved_by が必要）")
    for k in cfg.kpis:
        if k.get("status") != "approved":
            problems.append(f"KPI定義 {k['id']} が承認されていません（status={k.get('status')}）")
        if k["id"] not in (th.get("kpis") or {}):
            problems.append(f"KPI {k['id']} の本番閾値が未設定です")
    src_text = yaml.safe_dump(cfg.sources, allow_unicode=True)
    if "TODO" in src_text or cfg.sources.get("status") != "approved":
        problems.append("列対応表に TODO が残っているか、承認されていません（sources.status=approved が必要）")
    if problems:
        raise ConfigError("本番プロファイルを実行できません:\n- " + "\n- ".join(problems))
