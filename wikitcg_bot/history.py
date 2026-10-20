"""Historique local des tirages (`<state_dir>/history.jsonl`, une ligne JSON par run et par site)."""
from __future__ import annotations

import json
import time

from .config import Settings
from .models import SiteResult


def record(settings: Settings, result: SiteResult) -> None:
    if settings.dry_run or not result.opened:
        return
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "site": result.site,
        "opened": result.opened,
        "stock_before": result.packs_before,
        "cards": [{"title": c.title, "rarity": c.rarity} for c in result.cards],
    }
    settings.history_file.parent.mkdir(parents=True, exist_ok=True)
    with settings.history_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
