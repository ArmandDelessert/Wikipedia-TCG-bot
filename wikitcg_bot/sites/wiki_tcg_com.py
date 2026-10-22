"""wiki-tcg.com — état par l'API, ouverture en pilotant la vraie interface avec Chrome.

L'état se lit sans navigateur : `GET /api/v1/battement?parts=compte` donne `player.packsStored`
(stock, plafond 10, +1 toutes les 15 min), `player.dust` et `player.speciaux`. L'ouverture est une
Server Action Next.js (`openPack`) dont l'identifiant change à chaque déploiement : on clique donc
sur le sachet de `/fr/boosters` au lieu de la rejouer.

Le booster est consommé au clic (la Server Action répond en ~250 ms avec les cartes) ; la scène de
révélation qui suit est purement visuelle et ne refait aucun appel. Les cartes se lisent dans le DOM :
rareté dans `data-rarity` de `.flipper__slot`, titre dans `.tcg__name`.

La session est le cookie `wikitcg_session` (30 jours) : la connexion passe par Google/Firebase, le bot
ne peut pas la refaire seul (`python -m wikitcg_bot login wiki_tcg_com` ou un export de cookies).
"""
from __future__ import annotations

import email.utils
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response

from ..browser import browser_page
from ..config import Settings
from ..cookies import Cookie, header
from ..models import AuthError, Card, SiteError, SiteResult
from .base import pause, session_store

log = logging.getLogger(__name__)

BASE_URL = "https://wiki-tcg.com"
BOOSTERS_PATH = "/fr/boosters"
STATUS_PATH = "/api/v1/battement?parts=compte,connexion"
SESSION_COOKIE = "wikitcg_session"
PACK_MAX = 10  # plafond de la réserve
USER_AGENT = "Wikipedia-TCG-bot (client personnel)"
MAX_RETRIES = 3

# La fenêtre « Récompense de connexion » recouvre la page tant qu'on n'a pas cliqué sur « Récupérer »
# (`POST /api/v1/connexion`). Le propriétaire a autorisé la réclamation : le bot la récupère, une fois par
# jour, quand l'état annonce `connexion.aVoir`. Les clics se font par évènement DOM (`dispatch_event`),
# jamais au clavier (Échap ou un clic à côté déclencheraient aussi la réclamation).
_REWARD_BUTTON = ".cnx__bouton"

_SACHET = "button.sachet[aria-label='Ouvrir']"
_SPECIAUX = ".speciaux button.special"
# La barre sous la scène change de contenu : « Tout révéler » tant que des cartes sont face cachée, puis le
# bilan avec « Ouvrir un autre booster » et « Voir ma collection » (qui quitte la page : à ne pas cliquer).
_REVEAL_ALL = ".opener__bar:has(.opener__hint) button"
_AGAIN = ".opener__bar:has(.bilan) button.btn:not(.btn--ghost)"

_CARDS_JS = """
() => [...document.querySelectorAll('.opener .flipper__slot')].map(slot => ({
  rarity: slot.dataset.rarity || '',
  name: slot.querySelector('.tcg__name')?.textContent || '',
}))
"""

# Le clic a été pris en compte : la scène a quitté l'accueil (ou affiché une erreur). Le HTML du sachet
# existe avant l'hydratation de la page, mais son gestionnaire de clic non : un clic trop tôt est perdu.
_CLICK_TAKEN_JS = """
() => {
  const opener = document.querySelector('.opener');
  return !!opener && (opener.dataset.phase !== 'idle' || !!opener.querySelector('.auth__error'));
}
"""

# 'reveal' quand le tirage est arrivé ; 'refus' quand le site l'a refusé (la scène revient à l'accueil
# avec un message d'erreur, quel que soit le motif).
_AFTER_CLICK_JS = """
() => {
  const opener = document.querySelector('.opener');
  if (!opener) return false;
  if (opener.dataset.phase === 'reveal') return 'reveal';
  if (opener.dataset.phase === 'idle' && opener.querySelector('.auth__error')) return 'refus';
  return false;
}
"""

_CHALLENGE_TITLE = re.compile(r"just a moment|un instant|attention required|checking your browser", re.I)
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # un « token » RFC 6265


# -- fonctions pures -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Status:
    packs: int
    dust: int = 0
    speciaux: dict[str, int] = field(default_factory=dict)
    reward: bool = False  # « Récompense de connexion » du jour à récupérer

    @property
    def total(self) -> int:
        return self.packs + sum(self.speciaux.values())


def parse_speciaux(raw: Any) -> dict[str, int]:
    """`{sorte: nombre}` depuis la liste `[{sorte, nombre}]` du site (ou un dict déjà de cette forme)."""
    if isinstance(raw, list):
        raw = [(e.get("sorte"), e.get("nombre")) for e in raw if isinstance(e, dict)]
    elif isinstance(raw, dict):
        raw = list(raw.items())
    else:
        raw = []
    found: dict[str, int] = {}
    for sorte, nombre in raw:
        if isinstance(sorte, str) and isinstance(nombre, int) and nombre > 0:
            found[sorte] = found.get(sorte, 0) + nombre
    return found


def parse_status(payload: Any) -> Status:
    """Lit la réponse de `battement?parts=compte`. Une session refusée répond 200 avec `player: null` ; tout
    autre `statut` de la partie (le client du site fabrique un 500 quand elle manque) est une panne passagère,
    pas un cookie expiré : on ne doit pas pousser le propriétaire à se reconnecter pour ça."""
    try:
        compte = payload["parts"]["compte"]
        statut = compte.get("statut")
        player = compte["corps"]["player"] if statut == 200 else None
    except (KeyError, TypeError, AttributeError) as exc:
        raise SiteError("Réponse de l'état du compte inattendue (le site a-t-il changé ?).") from exc
    if statut in (401, 403) or (statut == 200 and player is None):
        raise AuthError("Cookie wikitcg_session refusé ou expiré : reconnectez-vous "
                        "(python -m wikitcg_bot login wiki_tcg_com) ou refaites un export de cookies.")
    if statut != 200:
        raise SiteError(f"Partie « compte » de l'état en erreur (statut {statut}).")
    if not isinstance(player, dict):
        raise SiteError("Réponse de l'état du compte avec un `player` inattendu (le site a-t-il changé ?).")
    packs = player.get("packsStored")
    if not isinstance(packs, int) or isinstance(packs, bool):
        raise SiteError("Réponse de l'état du compte sans `packsStored`.")
    dust = player.get("dust")
    return Status(packs, dust if isinstance(dust, int) else 0, parse_speciaux(player.get("speciaux")),
                  reward=_reward_waiting(payload))


def _reward_waiting(payload: Any) -> bool:
    """`parts.connexion.corps.connexion.aVoir` ; tout autre cas (partie absente, en erreur) vaut non."""
    try:
        return payload["parts"]["connexion"]["corps"]["connexion"]["aVoir"] is True
    except (KeyError, TypeError):
        return False


def parse_cards(raw: list[dict]) -> list[Card]:
    """Cartes de la scène de révélation (`[{rarity, name}]`), dans l'ordre d'affichage."""
    return [Card(title=(c.get("name") or "").strip() or "?", rarity=(c.get("rarity") or "").upper() or "?")
            for c in raw if isinstance(c, dict)]


def _expiry(attrs: dict[str, str], now: float) -> float:
    """Échéance d'un cookie (`Max-Age` l'emporte sur `Expires`) ; -1 pour un cookie de session."""
    max_age = attrs.get("max-age", "")
    if re.fullmatch(r"-?[0-9]+", max_age):
        return now + int(max_age)
    if not attrs.get("expires"):
        return -1
    try:
        moment = email.utils.parsedate_to_datetime(attrs["expires"])
        if moment.tzinfo is None:  # les dates HTTP sont en GMT
            moment = moment.replace(tzinfo=timezone.utc)
        # Une date passée (1970 pour une suppression) ne doit jamais valoir 0 : `is_expired` y verrait un
        # cookie de session, donc un cookie vide qui survivrait à la sauvegarde.
        return max(moment.timestamp(), 1.0)
    except (TypeError, ValueError, OverflowError):
        return -1


def parse_set_cookies(headers: list[str], host: str, now: float | None = None) -> list[Cookie]:
    """Cookies posés par des en-têtes `Set-Cookie`, au format Playwright (échéance -1 : session).

    Découpé à la main : `SimpleCookie` jette l'en-tête entier devant un attribut inconnu sans valeur
    (`Partitioned`) et fait un faux cookie d'un attribut inconnu avec valeur (`Priority=High`)."""
    now = time.time() if now is None else now
    cookies: list[Cookie] = []
    for raw in headers:
        pair, *rest = raw.split(";")
        name, equals, value = pair.partition("=")
        name = name.strip()
        if not equals or not _COOKIE_NAME.fullmatch(name):
            continue
        attrs = {key.strip().lower(): val.strip() for key, _, val in (a.partition("=") for a in rest)}
        domain = attrs.get("domain", "")
        cookies.append({
            "name": name, "value": value.strip(),
            "domain": ("." + domain.lstrip(".")) if domain else host,  # sans `Domain` : propre à l'hôte
            "path": attrs.get("path") or "/", "expires": _expiry(attrs, now),
            "httpOnly": "httponly" in attrs, "secure": "secure" in attrs,
        })
    return cookies


def merge_cookies(current: list[Cookie], updates: list[Cookie]) -> list[Cookie]:
    """`updates` remplace les cookies de même nom/domaine/chemin (un échu sera écarté à la sauvegarde)."""
    def key(c: Cookie) -> tuple[str, str, str]:
        return c["name"], c["domain"].lstrip("."), c["path"]
    replaced = {key(c) for c in updates}
    return [c for c in current if key(c) not in replaced] + updates


def looks_like_challenge(headers: dict[str, str], title: str) -> bool:
    """Page de défi Cloudflare (`cf-mitigated: challenge`, ou titre typique) : le bot s'arrête."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get("cf-mitigated", "").lower() == "challenge" or bool(_CHALLENGE_TITLE.search(title))


# -- site ------------------------------------------------------------------------------------------
def _url(path: str) -> str:
    return BASE_URL + path


class WikiTcgCom:
    name = "wiki_tcg_com"
    label = "WikiTCG (wiki-tcg.com)"
    hosts = ("wiki-tcg.com",)
    login_url = "https://wiki-tcg.com/fr/connexion"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport

    # -- configuration ----------------------------------------------------------------------------
    def configured(self, settings: Settings) -> bool:
        return session_store(settings, self).configured()

    def session_summary(self, settings: Settings) -> str:
        return session_store(settings, self).describe((SESSION_COOKIE,))

    # -- exécution --------------------------------------------------------------------------------
    def run(self, settings: Settings) -> SiteResult:
        store = session_store(settings, self)
        result = SiteResult(self.name, self.label, packs_max=PACK_MAX)
        status = self._status(store)
        result.packs_before = status.packs
        log.info("%s : %d booster(s) en réserve%s%s.", self.label, status.packs,
                 f", dont {sum(status.speciaux.values())} spécial(aux)" if status.speciaux else "",
                 ", récompense de connexion à récupérer" if status.reward else "")
        extra = f", dont {sum(status.speciaux.values())} spécial(aux)" if status.speciaux else ""
        if settings.dry_run:
            result.message = f"Simulation : rien n'a été ouvert ({status.total} booster(s) disponible(s){extra})."
            return result
        if status.total == 0 and not status.reward:
            result.message = "Aucun booster disponible (un nouveau toutes les 15 minutes)."
            return result

        session_before = store.value(SESSION_COOKIE)
        try:
            with browser_page(settings, store=store) as page:
                self._open_all(page, settings, result, status)
        except PlaywrightError as exc:
            self._interrupted(result, SiteError(f"Navigateur : {(str(exc).splitlines() or ['erreur'])[0]}"))
        except SiteError as exc:
            self._interrupted(result, exc)
        if store.value(SESSION_COOKIE) != session_before:
            log.info("wikitcg_session renouvelé par le site (%s).", store.describe((SESSION_COOKIE,)))

        if result.ok:
            left = self._remaining(store)
            reste = f", il en reste {left}" if left else ""
            if result.opened >= settings.max_packs and left != 0:
                result.message = f"Limite de {settings.max_packs} booster(s) atteinte{reste}."
            elif left is None:  # on ne sait pas ce qu'il reste : ne pas prétendre que tout est ouvert
                result.message = "Ouverture terminée (stock restant illisible)."
            elif left > 1:
                # Un booster peut tomber pendant le run (+1 toutes les 15 min), pas deux : la boucle s'est arrêtée
                # trop tôt (interface modifiée ?). Un échec, sinon les packs s'accumulent jusqu'au plafond sans alerte.
                result.ok = False
                result.message = (f"Arrêt prématuré après {result.opened} booster(s) : le site en annonce encore "
                                  f"{left} (l'interface a-t-elle changé ?).")
            elif left:
                result.message = f"Ouverture terminée{reste} d'après le site."
            else:
                result.message = "Tous les boosters disponibles ont été ouverts."
        return result

    @staticmethod
    def _interrupted(result: SiteResult, exc: SiteError) -> None:
        """Une erreur sans aucune ouverture se propage ; sinon on garde ce qui a été ouvert."""
        if not result.opened:
            raise exc
        result.ok = False
        result.auth_failed = isinstance(exc, AuthError)
        result.message = f"Interrompu après {result.opened} booster(s) : {exc}"

    def _remaining(self, store) -> int | None:
        """Stock restant après le run ; une panne ici ne doit pas faire échouer des ouvertures réussies."""
        try:
            return self._status(store).total
        except SiteError as exc:
            log.warning("État final illisible : %s", exc)
            return None

    # -- état (HTTP, sans navigateur) -------------------------------------------------------------
    def _status(self, store) -> Status:
        host = urlsplit(BASE_URL).hostname or self.hosts[0]
        cookies = store.load()
        if not cookies:
            raise AuthError("Aucune session wiki-tcg.com : placez un export de cookies dans Cookies/ "
                            "ou lancez « python -m wikitcg_bot login wiki_tcg_com ».")
        headers = {"Cookie": header(cookies, host), "Accept": "application/json", "User-Agent": USER_AGENT}
        with httpx.Client(timeout=30.0, transport=self._transport) as client:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = client.get(_url(STATUS_PATH), headers=headers)
                except httpx.TransportError as exc:
                    if attempt == MAX_RETRIES:
                        raise SiteError(f"Réseau indisponible ({exc.__class__.__name__}).") from exc
                    time.sleep(2 * attempt)
                    continue
                # Un défi peut porter un 5xx : l'arrêt passe avant le retry, on ne rejoue pas une requête contestée.
                if looks_like_challenge(dict(resp.headers), ""):
                    raise SiteError("Défi anti-bot détecté sur l'API : arrêt, aucun contournement n'est tenté.")
                if resp.status_code >= 500 and attempt < MAX_RETRIES:
                    time.sleep(2 * attempt)
                    continue
                break
        if resp.status_code in (401, 403):
            raise AuthError(f"État du compte refusé ({resp.status_code}) : cookie wikitcg_session expiré ?")
        if resp.status_code != 200:
            raise SiteError(f"État du compte → HTTP {resp.status_code}.")
        renewed = parse_set_cookies(resp.headers.get_list("set-cookie"), host)
        if renewed:
            store.save(merge_cookies(cookies, renewed))
            log.info("Cookies renouvelés par l'API d'état : %s.", ", ".join(c["name"] for c in renewed))
        try:
            return parse_status(resp.json())
        except ValueError as exc:
            raise SiteError("Réponse de l'état du compte illisible (JSON attendu).") from exc

    # -- ouverture (navigateur) -------------------------------------------------------------------
    def _open_all(self, page: Page, settings: Settings, result: SiteResult, status: Status) -> None:
        self._goto_boosters(page)
        claimed = status.reward and self._claim_login_reward(page)
        if claimed:
            result.message = "Récompense de connexion récupérée."
        specials = 0
        while result.opened < settings.max_packs:
            sorte = self._prepare(page)
            if sorte is None:
                if not result.opened and status.total and not claimed:  # l'API annonçait des boosters : « rien à ouvrir » serait un faux succès
                    raise SiteError("Aucun sachet ouvrable alors que l'API annonce des boosters "
                                    "(stock vidé entre-temps, ou le site a-t-il changé ?).")
                break
            result.add_pack(self._open_one(page))
            specials += bool(sorte)
            if result.opened >= settings.max_packs or not self._can_continue(page):
                break
            pause(settings)
            self._again(page)
        if specials:
            log.info("%d booster(s) spécial(aux) ouvert(s).", specials)

    @staticmethod
    def _claim_login_reward(page: Page) -> bool:
        """Clique sur « Récupérer » dans la fenêtre de récompense de connexion. Faux si elle n'apparaît pas."""
        try:
            page.wait_for_selector(_REWARD_BUTTON, timeout=8_000)
        except PlaywrightError:
            log.info("Récompense de connexion annoncée mais fenêtre absente (déjà récupérée ailleurs ?).")
            return False
        page.locator(_REWARD_BUTTON).dispatch_event("click")
        try:
            page.wait_for_selector(_REWARD_BUTTON, state="detached", timeout=10_000)
        except PlaywrightError as exc:
            raise SiteError("La récompense de connexion ne s'est pas fermée après « Récupérer ».") from exc
        log.info("Récompense de connexion récupérée.")
        return True

    @staticmethod
    def _goto_boosters(page: Page) -> None:
        response: Response | None = page.goto(_url(BOOSTERS_PATH), wait_until="domcontentloaded")
        WikiTcgCom._refuse_challenge(page, response)
        if "/connexion" in page.url:
            raise AuthError("Le site redirige vers la connexion : cookie wikitcg_session expiré ?")
        try:
            page.wait_for_selector(_SACHET, timeout=20_000)
        except PlaywrightError as exc:
            WikiTcgCom._refuse_challenge(page, response)
            raise SiteError("Le bouton d'ouverture des boosters est introuvable (le site a-t-il changé ?).") from exc

    @staticmethod
    def _refuse_challenge(page: Page, response: Response | None) -> None:
        headers = response.headers if response is not None else {}
        turnstile = page.locator("iframe[src*='challenges.cloudflare.com']").count()
        if turnstile or looks_like_challenge(headers, page.title()):
            raise SiteError("Défi anti-bot détecté sur wiki-tcg.com : arrêt, aucun contournement n'est tenté.")

    @staticmethod
    def _prepare(page: Page) -> str | None:
        """Met la scène d'accueil en état d'ouvrir. Rend la sorte du booster spécial choisi (« » pour un
        booster normal), ou None s'il n'y a plus rien à ouvrir. Les boosters normaux passent en premier."""
        sachet = page.locator(_SACHET)
        try:  # laisse à la page le temps de désélectionner un spécial épuisé
            page.wait_for_function("sel => { const b = document.querySelector(sel); return !!b && !b.disabled; }",
                                   arg=_SACHET, timeout=1_500)
        except PlaywrightError:
            specials = page.locator(_SPECIAUX)
            if not specials.count():
                return None
            if specials.first.get_attribute("aria-pressed") != "true":
                specials.first.dispatch_event("click")
            page.wait_for_selector("button.sachet[data-sorte]", timeout=5_000)
            if not sachet.is_enabled():
                return None
        return sachet.get_attribute("data-sorte") or ""

    @staticmethod
    def _open_one(page: Page) -> list[Card]:
        """Clique sur le sachet, attend le tirage et révèle toutes les cartes."""
        for attempt in range(3):
            page.locator(_SACHET).dispatch_event("click")
            try:
                page.wait_for_function(_CLICK_TAKEN_JS, polling=100, timeout=4_000)
                break
            except PlaywrightError:
                log.debug("Clic sur le sachet sans effet (page pas encore hydratée ?), essai %d.", attempt + 1)
        try:
            outcome = page.wait_for_function(_AFTER_CLICK_JS, polling=250, timeout=30_000).json_value()
        except PlaywrightError as exc:
            raise SiteError("Le tirage n'est pas arrivé après le clic sur le sachet.") from exc
        if outcome == "refus":
            raise SiteError("Le site a refusé l'ouverture du booster (réserve vide, compte restreint ?).")
        # Le booster est déjà consommé ici : un échec de lecture ne doit pas masquer l'ouverture.
        try:
            if page.locator(_REVEAL_ALL).count():
                page.locator(_REVEAL_ALL).dispatch_event("click")
            page.wait_for_selector(".opener[data-fini='1']", timeout=20_000)
            return parse_cards(page.evaluate(_CARDS_JS))
        except PlaywrightError:
            log.warning("Révélation incomplète : cartes tirées non lues.")
            return []

    @staticmethod
    def _can_continue(page: Page) -> bool:
        """Faux seulement si « Ouvrir un autre booster » est désactivé (plus rien à ouvrir) : qu'il manque ou
        soit ambigu est une anomalie, pas une fin."""
        again = page.locator(_AGAIN)
        if again.count() != 1:
            raise SiteError("Bouton « Ouvrir un autre booster » introuvable (révélation incomplète, "
                            "ou le site a-t-il changé ?).")
        return again.is_enabled()

    @staticmethod
    def _again(page: Page) -> None:
        page.locator(_AGAIN).dispatch_event("click")
        page.wait_for_selector(f".opener[data-phase='idle'] {_SACHET}", timeout=15_000)
