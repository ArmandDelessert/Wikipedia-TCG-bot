"""wikitcg.net — API HTTP directe, authentifiée par le cookie `wtcg_session`.

Pas de navigateur : l'API JSON du site répond sans défi depuis un poste résidentiel (ce qu'un
runner GitHub ne peut pas faire, d'où l'exécution locale). Endpoints : `/api/packs/status`,
`/api/series`, `/api/collection`, `/api/packs/open`, d'abord repris du projet
dochreidte/wikitcg-completer puis vérifiés avec le compte du propriétaire.

Le bot n'ouvre que des packs gratuits. Il n'appelle jamais `/api/packs/regen` (recharge payante
en encre) ni aucun endpoint d'achat, et ne cherche pas à franchir un défi Cloudflare : seul le
cookie `wtcg_session` est envoyé, jamais le `cf_clearance` que Cloudflare a délivré à Chrome.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any

import httpx

from ..config import Settings
from ..cookies import Cookie
from ..models import AuthError, Card, SiteError, SiteResult
from ..sessions import SessionStore
from .base import pause, session_store

log = logging.getLogger(__name__)

BASE_URL = "https://wikitcg.net"
COOKIE = "wtcg_session"
# Le pack mystère s'ouvre comme une série (`{"seriesId": "mystery"}`) mais /api/series ne le liste pas.
MYSTERY = "mystery"
# Le pack mystère se débloque environ toutes les 6 h : inutile d'en redemander un juste après un succès.
MYSTERY_COOLDOWN = 6 * 3600
# Le JWT dure 14 jours et le site ne le renouvelle pas à chaque appel : on prévient avant la fin.
RENEW_WITHIN_DAYS = 3
USER_AGENT = "Mozilla/5.0 (compatible; wikitcg-bot/1.0; outil personnel)"
MAX_RETRIES = 3
# Session absente ou invalide : le site répond 401 `{"error": "unauthorized"}`. Un 403 JSON est en
# général un refus métier (pack mystère : `{"error": "mystery_not_eligible"}`), pas un cookie périmé.
AUTH_ERRORS = {"unauthorized", "unauthenticated", "invalid_token", "session_expired"}
# Un code d'erreur du site est un identifiant court ; tout autre texte pourrait citer des données du compte.
SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,60}")


class Refusal(SiteError):
    """Refus métier du site (JSON `{"error": "…"}` en 4xx, ou en 200), pas un échec de session."""


class ServerError(SiteError):
    """Le serveur a répondu 5xx : la requête a peut-être été traitée, on ne la rejoue pas (POST)."""


# -- session ----------------------------------------------------------------------------------
def jwt_expiry(token: str) -> int | None:
    """Date d'expiration (epoch) d'un JWT, ou None s'il n'est pas décodable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if exp is not None else None
    except (IndexError, ValueError, TypeError, AttributeError):
        return None


def session_expiry(cookie: Cookie) -> float | None:
    """Échéance réelle de la session : celle du JWT, sinon celle que le cookie annonce."""
    exp = jwt_expiry(cookie.get("value") or "")
    if exp is not None:
        return exp
    declared = cookie.get("expires")
    return declared if declared and declared > 0 else None


def expiry_note(expiry: float | None, now: float) -> str:
    """Avertissement quand la session touche à sa fin, pour laisser le temps de la renouveler."""
    if expiry is None:
        return ""
    days = (expiry - now) / 86400
    if days <= 0:
        return "expirée"
    if days < RENEW_WITHIN_DAYS:
        return (f"à renouveler (il reste {days:.1f} j) : refaites un export de cookies "
                "ou lancez « login wikitcg_net »")
    return ""


def merge_cookies(stored: list[Cookie], fresh: list[Cookie]) -> tuple[list[Cookie], bool]:
    """Reporte dans les cookies stockés ce que le site a renouvelé (`fresh` = le pot du client).

    Un cookie déjà connu garde son identité (domaine, chemin, drapeaux) : seuls sa valeur et son
    échéance changent. Les cookies que le site vient de poser sont ajoutés, les autres restent
    intacts.
    """
    merged = [dict(c) for c in stored]
    latest: dict[str, Cookie] = {}
    for cookie in fresh:
        # Deux cookies de même nom (l'ancien et le nouveau) : on garde celui qui dure le plus.
        known = latest.get(cookie["name"])
        if known is None or (session_expiry(cookie) or 0) >= (session_expiry(known) or 0):
            latest[cookie["name"]] = cookie
    changed = False
    for name, new in latest.items():
        current = next((c for c in merged if c["name"] == name), None)
        if current is None:
            merged.append(dict(new))
            changed = True
        elif current["value"] != new["value"]:
            current["value"] = new["value"]
            # L'échéance d'un JWT est écrite dedans ; sinon on prend celle du Set-Cookie (ou « session »).
            current["expires"] = (jwt_expiry(new["value"]) if name == COOKIE else None) or new.get("expires") or -1
            changed = True
    return merged, changed


def jar_cookies(client: httpx.Client) -> list[Cookie]:
    """Les cookies du client, au format des cookies stockés."""
    return [{
        "name": c.name, "value": c.value or "", "domain": c.domain, "path": c.path or "/",
        "expires": int(c.expires) if c.expires else -1,
        "httpOnly": c.has_nonstandard_attr("HttpOnly"), "secure": bool(c.secure),
    } for c in client.cookies.jar]


# -- réponses du site -------------------------------------------------------------------------
def is_challenge(headers: httpx.Headers, body: str = "") -> bool:
    """Défi Cloudflare : en-tête `cf-mitigated: challenge`, ou page HTML « Just a moment »."""
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if "html" in headers.get("content-type", "").lower():
        head = body[:20_000].lower()
        return "<title>just a moment" in head or "_cf_chl_opt" in head
    return False


def error_code(resp: httpx.Response) -> str:
    """Code d'erreur d'une réponse JSON `{"error": "…"}` (vide si le corps n'est pas de ce format)."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) else ""


def shown_code(code: Any) -> str:
    """Code d'erreur à afficher : un identifiant court, sinon rien du texte reçu (il va dans les logs)."""
    return code if isinstance(code, str) and SAFE_CODE.fullmatch(code) else "code d'erreur non reconnu"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def free_stock(status: dict) -> int:
    """Packs gratuits en réserve ; les packs achetés (`paidPacks`) ne sont jamais comptés."""
    if "freePacks" in status:
        return max(_int(status["freePacks"]), 0)
    return max(_int(status.get("totalAvailable")) - _int(status.get("paidPacks")), 0)


def regen_text(next_regen_ms: Any, now: float) -> str:
    """Délai avant le prochain pack gratuit (« 1h 58m »), vide si inconnu ou déjà échu."""
    try:
        seconds = int(next_regen_ms / 1000 - now)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def stock_notes(status: dict) -> list[str]:
    """Ce que le bot laisse volontairement de côté, à signaler au propriétaire."""
    notes = []
    if _int(status.get("paidPacks")) > 0:
        notes.append(f"{_int(status['paidPacks'])} pack(s) acquis non ouvert(s) (le bot n'ouvre que les gratuits)")
    waiting = status.get("completionPacks")
    if isinstance(waiting, list) and waiting:
        notes.append(f"{len(waiting)} pack(s) de complétion à ouvrir à la main")
    return notes


def _as_list(data: Any, key: str) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get(key)
        if isinstance(inner, list):
            return inner
    return []


def choose_series(series: list[dict], collection: list[dict], pinned: str = "") -> str:
    """Série à ouvrir : celle imposée, sinon celle où il manque le plus de cartes.

    À égalité la première de la liste l'emporte (choix stable d'un run à l'autre). Le pack mystère
    n'est jamais une série ordinaire.
    """
    if pinned:
        return pinned
    owned = {e.get("series_id"): _int(e.get("owned")) for e in collection if isinstance(e, dict)}
    best_id, best_missing = None, None
    for s in series:
        sid = s.get("id") if isinstance(s, dict) else None
        if not sid or sid == MYSTERY:
            continue
        missing = _int(s.get("cardCount")) - owned.get(sid, 0)
        if best_missing is None or missing > best_missing:
            best_id, best_missing = sid, missing
    if best_id is None:
        raise SiteError("Aucune série ouvrable trouvée (WIKITCG_NET_SERIES peut en imposer une).")
    return best_id


def parse_cards(res: dict) -> list[Card]:
    """Cartes d'une ouverture. Tolérant : le pack est déjà ouvert, une forme inattendue ne doit pas
    faire perdre le reste de l'historique."""
    found = res.get("cards")
    if not isinstance(found, list):
        return []
    cards = []
    for c in found:
        if not isinstance(c, dict):
            continue
        article = c.get("article")
        title = (article.get("title") if isinstance(article, dict) else None) or c.get("id") or "?"
        cards.append(Card(title=str(title), rarity=str(c.get("rarity") or "?")))
    return cards


def opened_ok(res: Any) -> bool:
    """Une ouverture réussie renvoie des cartes ; un refus renvoie `{"error": …}`, parfois en 200."""
    return isinstance(res, dict) and not res.get("error") and isinstance(res.get("cards"), list) and bool(res["cards"])


class WikiTcgNet:
    name = "wikitcg_net"
    label = "wikitcg.net"
    hosts = ("wikitcg.net",)
    login_url = "https://wikitcg.net/"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport

    # -- configuration ------------------------------------------------------
    def configured(self, settings: Settings) -> bool:
        return session_store(settings, self).configured()

    def session_summary(self, settings: Settings) -> str:
        store = session_store(settings, self)
        try:
            summary = store.describe((COOKIE,))
            session = next((c for c in store.load() if c["name"] == COOKIE), None)
        except (OSError, ValueError):
            return "état de session illisible"
        note = expiry_note(session_expiry(session), time.time()) if session else ""
        return f"{summary} — {note}" if note else summary

    # -- exécution ----------------------------------------------------------
    def run(self, settings: Settings) -> SiteResult:
        store = session_store(settings, self)
        cookies = store.load()
        session = next((c for c in cookies if c["name"] == COOKIE and c.get("value")), None)
        if session is None:
            raise AuthError("Aucun cookie wtcg_session valable (absent ou expiré) : refaites un export "
                            "dans Cookies/ ou lancez « login wikitcg_net ».")
        now = time.time()
        expiry = session_expiry(session)
        if expiry is not None and expiry <= now:
            raise AuthError("Le cookie wtcg_session a expiré : refaites un export dans Cookies/ "
                            "ou lancez « login wikitcg_net ».")
        if note := expiry_note(expiry, now):
            log.warning("%s : session %s.", self.label, note)

        result = SiteResult(self.name, self.label)
        with httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                     "Origin": BASE_URL, "Referer": BASE_URL + "/"},
            timeout=30.0,
            transport=self._transport,
        ) as client:
            # La session suffit à l'API. `cf_clearance` (et les autres cookies de l'export) reste dans
            # l'état local mais n'est jamais rejoué : Cloudflare l'a délivré à Chrome, pas à ce client.
            client.cookies.set(session["name"], session["value"], domain=session["domain"],
                               path=session.get("path") or "/")
            try:
                self._play(client, settings, result)
            finally:
                # Même si un appel échoue, un renouvellement déjà reçu doit survivre : c'est le
                # seul exemplaire valable du cookie.
                self._persist(store, cookies, client)
        return result

    def _play(self, client: httpx.Client, settings: Settings, result: SiteResult) -> None:
        status = self._request(client, "GET", "/api/packs/status")
        if not isinstance(status, dict):
            raise SiteError("Réponse inattendue de /api/packs/status.")
        free = free_stock(status)
        result.packs_before = free
        result.packs_max = status.get("maxFreePacks")
        now = time.time()
        notes = stock_notes(status)

        if settings.dry_run:
            parts = [f"Simulation : {free} pack(s) gratuit(s) prêt(s), rien n'a été ouvert."]
            if (wait := regen_text(status.get("nextRegenAt"), now)) and free < (result.packs_max or 0):
                parts.append(f"Prochain pack gratuit dans {wait}.")
            if self._mystery_due(settings):
                parts.append("Pack mystère à tenter.")
            result.message = " ".join(parts + notes)
            return

        try:
            mystery = self._try_mystery(client, settings, result)
            if mystery and "freePacks" in mystery:
                free = max(_int(mystery["freePacks"]), 0)  # au cas où le mystère entamerait le stock
            free, refusal = self._open_free_packs(client, settings, result, free)
        except Exception as exc:  # noqa: BLE001 — quelle que soit la cause, l'historique prime
            if not result.opened:
                raise
            # Ce qui a déjà été ouvert doit rester dans l'historique : on rend un résultat partiel.
            known = isinstance(exc, SiteError)
            detail = str(exc) if known else exc.__class__.__name__
            log.error("%s : arrêt après %d pack(s) : %s", self.label, result.opened, detail, exc_info=not known)
            result.ok = False
            result.auth_failed = isinstance(exc, AuthError)
            result.message = f"Interrompu après {result.opened} pack(s) : {detail}"
            return

        if refusal:
            log.warning("%s : ouverture refusée : %s", self.label, refusal)
            message = f"Ouverture refusée par le site : {refusal}."
        elif free > 0:
            message = f"Limite de {settings.max_packs} packs atteinte, il en reste {free}."
        elif result.opened:
            message = "Tous les packs gratuits disponibles ont été ouverts."
        else:
            wait = regen_text(status.get("nextRegenAt"), now)
            message = "Aucun pack gratuit disponible" + (f" (prochain dans {wait})." if wait else ".")
        result.message = " ".join([message, *(f"{note}." for note in notes)])

    def _open_free_packs(self, client: httpx.Client, settings: Settings, result: SiteResult,
                         free: int) -> tuple[int, str]:
        """Ouvre les packs gratuits (au plus `max_packs` par run) ; rend ce qu'il en reste et,
        si le site a refusé une ouverture, la raison.

        Un refus n'est normal que si le stock a changé en cours de route (donc après une ouverture).
        Refusé d'emblée alors que le statut annonçait des packs, c'est une anomalie (série
        inconnue, API modifiée…) : l'ignorer laisserait chaque run passer pour un succès.
        """
        series_id = refusal = ""
        while free > 0 and result.opened < settings.max_packs:
            series_id = series_id or self._pick_series(client, settings)
            if result.opened:
                pause(settings)
            try:
                res = self._open_pack(client, series_id)
            except Refusal as exc:
                if not result.opened:
                    raise SiteError(f"Ouverture refusée alors que {free} pack(s) gratuit(s) étaient "
                                    f"annoncés : {exc}.") from exc
                refusal = str(exc)
                break
            self._record(result, res)
            free = max(_int(res["freePacks"]) if "freePacks" in res else free - 1, 0)
        return free, refusal

    def _pick_series(self, client: httpx.Client, settings: Settings) -> str:
        pinned = settings.get("WIKITCG_NET_SERIES")
        if pinned:
            return pinned
        series = _as_list(self._request(client, "GET", "/api/series"), "series")
        collection = _as_list(self._request(client, "GET", "/api/collection"), "collection")
        return choose_series(series, collection)

    # -- pack mystère -------------------------------------------------------
    @staticmethod
    def _mystery_file(settings: Settings):
        return settings.state_dir / "wikitcg_net_mystery.json"

    def _mystery_due(self, settings: Settings) -> bool:
        """Faut-il (re)demander le pack mystère ? Non s'il est désactivé ou si le dernier est trop récent."""
        if settings.get("WIKITCG_NET_MYSTERY") == "0":
            return False
        try:
            not_before = float(json.loads(self._mystery_file(settings).read_text(encoding="utf-8"))["not_before"])
        except (OSError, ValueError, KeyError, TypeError):
            return True
        return time.time() >= not_before

    def _try_mystery(self, client: httpx.Client, settings: Settings, result: SiteResult) -> dict | None:
        """Le pack mystère n'apparaît dans aucune lecture (ni statut, ni /api/series) : on le demande.

        Hors délai, le site répond 403 `mystery_not_eligible`, ce qui est normal. Pour ne pas le
        redemander à chaque run, on attend la fin du délai après chaque succès. Rend la réponse du
        site si le pack a été ouvert. Cette demande à l'aveugle ne doit pas empêcher les packs
        ordinaires : on tolère un refus JSON du site et une erreur serveur (la requête suivante
        échouera aussi si c'est général). Un blocage (HTML, 4xx sans code), un 429 ou une réponse
        inattendue arrête en revanche le run, au lieu d'enchaîner d'autres requêtes.
        """
        if not self._mystery_due(settings):
            return None
        try:
            res = self._open_pack(client, MYSTERY)
        except (Refusal, ServerError) as exc:
            level = logging.INFO if isinstance(exc, Refusal) else logging.WARNING
            log.log(level, "%s : pack mystère indisponible : %s", self.label, exc)
            return None
        self._record(result, res)
        log.info("%s : pack mystère ouvert.", self.label)
        # Le délai n'est qu'une économie de requêtes : ne pas perdre le pack s'il ne s'écrit pas.
        path = self._mystery_file(settings)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"not_before": int(time.time()) + MYSTERY_COOLDOWN}), encoding="utf-8")
        except OSError:
            log.warning("%s : délai du pack mystère non enregistré.", self.label, exc_info=True)
        return res

    def _open_pack(self, client: httpx.Client, series_id: str) -> dict:
        """Demande l'ouverture d'un pack et rend la réponse du site, qui contient les cartes.

        Lève `Refusal` si le site refuse, `SiteError` si la réponse n'est pas celle d'une ouverture :
        le pack a alors peut-être été ouvert, donc on ne rejoue rien.
        """
        res = self._request(client, "POST", "/api/packs/open", json={"seriesId": series_id})
        if opened_ok(res):
            return res
        if isinstance(res, dict) and res.get("error"):
            raise Refusal(f"POST /api/packs/open → 200 : {shown_code(res['error'])}")
        raise SiteError("POST /api/packs/open : réponse inattendue (le pack a peut-être été ouvert).")

    @staticmethod
    def _record(result: SiteResult, res: dict) -> None:
        result.opened += 1
        result.cards.extend(parse_cards(res))

    # -- HTTP ---------------------------------------------------------------
    def _request(self, client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                # Rejouer une ouverture dont la réponse s'est perdue pourrait en ouvrir deux.
                safe = method == "GET" or isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if not safe or attempt == MAX_RETRIES:
                    raise SiteError(f"Réseau indisponible ({exc.__class__.__name__}).") from exc
                time.sleep(2 ** attempt)
                continue
            html = "html" in resp.headers.get("content-type", "").lower()
            if is_challenge(resp.headers, resp.text if html else ""):
                raise SiteError(
                    f"{method} {path} → {resp.status_code} : défi Cloudflare (cf-mitigated). Le bot ne "
                    "le contourne pas : réessayez plus tard, ou depuis la connexion où le site s'ouvre "
                    "normalement dans Chrome."
                )
            code = error_code(resp)
            if resp.status_code == 401 or (resp.status_code == 403 and code in AUTH_ERRORS):
                raise AuthError(
                    f"{method} {path} → {resp.status_code} : cookie wtcg_session refusé ou expiré."
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                if (method != "GET" and resp.status_code >= 500) or attempt == MAX_RETRIES:
                    failure = ServerError if resp.status_code >= 500 else SiteError  # 429 : on ralentit, on s'arrête
                    raise failure(f"{method} {path} → {resp.status_code} après {attempt} essai(s).")
                wait = resp.headers.get("Retry-After", "")
                time.sleep(min(float(wait), 60) if wait.isdigit() else 2 ** attempt)
                continue
            if resp.status_code >= 400:
                if code:
                    # Un refus métier (« mystery_not_eligible »…) n'est pas un échec de session.
                    raise Refusal(f"{method} {path} → {resp.status_code} : {shown_code(code)}")
                # Sans code d'erreur du site, ce n'est pas lui qui parle : on n'en recopie rien (le
                # corps pourrait citer le compte) et on ne le prend surtout pas pour un simple refus.
                blocked = " : accès bloqué par le site ou par Cloudflare" if resp.status_code == 403 else ""
                raise SiteError(f"{method} {path} → {resp.status_code} sans code d'erreur JSON{blocked}.")
            try:
                return resp.json() if resp.content else None
            except ValueError as exc:
                raise SiteError(f"{method} {path} : réponse qui n'est pas du JSON.") from exc
        raise SiteError("unreachable")  # pragma: no cover

    def _persist(self, store: SessionStore, stored: list[Cookie], client: httpx.Client) -> None:
        """Garde dans la session locale les cookies que le site a renouvelés pendant ce run."""
        try:
            merged, changed = merge_cookies(stored, jar_cookies(client))
            if changed:
                store.save(merged)
                log.info("%s : cookie de session renouvelé par le site, enregistré.", self.label)
        except Exception:  # ne pas masquer l'erreur du run qui nous a amenés ici
            log.warning("%s : session non enregistrée.", self.label, exc_info=True)
