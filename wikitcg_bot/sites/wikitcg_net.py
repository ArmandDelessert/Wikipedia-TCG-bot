"""wikitcg.net — API HTTP directe, authentifiée par le cookie `wtcg_session`.

Les endpoints (`/api/nav/status`, `/api/series`, `/api/collection`,
`/api/packs/open`) viennent du projet dochreidte/wikitcg-completer.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..models import AuthError, Card, SiteError, SiteResult
from .base import pause

log = logging.getLogger(__name__)

BASE_URL = "https://wikitcg.net"
COOKIE = "wtcg_session"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0"
)
MAX_RETRIES = 3


def jwt_expiry(token: str) -> int | None:
    """Date d'expiration (epoch) d'un JWT, ou None s'il n'est pas décodable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if exp is not None else None
    except (IndexError, ValueError, TypeError):
        return None


def pick_session(candidates: list[str], now: float | None = None) -> str:
    """Retient le cookie le plus frais parmi le secret et celui rafraîchi par le serveur.

    `candidates` est classé par priorité : en cas d'égalité le premier gagne.
    """
    now = time.time() if now is None else now
    candidates = [c for c in candidates if c]
    if not candidates:
        raise AuthError("WIKITCG_NET_SESSION n'est pas défini.")
    best = max(candidates, key=lambda c: jwt_expiry(c) or 0)
    expiry = jwt_expiry(best)
    if expiry is not None and expiry <= now:
        raise AuthError(
            "Le cookie wtcg_session a expiré : copiez-en un nouveau dans le secret "
            "WIKITCG_NET_SESSION."
        )
    return best


def _as_list(data: Any, key: str) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get(key)
        if isinstance(inner, list):
            return inner
    return []


def choose_series(series: list[dict], collection: list[dict], pinned: str = "") -> str:
    """Série à ouvrir : celle imposée, sinon celle où il manque le plus de cartes."""
    if pinned:
        return pinned
    owned = {e.get("series_id"): e.get("owned") or 0 for e in collection}
    best_id, best_missing = None, -1
    for s in series:
        sid = s.get("id")
        if not sid:
            continue
        missing = (s.get("cardCount") or 0) - owned.get(sid, 0)
        if missing > best_missing:
            best_id, best_missing = sid, missing
    if best_id is None:
        raise SiteError("Aucune série ouvrable trouvée (WIKITCG_NET_SERIES peut en imposer une).")
    return best_id


class WikiTcgNet:
    name = "wikitcg_net"
    label = "wikitcg.net"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport
        self._saved = ""

    # -- configuration ------------------------------------------------------
    def _state_file(self, settings: Settings) -> Path:
        return settings.state_dir / "wikitcg_net_session.txt"

    def _saved_session(self, settings: Settings) -> str:
        try:
            return self._state_file(settings).read_text().strip()
        except OSError:
            return ""

    def configured(self, settings: Settings) -> bool:
        return bool(settings.get("WIKITCG_NET_SESSION"))

    # -- exécution ----------------------------------------------------------
    def run(self, settings: Settings) -> SiteResult:
        session = pick_session(
            [settings.get("WIKITCG_NET_SESSION"), self._saved_session(settings)]
        )
        result = SiteResult(self.name, self.label)
        with httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Origin": BASE_URL, "Referer": BASE_URL + "/"},
            timeout=30.0,
            transport=self._transport,
        ) as client:
            client.cookies.set(COOKIE, session, domain="wikitcg.net")
            self._saved = session
            status = self._request(client, settings, "GET", "/api/nav/status")
            remaining = int(status.get("totalAvailable", status.get("freePacks", 0)) or 0)
            result.packs_before = remaining
            result.packs_max = status.get("maxFreePacks")

            if not settings.dry_run:
                self._open_mystery(client, settings, result)
            if remaining <= 0 or settings.dry_run:
                result.message = (
                    "Simulation : rien n'a été ouvert." if settings.dry_run
                    else f"Aucun pack disponible (prochaine régénération : "
                         f"{status.get('nextRegenAt') or 'inconnue'})."
                )
                return result

            series_id = self._pick_series(client, settings)
            while remaining > 0 and result.opened < settings.max_packs:
                res = self._request(
                    client, settings, "POST", "/api/packs/open", json={"seriesId": series_id}
                )
                if not isinstance(res, dict) or res.get("error"):
                    result.message = f"Ouverture refusée : {res.get('error') if isinstance(res, dict) else res}"
                    break
                self._record(result, res)
                remaining = int(res.get("totalAvailable", remaining - 1))
                if remaining > 0 and result.opened < settings.max_packs:
                    pause(settings)
            if not result.message:
                result.message = (
                    f"Limite de {settings.max_packs} packs atteinte, il en reste {remaining}."
                    if remaining > 0 else "Tous les packs disponibles ont été ouverts."
                )
        return result

    def _pick_series(self, client: httpx.Client, settings: Settings) -> str:
        pinned = settings.get("WIKITCG_NET_SERIES")
        if pinned:
            return pinned
        series = _as_list(self._request(client, settings, "GET", "/api/series"), "series")
        collection = _as_list(self._request(client, settings, "GET", "/api/collection"), "collection")
        return choose_series(series, collection)

    def _open_mystery(self, client: httpx.Client, settings: Settings, result: SiteResult) -> None:
        """Le pack mystère se débloque environ toutes les 6 h ; un refus est normal."""
        if settings.get("WIKITCG_NET_MYSTERY") == "0":
            return
        try:
            res = self._request(
                client, settings, "POST", "/api/packs/open", json={"seriesId": "mystery"}
            )
        except SiteError as exc:
            if isinstance(exc, AuthError):
                raise
            log.info("Pack mystère indisponible : %s", exc)
            return
        if isinstance(res, dict) and not res.get("error") and res.get("cards"):
            self._record(result, res)
            log.info("Pack mystère ouvert.")

    @staticmethod
    def _record(result: SiteResult, res: dict) -> None:
        result.opened += 1
        for c in res.get("cards") or []:
            title = (c.get("article") or {}).get("title") or c.get("id") or "?"
            result.cards.append(Card(title=str(title), rarity=str(c.get("rarity") or "?")))

    # -- HTTP ---------------------------------------------------------------
    def _request(self, client: httpx.Client, settings: Settings, method: str, path: str,
                 **kwargs: Any) -> Any:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == MAX_RETRIES:
                    raise SiteError(f"Réseau indisponible ({exc.__class__.__name__}).") from exc
                time.sleep(2 ** attempt)
                continue
            self._remember_cookie(client, settings)
            if resp.status_code in (401, 403):
                raise AuthError(
                    f"{method} {path} → {resp.status_code} : cookie wtcg_session refusé ou expiré."
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_RETRIES:
                    raise SiteError(f"{method} {path} → {resp.status_code} après {attempt} essais.")
                wait = resp.headers.get("Retry-After", "")
                time.sleep(min(float(wait), 60) if wait.isdigit() else 2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise SiteError(f"{method} {path} → {resp.status_code} : {resp.text[:200]}")
            return resp.json() if resp.content else None
        raise SiteError("unreachable")  # pragma: no cover

    def _remember_cookie(self, client: httpx.Client, settings: Settings) -> None:
        """Le serveur fait tourner le cookie : on garde le dernier pour le prochain run."""
        fresh = ""
        for cookie in client.cookies.jar:
            if cookie.name == COOKIE and cookie.value:
                fresh = cookie.value
        if fresh and fresh != self._saved:
            self._saved = fresh
            path = self._state_file(settings)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fresh)
            log.info("Cookie de session rafraîchi par le serveur.")
