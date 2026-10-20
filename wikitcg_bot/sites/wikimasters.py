"""wiki-masters.com — session Supabase d'un profil Chrome dédié, puis `POST /api/packs/open`.

Le bot ne se connecte jamais lui-même : le formulaire est protégé par un Turnstile et le mot de
passe ne passe pas par lui. Vous vous connectez une fois à la main
(`python -m wikitcg_bot login wikimasters`, profil `.state/profiles/wikimasters`) ; le JavaScript du
site renouvelle ensuite le jeton Supabase dans ce profil, et `browser_page` réécrit les cookies dans
l'état de session à la fin de chaque run.

Le jeton de rafraîchissement de Supabase est à usage unique : l'export de cookies de votre navigateur
habituel ne doit jamais servir ici (`import_export = False`), sous peine de le déconnecter.

Les endpoints viennent du bundle JavaScript de la page /pulls (docs/exploration.md). Ceux des packs
spéciaux (`/api/packs/special`) n'y ont été que lus, jamais observés en réseau : on les traite avec
méfiance.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..browser import browser_page
from ..config import Settings
from ..models import AuthError, Card, SiteError, SiteResult
from .base import pause, session_store

log = logging.getLogger(__name__)

BASE_URL = "https://www.wiki-masters.com"
PULLS_PATH = "/pulls"
PROFILE = "wikimasters"
LOGIN_COMMAND = "python -m wikitcg_bot login wikimasters"

AUTH_MESSAGE = f"Session WikiMasters absente ou expirée : lancez « {LOGIN_COMMAND} »"
HUMAN_MESSAGE = (
    "WikiMasters exige une vérification humaine (Turnstile) : le bot ne la contourne pas. "
    f"Lancez « {LOGIN_COMMAND} », ouvrez un paquet à la main dans la fenêtre et validez-la."
)

# Cookie de session Supabase, coupé en morceaux `.0`, `.1` (limite de 4 Ko par cookie).
AUTH_COOKIE = re.compile(r"^sb-.+-auth-token(\.\d+)?$")
PACK_STATUS = re.compile(r"(\d+)\s*/\s*(\d+)\s+paquets?\s+disponibles?", re.IGNORECASE)
NEXT_LABEL = re.compile(r"Prochain dans", re.IGNORECASE)
NEXT_PACK = re.compile(r"Prochain dans\s*(\d+:\d{2})", re.IGNORECASE)
SUSPENDED = re.compile(r"compte suspendu", re.IGNORECASE)
# L'en-tête de la section des packs spéciaux, avec son libellé : « Gratuit · 1 / 6h » ou « Illimité ».
SPECIAL_OFFER = re.compile(r"Packs Spéciaux\s*(?:Gratuit|Illimité)", re.IGNORECASE)

PULLS_TIMEOUT_S = 15.0  # attente du stock sur /pulls
SPECIAL_WAIT_S = 4.0    # attente de la section des packs spéciaux, que la page charge après le stock

# Les seuls appels que le bot peut faire depuis la page. Tout le reste de l'API (pack PRO quotidien,
# « grâce » VIP, paiement, facturation, vérification humaine) est hors périmètre : en le refusant
# ici, une faute de frappe ne peut pas dépenser ni réclamer quoi que ce soit.
ALLOWED_CALLS = frozenset({
    ("POST", "/api/packs/open"),
    ("GET", "/api/packs/special"),
    ("POST", "/api/packs/special"),
})

# En-têtes par lesquels Cloudflare et Vercel annoncent qu'ils opposent un défi à la requête.
MITIGATION_HEADERS = ("cf-mitigated", "x-vercel-mitigated")

# `page.evaluate` ignore le délai par défaut de Playwright : sans ce signal, un `fetch` qui n'obtient
# jamais de réponse bloquerait le run (et le verrou des autres sites) jusqu'à la limite de la tâche
# planifiée, dont l'arrêt brutal ne réécrit ni les cookies renouvelés ni l'historique.
CALL_TIMEOUT_S = 30.0

# `fetch` depuis la page : même origine, cookies de session du profil, comme le fait le site.
_CALL_JS = """
async ({ method, path, payload, watch, timeoutMs }) => {
  const init = { method, credentials: 'include', signal: AbortSignal.timeout(timeoutMs) };
  if (payload != null) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(payload);
  }
  const r = await fetch(path, init);
  let body = null;
  try { body = await r.json(); } catch (e) {
    // Une réponse qui n'est pas du JSON est normale (404 HTML) ; un délai dépassé en cours de lecture non.
    if (e && (e.name === 'TimeoutError' || e.name === 'AbortError')) throw e;
  }
  const mitigated = watch.filter(n => r.headers.get(n)).map(n => n + ': ' + r.headers.get(n)).join(', ');
  return { status: r.status, url: r.url, body, mitigated };
}
"""


class StopRun(SiteError):
    """Arrêt volontaire : `run` rend ce qui a déjà été ouvert au lieu de lever l'erreur."""


class HumanVerificationRequired(StopRun):
    """Le site demande un Turnstile : on s'arrête, on ne le résout pas."""


class ChallengeDetected(StopRun):
    """Cloudflare ou Vercel oppose un défi anti-robot : on s'arrête, on ne le contourne pas."""


@dataclass(frozen=True)
class Stock:
    available: int
    maximum: int
    next_in: str | None = None  # « m:ss », tel qu'affiché par la page


@dataclass(frozen=True)
class Reply:
    status: int
    body: dict
    url: str = ""
    mitigated: str = ""  # « cf-mitigated: challenge », si le site oppose un défi

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def auth_refused(self) -> bool:
        """401 `{"error":"Non autorisé"}`, ou redirection vers /login (fetch la suit sans le dire)."""
        return self.status == 401 or urlsplit(self.url).path.startswith("/login")

    @property
    def human_check(self) -> bool:
        return bool(self.body.get("human_verification_required"))

    @property
    def error(self) -> str:
        return str(self.body.get("error") or f"HTTP {self.status}")

    @property
    def cards(self) -> list[dict]:
        cards = self.body.get("cards") if self.ok else None
        return [c for c in cards if isinstance(c, dict)] if isinstance(cards, list) else []

    @property
    def packs_remaining(self) -> int | None:
        value = self.body.get("packs_remaining")
        return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class SpecialPacks:
    pack_ids: tuple[str, ...]
    available: bool
    is_vip: bool = False
    next_available_at: str | None = None


def parse_card(raw: dict) -> Card:
    """Les cartes arrivent à plat ou imbriquées sous `card` selon l'endpoint."""
    inner = raw.get("card") if isinstance(raw.get("card"), dict) else {}
    title = (raw.get("wikipedia_title") or inner.get("wikipedia_title")
             or raw.get("title") or inner.get("title") or raw.get("name") or "?")
    rarity = raw.get("rarity") or inner.get("rarity") or "?"
    return Card(title=str(title), rarity=str(rarity).upper())


def parse_stock(text: str) -> Stock | None:
    """« 3 / 10 paquets disponibles », « Prochain dans 4:37 » : le texte de la page /pulls."""
    match = PACK_STATUS.search(text)
    if not match:
        return None
    following = NEXT_PACK.search(text)
    return Stock(int(match.group(1)), int(match.group(2)), following.group(1) if following else None)


def stock_synced(text: str, stock: Stock) -> bool:
    """Le stock affiché vient-il bien du profil ?

    La page écrit « 0 / 10 » aussi quand elle n'a pas pu lire le profil (`packs_remaining ?? 0`) : c'est
    la seule valeur de repli, donc tout stock non nul a forcément été lu. Seul le zéro est suspect, et
    un zéro réellement lu porte toujours « Prochain dans » (le libellé suit tout stock sous le
    maximum) : sans lui, « 0 / 10 » n'est pas un stock vide mais une page qui ne s'est pas synchronisée.
    Se limiter au zéro évite qu'un simple changement de formulation ne bloque tous les runs.
    """
    return stock.available > 0 or bool(NEXT_LABEL.search(text))


def special_offered(text: str) -> bool:
    """La page affiche-t-elle des packs spéciaux gratuits (ou illimités) ?

    La section est derrière un indicateur de build du site, éteint dans nos captures : le bot ne
    tente que ce que l'interface propose, il n'ouvre pas un pack que le propriétaire ne voit pas.
    """
    return bool(SPECIAL_OFFER.search(unicodedata.normalize("NFC", text)))


def as_reply(raw: object) -> Reply:
    raw = raw if isinstance(raw, dict) else {}
    body = raw.get("body")
    return Reply(int(raw.get("status") or 0), body if isinstance(body, dict) else {},
                 str(raw.get("url") or ""), str(raw.get("mitigated") or ""))


def parse_special(body: dict) -> SpecialPacks:
    """Réponse de `GET /api/packs/special` ; forme déduite du JS, donc lue sans rien supposer."""
    ids = []
    packs = body.get("packs")
    for pack in packs if isinstance(packs, list) else []:
        pack_id = pack.get("id") if isinstance(pack, dict) else pack
        if isinstance(pack_id, str) and pack_id:
            ids.append(pack_id)
    next_at = body.get("next_available_at")
    return SpecialPacks(tuple(ids), body.get("available") is True, body.get("is_vip") is True,
                        next_at if isinstance(next_at, str) else None)


def wait_text(moment: str | None, now: datetime | None = None) -> str | None:
    """« 5 h 12 min » avant l'instant ISO donné ; None s'il est illisible ou déjà passé."""
    if not moment:
        return None
    try:
        target = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    seconds = int((target - (now or datetime.now(timezone.utc))).total_seconds())
    if seconds <= 0:
        return None
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def auth_cookies(store) -> list[dict]:
    return [c for c in store.load() if AUTH_COOKIE.match(c["name"])]


def _note(result: SiteResult, text: str) -> None:
    result.message = f"{result.message} {text}".strip()


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=1000)
    except PlaywrightError:  # navigation en cours
        return ""


def _challenge(url: str, status: int, found: str) -> ChallengeDetected:
    """Un défi anti-robot n'est jamais contourné : on s'arrête en le décrivant précisément."""
    return ChallengeDetected(
        f"Défi anti-robot sur {urlsplit(url).path} (HTTP {status}, {found}) : "
        "le bot ne le contourne pas, ouvrez le site à la main."
    )


def _refuse_challenge(response) -> None:
    if response is None:
        return
    found = ", ".join(f"{n}: {response.headers[n]}" for n in MITIGATION_HEADERS if response.headers.get(n))
    if found:
        raise _challenge(response.url, response.status, found)


def enter_pulls(page: Page) -> tuple[str, Stock]:
    """Charge /pulls et attend que le stock s'affiche ; sans session, le site renvoie vers /login.

    La redirection est faite côté serveur (307, vérifié sans cookie), mais on surveille aussi l'URL
    pendant le chargement : le site pourrait la faire côté client une fois le jeton refusé.
    """
    response = page.goto(BASE_URL + PULLS_PATH, wait_until="domcontentloaded")
    _refuse_challenge(response)
    deadline = time.monotonic() + PULLS_TIMEOUT_S
    unsynced: Stock | None = None
    while time.monotonic() < deadline:
        if urlsplit(page.url).path.startswith("/login"):
            raise AuthError(AUTH_MESSAGE)
        text = _body_text(page)
        stock = parse_stock(text)
        if stock and stock_synced(text, stock):
            return text, stock
        unsynced = stock or unsynced
        page.wait_for_timeout(300)
    if unsynced:
        # Prendre ce « 0 / 10 » pour un stock vide ferait croire, à chaque run, que tout va bien.
        raise SiteError(
            f"Stock WikiMasters non synchronisé (« {unsynced.available} / {unsynced.maximum} » sans "
            "« Prochain dans ») : la page n'a pas pu lire le profil."
        )
    raise SiteError(
        f"Page des paquets WikiMasters inattendue (« {urlsplit(page.url).path} » sans stock affiché) : "
        "le site a-t-il changé ?"
    )


def call(page: Page, method: str, path: str, payload: dict | None = None) -> Reply:
    if (method, path) not in ALLOWED_CALLS:
        raise ValueError(f"Appel hors périmètre : {method} {path}")
    try:
        raw = page.evaluate(_CALL_JS, {"method": method, "path": path, "payload": payload,
                                       "watch": list(MITIGATION_HEADERS),
                                       "timeoutMs": int(CALL_TIMEOUT_S * 1000)})
    except PlaywrightError as exc:
        # Le signal de `fetch` fait lever un « TimeoutError » : on le dit plutôt que le nom générique.
        why = f"sans réponse après {CALL_TIMEOUT_S:.0f} s" if "TimeoutError" in str(exc) else exc.__class__.__name__
        raise SiteError(f"{method} {path} impossible depuis la page ({why}).") from exc
    return as_reply(raw)


def _check(reply: Reply, session: bool = True) -> None:
    """Lève l'arrêt qui s'impose si le site refuse la session, exige un Turnstile ou oppose un défi."""
    if reply.mitigated:
        raise _challenge(reply.url, reply.status, reply.mitigated)
    if not session:
        return
    if reply.auth_refused:
        raise AuthError(AUTH_MESSAGE)
    if reply.human_check:
        raise HumanVerificationRequired(HUMAN_MESSAGE)


def _record(result: SiteResult, reply: Reply) -> None:
    result.opened += 1
    result.cards.extend(parse_card(c) for c in reply.cards)


def open_regular(page: Page, settings: Settings, result: SiteResult, stock: Stock) -> None:
    """Ouvre les packs du stock, un à la fois, jusqu'à épuisement ou jusqu'à la limite du run."""
    remaining = stock.available
    if remaining <= 0:
        _note(result, "Aucun pack disponible" + (f" (prochain dans {stock.next_in})." if stock.next_in else "."))
        return
    while remaining > 0:
        if result.opened >= settings.max_packs:
            _note(result, f"Limite de {settings.max_packs} packs atteinte, il en reste {remaining}.")
            return
        if result.opened:
            pause(settings)
        reply = call(page, "POST", "/api/packs/open")
        _check(reply)
        if not reply.cards:
            # Stock vide côté serveur : normal. Toute autre raison est un refus inattendu (compte
            # bloqué, limitation de débit…) : on arrête le run, sans tenter autre chose sur ce compte.
            if reply.packs_remaining == 0:
                _note(result, f"Plus de pack à ouvrir ({reply.error}).")
                return
            raise StopRun(f"Ouverture refusée ({reply.error}).")
        _record(result, reply)
        left = reply.packs_remaining
        remaining = left if left is not None else remaining - 1
    _note(result, "Tous les packs disponibles ont été ouverts.")


def special_on_page(page: Page) -> bool:
    """Attend (un peu) que /pulls affiche la section des packs spéciaux, que le site charge après le stock."""
    deadline = time.monotonic() + SPECIAL_WAIT_S
    while True:
        if special_offered(_body_text(page)):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(300)


def open_special(page: Page, settings: Settings, result: SiteResult) -> None:
    """Pack spécial gratuit (1 toutes les 6 h) : au plus un par run, même si le site en offrait plus.

    La fonctionnalité est derrière un indicateur de build du site : si /pulls n'affiche pas la section,
    on n'appelle rien (l'interface ne l'offre pas) ; une réponse qui n'est pas du JSON de succès
    signifie simplement qu'elle est absente, ce n'est pas une erreur. Le pack spécial compte dans la
    limite de packs du run.
    """
    if result.opened >= settings.max_packs:
        log.info("Pack spécial WikiMasters non tenté : limite de %d packs atteinte.", settings.max_packs)
        return
    if not special_on_page(page):
        log.debug("Packs spéciaux WikiMasters non proposés par la page /pulls.")
        return
    reply = call(page, "GET", "/api/packs/special")
    # Lecture d'un endpoint non vérifié, alors que /pulls vient de s'afficher : un refus n'est pas
    # une session perdue, juste l'absence de la fonctionnalité.
    _check(reply, session=False)
    if not reply.ok:
        log.debug("Packs spéciaux WikiMasters indisponibles (HTTP %s).", reply.status)
        return
    special = parse_special(reply.body)
    if not special.pack_ids:
        return
    if not special.available:
        wait = wait_text(special.next_available_at)
        _note(result, "Pack spécial : prochain " + (f"dans {wait}." if wait else "plus tard."))
        return
    if settings.dry_run:
        _note(result, "Pack spécial disponible.")
        return
    if result.opened:
        pause(settings)
    reply = call(page, "POST", "/api/packs/special", {"packId": special.pack_ids[0]})
    _check(reply)
    if not reply.cards:
        # Optionnel et non vérifié : un refus se signale sans faire échouer tout le site.
        log.warning("Pack spécial WikiMasters refusé : %s", reply.error)
        _note(result, f"Pack spécial refusé ({reply.error}).")
        return
    _record(result, reply)
    _note(result, "Pack spécial ouvert.")


class WikiMasters:
    name = "wikimasters"
    label = "WikiMasters"
    hosts = ("www.wiki-masters.com",)
    login_url = f"{BASE_URL}/login"
    # Jeton de rafraîchissement à usage unique : réutiliser l'export de cookies du navigateur habituel
    # déclencherait la détection de réutilisation et déconnecterait ce dernier.
    import_export = False

    def has_profile(self, settings: Settings) -> bool:
        return (settings.profiles_dir / PROFILE).is_dir()

    def configured(self, settings: Settings) -> bool:
        # Comme `run` : une fois le profil créé, le site reste dans les runs planifiés. Si le site
        # efface les cookies d'une session morte, l'état n'en a plus mais le rappel « login » (code 2)
        # doit continuer, au lieu que WikiMasters disparaisse en silence.
        return bool(auth_cookies(session_store(settings, self))) or self.has_profile(settings)

    def session_summary(self, settings: Settings) -> str:
        cookies = auth_cookies(session_store(settings, self))
        if not cookies:
            return f"aucune session (lancez « {LOGIN_COMMAND} »)"
        expiries = [c["expires"] for c in cookies if (c.get("expires") or -1) > 0]
        if not expiries:
            return "session enregistrée dans le profil Chrome dédié"
        days = (min(expiries) - time.time()) / 86400
        return f"session enregistrée dans le profil Chrome dédié (cookie valable encore {days:.0f} j)"

    def run(self, settings: Settings) -> SiteResult:
        result = SiteResult(self.name, self.label)
        store = session_store(settings, self)
        if not self.configured(settings):
            raise AuthError(AUTH_MESSAGE)
        with browser_page(settings, store=store, profile=PROFILE) as page:
            text, stock = enter_pulls(page)
            result.packs_before, result.packs_max = stock.available, stock.maximum
            if SUSPENDED.search(text):
                result.ok = False
                result.message = "Compte suspendu : le site bloque les ouvertures de packs."
                return result
            try:
                if settings.dry_run:
                    _note(result, "Simulation : rien n'a été ouvert.")
                    if stock.next_in:
                        _note(result, f"Prochain pack dans {stock.next_in}.")
                else:
                    open_regular(page, settings, result, stock)
                open_special(page, settings, result)
            except StopRun as exc:
                result.ok = False
                _note(result, str(exc))
            except AuthError as exc:
                if not result.opened:
                    raise
                # Des packs sont déjà ouverts : on rend le résultat pour que l'historique les garde.
                result.ok, result.auth_failed = False, True
                _note(result, str(exc))
            except SiteError as exc:
                if not result.opened:
                    raise
                # Réseau coupé, page qui navigue… : les packs déjà consommés ne doivent pas être perdus
                # (un SiteError qui s'échappe de `run` donne un résultat vide, sans historique).
                result.ok = False
                _note(result, f"Interrompu : {exc}")
        return result
