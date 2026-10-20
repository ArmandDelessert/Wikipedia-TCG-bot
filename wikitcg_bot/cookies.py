"""Cookies au format Netscape (« cookies.txt », celui de curl) et au format Playwright.

Chaque ligne d'un export compte 7 colonnes séparées par des tabulations :

    domaine  inclut-les-sous-domaines  chemin  https-uniquement  expiration  nom  valeur

L'expiration est un horodatage Unix en secondes ; 0 désigne un cookie de session. Un préfixe
`#HttpOnly_` devant le domaine marque un cookie inaccessible au JavaScript de la page.
"""
from __future__ import annotations

import time
from pathlib import Path

HTTPONLY_PREFIX = "#HttpOnly_"

Cookie = dict  # forme attendue par `BrowserContext.add_cookies`


def parse_netscape(text: str) -> list[Cookie]:
    cookies: list[Cookie] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        http_only = line.startswith(HTTPONLY_PREFIX)
        if http_only:
            line = line[len(HTTPONLY_PREFIX):]
        elif not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, subdomains, path, secure, expiry, name, value = fields
        if subdomains.upper() == "TRUE" and not domain.startswith("."):
            domain = "." + domain
        cookies.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "expires": int(expiry) if expiry.isdigit() and int(expiry) > 0 else -1,
            "httpOnly": http_only,
            "secure": secure.upper() == "TRUE",
        })
    return cookies


def read_netscape(path: Path) -> list[Cookie]:
    return parse_netscape(path.read_text(encoding="utf-8"))


def is_expired(cookie: Cookie, now: float | None = None) -> bool:
    expires = cookie.get("expires", -1)
    return expires not in (None, -1) and expires > 0 and expires <= (time.time() if now is None else now)


def covers(domain: str, host: str) -> bool:
    """Le cookie de ce `domain` est-il envoyé à `host` ? (« .a.com » couvre « www.a.com »)"""
    bare = domain.lstrip(".").lower()
    host = host.lower()
    if domain.startswith("."):
        return host == bare or host.endswith("." + bare)
    return host == bare  # sans point initial : cookie propre à cet hôte


def for_hosts(cookies: list[Cookie], hosts: tuple[str, ...] | list[str]) -> list[Cookie]:
    return [c for c in cookies if any(covers(c["domain"], h) for h in hosts)]


def header(cookies: list[Cookie], host: str, now: float | None = None) -> str:
    """Valeur d'un en-tête `Cookie:` pour `host` (utile pour un client HTTP simple)."""
    return "; ".join(
        f"{c['name']}={c['value']}" for c in cookies
        if covers(c["domain"], host) and not is_expired(c, now)
    )
