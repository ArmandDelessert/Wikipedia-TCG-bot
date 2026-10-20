from __future__ import annotations

import random
import time
from typing import Protocol

from ..config import Settings
from ..models import SiteResult
from ..sessions import SessionStore


class Site(Protocol):
    name: str
    label: str
    hosts: tuple[str, ...]  # hôtes dont les cookies font la session (cf. `SessionStore`)
    login_url: str  # page où se connecter à la main (`python -m wikitcg_bot login <site>`)

    def configured(self, settings: Settings) -> bool: ...

    def run(self, settings: Settings) -> SiteResult: ...

    def session_summary(self, settings: Settings) -> str: ...


def session_store(settings: Settings, site: Site) -> SessionStore:
    return SessionStore(settings, site.name, site.hosts)


def pause(settings: Settings) -> None:
    low, high = settings.delay
    if high > 0:
        time.sleep(random.uniform(low, high))
