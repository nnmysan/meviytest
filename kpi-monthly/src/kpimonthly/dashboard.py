"""確認用HTMLダッシュボードの生成（データを埋め込んだ単一ファイル）。

本番の閲覧は Power BI / DOMO を想定。このHTMLは画面構成の検証と、ダミーデータでの動作確認用。
"""
from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("dashboard_template.html")


def render(bundle: dict, path: Path) -> None:
    from .export import _jsonable
    data = json.dumps(_clean(bundle), ensure_ascii=False, default=_jsonable, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", data)
    Path(path).write_text(html, encoding="utf-8")


def _clean(o):
    """NaN/Inf を null に（JSON として正しくするため）。"""
    import math
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o
