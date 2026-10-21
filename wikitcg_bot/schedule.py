"""Quand repasser sur un site.

Les sites ne se rechargent pas au même rythme (Wikipedia Gacha : 1 pack par minute, plafond 10 ;
WikiMasters : 1 par 10 min, plafond 10 ; wiki-tcg.com : 1 par 15 min, plafond 10 ; wikitcg.net : 1
pack gratuit toutes les ~2 h 40, plafond 5). Une seule tâche planifiée, assez fréquente pour le plus
rapide, suffit donc : chaque site n'est relancé que lorsque son stock approche du plafond, ce qui
évite de lancer Chrome pour rien et de gaspiller des packs.
"""
from __future__ import annotations

import json
import os
import time

from .config import Settings
from .models import SiteResult

# Durée de régénération d'une unité de stock, en secondes.
PERIOD_S = {"wikigacha": 60.0, "wikimasters": 600.0, "wiki_tcg_com": 900.0, "wikitcg_net": 9600.0}
DEFAULT_MAX = {"wikigacha": 10, "wikimasters": 10, "wiki_tcg_com": 10, "wikitcg_net": 5}
SAFETY = 0.8  # on repasse un peu avant que le stock soit plein
MIN_WAIT = 60.0
MAX_WAIT = 12 * 3600.0
# wikitcg.net : une simple requête HTTP, et un pack mystère à la cadence encore inconnue (~6 h ?) qu'un
# stock vidé ne reflète pas : on ne s'éloigne pas de plus de 3 h.
MAX_WAIT_BY_SITE = {"wikitcg_net": 3 * 3600.0}
RETRY_AFTER_FAILURE = 10 * 60.0  # erreur passagère : au tour suivant (un site rapide perdrait des packs)
RETRY_AFTER_AUTH_FAILURE = 30 * 60.0  # session expirée : inutile d'insister, il faut une action humaine


def _path(settings: Settings):
    return settings.state_dir / "schedule.json"


def _load(settings: Settings) -> dict[str, float]:
    try:
        data = json.loads(_path(settings).read_text(encoding="utf-8"))
        return {k: float(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save(settings: Settings, due: dict[str, float]) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(due), encoding="utf-8")
    os.replace(tmp, path)


def due_in(result: SiteResult) -> float | None:
    """Secondes avant de revenir, ou None si on ne sait pas (le site sera alors relancé à chaque tour)."""
    if not result.ok:
        return RETRY_AFTER_AUTH_FAILURE if result.auth_failed else RETRY_AFTER_FAILURE
    maximum = result.packs_max or DEFAULT_MAX.get(result.site)
    period = PERIOD_S.get(result.site)
    if result.packs_before is None or not maximum or not period:
        return None
    stock_after = max(0, result.packs_before - result.opened)
    wait = max(0, maximum - stock_after) * period * SAFETY
    return min(MAX_WAIT_BY_SITE.get(result.site, MAX_WAIT), MAX_WAIT, max(MIN_WAIT, wait))


def is_due(settings: Settings, site: str, now: float | None = None) -> bool:
    return _load(settings).get(site, 0.0) <= (time.time() if now is None else now)


def seconds_left(settings: Settings, site: str, now: float | None = None) -> float:
    return max(0.0, _load(settings).get(site, 0.0) - (time.time() if now is None else now))


def record(settings: Settings, result: SiteResult, now: float | None = None) -> None:
    """Note la prochaine échéance du site (appelé après un vrai run, jamais après un `status`)."""
    now = time.time() if now is None else now
    due = _load(settings)
    wait = due_in(result)
    if wait is None:
        due.pop(result.site, None)
    else:
        due[result.site] = now + wait
    _save(settings, due)


def clear(settings: Settings, site: str) -> None:
    due = _load(settings)
    if due.pop(site, None) is not None:
        _save(settings, due)
