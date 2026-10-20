"""Session locale d'un site : cookies exportés du navigateur, puis cookies renouvelés par le site.

Le bot part de l'export `<cookies_dir>/<hôte>_cookies.txt`, recopié dans
`<state_dir>/sessions/<site>.json`. Chaque run y réécrit les cookies tels que le site les a
renouvelés : c'est ce qui rend inutile tout « secret à mettre à jour ». Si vous refaites un export
plus récent que cet état, il reprend le dessus.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .config import Settings
from .cookies import Cookie, for_hosts, is_expired, read_netscape

log = logging.getLogger(__name__)


class SessionStore:
    def __init__(self, settings: Settings, site: str, hosts: tuple[str, ...],
                 import_export: bool = True):
        self.settings = settings
        self.site = site
        self.hosts = tuple(hosts)
        # Faux pour un site dont le jeton de session tourne à chaque usage (Supabase) : réutiliser
        # une copie exportée invaliderait la session du navigateur d'où elle vient.
        self.import_export = import_export

    @property
    def state_path(self) -> Path:
        return self.settings.sessions_dir / f"{self.site}.json"

    def export_path(self) -> Path | None:
        if not self.import_export:
            return None
        for host in self.hosts:
            path = self.settings.cookies_dir / f"{host}_cookies.txt"
            if path.is_file():
                return path
        return None

    def configured(self) -> bool:
        return self.state_path.is_file() or self.export_path() is not None

    def load(self) -> list[Cookie]:
        """Cookies à utiliser pour ce run (sans ceux qui ont expiré)."""
        export = self.export_path()
        state = self.state_path
        if export and (not state.is_file() or export.stat().st_mtime > state.stat().st_mtime):
            cookies = for_hosts(read_netscape(export), self.hosts)
            log.info("Session %s : cookies importés depuis %s.", self.site, export.name)
            self.save(cookies)
            return [c for c in cookies if not is_expired(c)]
        if state.is_file():
            data = json.loads(state.read_text(encoding="utf-8"))
            return [c for c in data.get("cookies", []) if not is_expired(c)]
        return []

    def save(self, cookies: list[Cookie]) -> None:
        """Écrit l'état (de façon atomique) ; ne garde que les cookies de ce site, non expirés."""
        keep = [c for c in for_hosts(cookies, self.hosts) if not is_expired(c)]
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at": int(time.time()), "cookies": keep}, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)

    def value(self, name: str) -> str:
        for cookie in self.load():
            if cookie["name"] == name:
                return cookie["value"]
        return ""

    def describe(self, names: tuple[str, ...] = ()) -> str:
        """Résumé lisible des échéances (jamais les valeurs)."""
        cookies = [c for c in self.load() if not names or c["name"] in names]
        if not cookies:
            return "aucun cookie"
        now = time.time()
        parts = []
        for cookie in sorted(cookies, key=lambda c: c["name"]):
            expires = cookie.get("expires", -1)
            if expires in (None, -1) or expires <= 0:
                parts.append(f"{cookie['name']} (session)")
            else:
                days = (expires - now) / 86400
                parts.append(f"{cookie['name']} (expire dans {days:.1f} j)")
        return ", ".join(parts)
