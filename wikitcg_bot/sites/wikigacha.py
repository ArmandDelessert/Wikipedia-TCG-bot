"""wikigacha.com — pilotage de l'interface réelle (Chrome), avec des garde-fous sur la collection.

Le site est une application cliente : la collection vit dans la base locale du navigateur
(IndexedDB `wiki-gacha-db`) et se synchronise, **par langue**, avec une sauvegarde serveur
(`/api/user/progress?lang=FR`). On joue donc toujours en français, dans un **profil de navigateur
persistant** : repartir d'une base vide à chaque run obligerait à réimporter toute la collection, et
les ouvertures non encore synchronisées (file `sync_queue`) seraient perdues avec le profil.

Avant toute ouverture, le module :
  1. vérifie que la session est valide (sinon `AuthError`) et qu'aucun défi Cloudflare n'est servi ;
  2. sauvegarde la collection du cloud (une fois par jour au plus) dans `<state_dir>/backups/` ;
  3. exige que la collection locale du navigateur soit identique à celle du cloud, sans changement
     en attente : un navigateur qui part d'une collection vide ou partielle n'ouvre rien.
Après chaque pack et à la fin, il relit le résumé du cloud : toute baisse arrête tout.

On ne touche à rien d'autre : ni synchronisation manuelle, ni publicité, ni partage, ni missions,
ni déconnexion. La publicité de recharge (« Regarder une pub pour recharger ») n'est jamais cliquée.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response

from ..browser import browser_page
from ..config import Settings
from ..models import AuthError, Card, SiteError, SiteResult
from .base import pause, session_store

log = logging.getLogger(__name__)

BASE_URL = "https://wikigacha.com"
LANG = "FR"  # en majuscules dans les appels d'API ; la langue du compte du propriétaire
PROFILE = "wikigacha"
SESSION_COOKIE = "__Secure-next-auth.session-token"

# Régénération lue dans le JavaScript du site : 1 pack par minute, plafond de 10 (mesurée en réel : voir
# docs/exploration.md). Les missions peuvent faire dépasser le plafond.
STOCK_CAP = 10
REFILL_MS = 60_000

BACKUP_EVERY_S = 20 * 3600  # une sauvegarde par jour au plus (marge pour un planning horaire)
BACKUPS_KEPT = 14
SYNC_WAIT_S = 90  # attente de la synchronisation après les ouvertures (relevé du cloud toutes les SYNC_POLL_MS)
SYNC_POLL_MS = 10_000
GATE_WAIT_S = 75  # attente de la convergence local/cloud avant d'ouvrir
GATE_POLL_MS = 5000
OVERLAY_PATIENCE_S = 4.0  # le bandeau de consentement et les annonces arrivent en différé
SETTLE_MS = 1500  # laisse l'interface adopter l'état importé du cloud avant de lire le stock

# Libellés français et anglais (la langue de l'interface suit ?lang=).
STOCK = re.compile(r"(?:PAQUETS QUOTIDIENS|DAILY PACKS)\s*:?\s*(\d+)\s*/\s*(\d+)", re.I)
COUNTDOWN = re.compile(r"(?:Prochain pack dans|Next pack in)\s*:?\s*(\d+):(\d{2})", re.I)
NO_PACKS = re.compile(r"plus de packs|no packs left", re.I)
BACK = re.compile(r"retour aux packs|back to packs", re.I)
SKIP = re.compile(r"^(voir les résultats|skip to results)$", re.I)
CHALLENGE_TITLE = re.compile(r"un instant|just a moment|attention required|vérification", re.I)
# Fenêtres d'information à refermer : jamais « Partager la campagne », « Se déconnecter », une pub…
INFO_OK = re.compile(r"^(ok|compris|got it)$", re.I)
CONSENT_MANAGE = re.compile(r"^(gérer les options|manage options)$", re.I)
CONSENT_CONFIRM = re.compile(r"^(confirmer les choix|confirm choices)$", re.I)
LEGITIMATE_INTEREST = re.compile(r"^(intérêt légitime|legitimate interest)", re.I)

_FETCH_JS = """
async (path) => {
  const r = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
  let body = null;
  try { body = await r.json(); } catch (e) {}
  return { status: r.status, body };
}
"""

# Lecture seule de la base locale du jeu : exemplaires de la langue + changements en attente de sync.
_LOCAL_JS = """
async (lang) => {
  const out = { present: false, cards: 0, copies: 0, pending: 0 };
  const dbs = await indexedDB.databases();
  if (!dbs.some(d => d.name === 'wiki-gacha-db')) return out;
  const db = await new Promise((resolve, reject) => {
    const req = indexedDB.open('wiki-gacha-db');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  try {
    out.present = true;
    const store = 'cards_' + lang.toLowerCase();
    const names = Array.from(db.objectStoreNames);
    if (!names.includes(store)) return out;
    const tx = db.transaction([store, 'sync_queue'], 'readonly');
    const all = (s) => new Promise((resolve, reject) => {
      const req = tx.objectStore(s).getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    const rows = (await all(store)).filter(c => (c.count || 0) > 0);
    out.cards = rows.length;
    out.copies = rows.reduce((n, c) => n + Math.floor(c.count || 0), 0);
    out.pending = (await all('sync_queue')).filter(e => String(e.lang || '').toUpperCase() === lang.toUpperCase()).length;
    return out;
  } finally {
    db.close();
  }
}
"""


# -- fonctions pures (testées sans navigateur) -------------------------------------------------------
@dataclass(frozen=True)
class Collection:
    """Taille d'une collection : cartes distinctes et exemplaires."""
    cards: int
    copies: int

    def __str__(self) -> str:
        return f"{self.cards} cartes / {self.copies} exemplaires"


def _count(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def parse_stock(text: str) -> tuple[int, int] | None:
    """(packs disponibles, plafond) d'après « PAQUETS QUOTIDIENS : 7 / 10 » ou « DAILY PACKS: 7 / 10 »."""
    match = STOCK.search(text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def parse_countdown(text: str) -> int | None:
    """Secondes avant le prochain pack d'après « Prochain pack dans : 0:18 »."""
    match = COUNTDOWN.search(text)
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def no_packs_left(text: str) -> bool:
    return bool(NO_PACKS.search(text))


def effective_stock(balance: int, last_refill_at: int, now_ms: int) -> int:
    """Stock courant d'après l'état signé `packState` (même règle que le client : 1 pack par minute)."""
    if balance >= STOCK_CAP:
        return balance  # le plafond ne rogne jamais un stock gagné par mission
    gained = max(0, now_ms - last_refill_at) // REFILL_MS
    return min(STOCK_CAP, balance + gained)


def parse_cloud_summary(payload: Any) -> Collection | None:
    """Résumé de collection du cloud (`cardSummary` de /api/user/progress), ou None s'il manque."""
    summary = payload.get("cardSummary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict) or "cardCount" not in summary or "copyCount" not in summary:
        return None
    return Collection(_count(summary["cardCount"]), _count(summary["copyCount"]))


def parse_pack_state(payload: Any) -> tuple[int, int] | None:
    """(balance, lastRefillAt en ms) du `packState` signé de la sauvegarde cloud."""
    try:
        state = payload["state"]["meta"]["packState"]
        return int(state["balance"]), int(state["lastRefillAt"])
    except (KeyError, TypeError, ValueError):
        return None


def parse_cards(payload: Any) -> list[Card]:
    """Cartes d'une réponse de POST /api/gacha (`cards[].title` et `rarity_rank` : C/UC/R/SR/SSR/UR/LR)."""
    cards = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(cards, list):
        return []
    return [Card(title=str(c.get("title") or c.get("id") or "?"), rarity=str(c.get("rarity_rank") or "?").upper())
            for c in cards if isinstance(c, dict)]


def signed_in(session: Any) -> bool:
    """NextAuth répond `{}` sans session, `{user: {...}, expires}` avec."""
    return isinstance(session, dict) and isinstance(session.get("user"), dict)


def is_challenge(title: str, mitigated: str | None, status: int = 200) -> bool:
    """Page de défi Cloudflare (« Un instant… », en-tête `cf-mitigated: challenge`)."""
    if (mitigated or "").strip().lower() == "challenge":
        return True
    return status in (403, 503) and bool(CHALLENGE_TITLE.search(title or ""))


def collection_gate(cloud: Collection, local: Collection, pending: int) -> str | None:
    """Raison de ne PAS ouvrir de pack (None : la collection locale est celle du cloud, rien en attente).

    Un navigateur qui part d'une collection vide ou partielle, ou qui garde des changements non
    synchronisés, ne doit rien ouvrir tant que la comparaison n'est pas rétablie.
    """
    if cloud.cards > 0 and local.cards == 0:
        return f"collection locale vide alors que le cloud compte {cloud}"
    if local.cards < cloud.cards or local.copies < cloud.copies:
        return f"collection locale incomplète ({local}) face au cloud ({cloud})"
    if local.cards > cloud.cards or local.copies > cloud.copies:
        return f"collection locale en avance ({local}) sur le cloud ({cloud})"
    if pending:
        return f"{pending} changement(s) local(aux) en attente de synchronisation"
    return None


def judge_growth(before: Collection, after: Collection, drawn: int) -> str:
    """'dropped' si le cloud a perdu des cartes ou des exemplaires, 'ok' s'il a gagné les `drawn` exemplaires
    tirés, 'pending' sinon (synchronisation différée)."""
    if after.cards < before.cards or after.copies < before.copies:
        return "dropped"
    return "ok" if after.copies >= before.copies + drawn else "pending"


def latest_backup_age(directory: Path, now: float) -> float | None:
    """Âge en secondes de la plus récente sauvegarde, ou None s'il n'y en a pas."""
    try:
        times = [p.stat().st_mtime for p in directory.glob(f"wikigacha_{LANG}_*.json")]
    except OSError:
        return None
    return now - max(times) if times else None


def prune_backups(directory: Path, keep: int = BACKUPS_KEPT) -> None:
    """Ne garde que les `keep` sauvegardes les plus récentes (les nôtres : même préfixe)."""
    files = sorted(directory.glob(f"wikigacha_{LANG}_*.json"))
    for old in files[:-keep] if keep > 0 else files:
        try:
            old.unlink()
        except OSError:
            log.warning("Ancienne sauvegarde %s non supprimée.", old.name)


# -- navigateur --------------------------------------------------------------------------------------
def _fetch(page: Page, path: str) -> tuple[int, Any]:
    reply = page.evaluate(_FETCH_JS, path)
    return reply["status"], reply["body"]


def _progress_path(include_cards: bool) -> str:
    return f"/api/user/progress?lang={LANG}&includeCards={int(include_cards)}"


def _cloud_progress(page: Page, include_cards: bool = False) -> Any:
    status, body = _fetch(page, _progress_path(include_cards))
    if status in (401, 403):
        raise AuthError(f"GET /api/user/progress → {status} : session Wikipedia Gacha refusée.")
    if status != 200 or not isinstance(body, dict):
        raise SiteError(f"GET /api/user/progress → {status} : réponse inattendue.")
    return body


def _cloud_collection(page: Page) -> Collection:
    summary = parse_cloud_summary(_cloud_progress(page))
    if summary is None:
        raise SiteError("Résumé de collection absent de la sauvegarde cloud : le site a-t-il changé ?")
    return summary


def _local_collection(page: Page) -> tuple[Collection, int]:
    """(collection de la base locale du navigateur, nombre de changements en attente de synchronisation)."""
    local = page.evaluate(_LOCAL_JS, LANG)
    return Collection(local["cards"], local["copies"]), local["pending"]


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except PlaywrightError:
        return ""


def _open_site(page: Page) -> None:
    """Charge la page de jeu ; défi Cloudflare ou session expirée interrompent le run."""
    response = page.goto(f"{BASE_URL}/?lang={LANG}", wait_until="domcontentloaded")
    status = response.status if response else 0
    mitigated = response.headers.get("cf-mitigated") if response else None
    if is_challenge(page.title(), mitigated, status):
        raise SiteError(
            f"défi Cloudflare servi à ce navigateur (HTTP {status}, cf-mitigated: {mitigated or 'absent'}) : "
            "essayez HEADLESS=0 depuis votre connexion habituelle ; le bot ne le contourne pas.")
    session_status, session = _fetch(page, "/api/auth/session")
    if session_status != 200 or not signed_in(session):
        raise AuthError("Session Wikipedia Gacha expirée : reconnectez-vous avec "
                        "« python -m wikitcg_bot login wikigacha ».")


def _wait_for_game(page: Page, timeout_s: float = 40.0) -> str:
    """Attend que le compteur de packs (ou l'écran « Plus de packs ») soit affiché ; rend le texte de la page."""
    deadline = time.monotonic() + timeout_s
    text = ""
    while time.monotonic() < deadline:
        text = _body_text(page)
        if parse_stock(text) or no_packs_left(text):
            return text
        page.wait_for_timeout(500)
    raise SiteError("L'écran des packs n'est pas apparu : l'interface a peut-être changé.")


def _dismiss_overlays(page: Page, patience_s: float | None = None) -> None:
    """Refuse le consentement publicitaire (option la plus protectrice) et referme les fenêtres d'information.

    Le bandeau et les fenêtres arrivent en différé et l'une cache la suivante : on attend `patience_s`
    sans nouveauté avant de conclure qu'il n'y a plus rien. Seuls des boutons précis sont actionnés :
    jamais « Tout accepter », « Autoriser », « Partager la campagne », une publicité ni « Se déconnecter ».
    """
    patience_s = OVERLAY_PATIENCE_S if patience_s is None else patience_s
    quiet_until = time.monotonic() + patience_s
    actions = 0
    while actions < 12:
        manage = page.get_by_role("button", name=CONSENT_MANAGE)
        info = page.get_by_role("button", name=INFO_OK)
        if manage.count() and manage.first.is_visible():
            manage.first.click()
            page.wait_for_timeout(700)
            # Le consentement est refusé d'office ; seuls les « intérêts légitimes » sont cochés par défaut.
            unchecked = 0
            for toggle in page.locator("[role=dialog]:visible").locator(
                    "input[type=checkbox], [role=switch], [role=checkbox]").all():
                label = toggle.get_attribute("aria-label") or ""
                if LEGITIMATE_INTEREST.match(label) and toggle.evaluate("e => e.checked ?? e.ariaChecked === 'true'"):
                    toggle.evaluate("e => e.click()")
                    unchecked += 1
            confirm = page.get_by_role("button", name=CONSENT_CONFIRM)
            if not confirm.count():
                raise SiteError("Bandeau de consentement : bouton « Confirmer les choix » introuvable.")
            confirm.first.click()
            log.info("Consentement publicitaire refusé (%d intérêt(s) légitime(s) décoché(s)).", unchecked)
            page.wait_for_timeout(700)
        elif info.count() and info.first.is_visible():
            info.first.click()
            page.wait_for_timeout(500)
        elif time.monotonic() < quiet_until:
            page.wait_for_timeout(400)
            continue
        else:
            return
        actions += 1
        quiet_until = time.monotonic() + min(patience_s, 1.5)
    raise SiteError("Les fenêtres d'information du site ne se referment pas.")


def _use_classic_effects(page: Page) -> None:
    """Le « mode effets renforcés » (préférence locale du navigateur, localStorage) allonge les animations et
    ajoute des étapes : on garde le mode classique, plus simple à piloter."""
    toggle = page.locator('button[aria-pressed="true"][class*="GachaEffects_toggle"]')
    if toggle.count():
        toggle.first.click()
        page.wait_for_timeout(300)


def _check_language(page: Page) -> None:
    """Jamais d'ouverture dans une autre langue que celle du compte : la collection est par langue."""
    label = page.locator('button[aria-label="Select Language"]')
    if not label.count() or LANG not in label.first.inner_text().upper():
        raise SiteError(f"La langue de l'interface n'est pas {LANG} : ouverture refusée.")


def _write_backup(settings: Settings, payload: dict) -> Path:
    directory = settings.state_dir / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / time.strftime(f"wikigacha_{LANG}_%Y%m%d-%H%M%S.json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    prune_backups(directory)
    return path


def _backup_if_due(page: Page, settings: Settings, expected: Collection) -> None:
    """Copie intégrale (lecture seule) de la collection du cloud avant d'ouvrir quoi que ce soit."""
    directory = settings.state_dir / "backups"
    age = latest_backup_age(directory, time.time())
    if age is not None and age < BACKUP_EVERY_S:
        return
    payload = _cloud_progress(page, include_cards=True)
    cards = (payload.get("state") or {}).get("cards")
    if not isinstance(cards, list) or len(cards) != expected.cards:
        raise SiteError("Sauvegarde du cloud incomplète : aucune ouverture tant qu'elle n'est pas fiable.")
    path = _write_backup(settings, payload)
    log.info("Collection du cloud sauvegardée dans %s (%d cartes).", path.name, len(cards))


def _converge(page: Page, cloud: Collection) -> tuple[Collection, str | None]:
    """Attend (≤ GATE_WAIT_S) que la base locale rejoigne le cloud : (résumé cloud, raison du blocage ou None)."""
    deadline = time.monotonic() + GATE_WAIT_S
    while True:
        local, pending = _local_collection(page)
        reason = collection_gate(cloud, local, pending)
        if reason is None or time.monotonic() >= deadline:
            return cloud, reason
        page.wait_for_timeout(GATE_POLL_MS)
        cloud = _cloud_collection(page)


def _gacha_responses(page: Page) -> list[Response]:
    """Écoute les réponses de POST /api/gacha (le client peut en émettre plusieurs : nouvelle tentative)."""
    seen: list[Response] = []

    def on_response(response: Response) -> None:
        if response.request.method == "POST" and response.url.split("?")[0].endswith("/api/gacha"):
            seen.append(response)

    page.on("response", on_response)
    return seen


def _gacha_failure(seen: list[Response]) -> str | None:
    """Si toutes les réponses de /api/gacha sont des échecs : « HTTP 503 (server is busy) », sinon None."""
    if not seen or any(r.ok for r in seen):
        return None
    last = seen[-1]
    try:
        detail = str((last.json() or {}).get("error") or "")[:100]
    except (PlaywrightError, ValueError, AttributeError):
        detail = ""
    return f"HTTP {last.status}" + (f" ({detail})" if detail else "")


def _open_one(page: Page, seen: list[Response]) -> list[Card] | None:
    """Touche le pack, lit les cartes dans la réponse de /api/gacha, revient à l'écran des packs.

    Rend None si le pack n'a pas réagi parce que le stock est en fait vide.
    """
    seen.clear()
    _dismiss_overlays(page, patience_s=0)  # une fenêtre tardive (consentement, annonce) cacherait le pack
    try:
        page.locator("#gacha-pack-container").click()
    except PlaywrightError as exc:
        raise SiteError("Impossible de toucher le pack (une fenêtre le recouvre ?).") from exc
    back = page.get_by_role("button", name=BACK)
    skip = page.get_by_role("button", name=SKIP)
    started = time.monotonic()
    while not (back.count() and back.first.is_visible()):
        waited = time.monotonic() - started
        if skip.count() and skip.first.is_visible():
            skip.first.click()
        if not seen and waited > 15:
            text = _body_text(page)
            if no_packs_left(text) or (parse_stock(text) or (1, 0))[0] == 0:
                return None
            raise SiteError("Le pack ne réagit pas au clic.")
        failure = _gacha_failure(seen) if waited > 20 else None
        if failure:
            raise SiteError(f"POST /api/gacha refusé : {failure}.")
        if waited > 60:
            raise SiteError("L'écran des résultats n'est pas apparu après l'ouverture du pack.")
        page.wait_for_timeout(400)
    cards: list[Card] = []
    for response in seen:
        if response.ok:
            cards = parse_cards(response.json()) or cards
    if not cards:
        raise SiteError("Aucune carte dans la réponse de /api/gacha : le site a-t-il changé ?")
    back.first.click()
    page.wait_for_timeout(500)
    return cards


def _wait_for_sync(page: Page, before: Collection, drawn: int) -> tuple[str, Collection, int]:
    """Attend la synchronisation différée (≤ SYNC_WAIT_S) : (verdict, résumé cloud, changements en attente)."""
    deadline = time.monotonic() + SYNC_WAIT_S
    while True:
        _, pending = _local_collection(page)
        after = _cloud_collection(page)
        verdict = judge_growth(before, after, drawn)
        if verdict == "dropped" or (verdict == "ok" and pending == 0) or time.monotonic() >= deadline:
            return verdict, after, pending
        page.wait_for_timeout(SYNC_POLL_MS)


class WikiGacha:
    name = "wikigacha"
    label = "Wikipedia Gacha"
    hosts = ("wikigacha.com",)
    login_url = f"{BASE_URL}/?lang={LANG}"

    def configured(self, settings: Settings) -> bool:
        return session_store(settings, self).configured()

    def session_summary(self, settings: Settings) -> str:
        return session_store(settings, self).describe((SESSION_COOKIE,))

    def run(self, settings: Settings) -> SiteResult:
        result = SiteResult(self.name, self.label)
        store = session_store(settings, self)
        with browser_page(settings, store=store, profile=PROFILE) as page:
            _open_site(page)
            _wait_for_game(page)
            if not settings.dry_run:
                _dismiss_overlays(page)
                _use_classic_effects(page)
                _check_language(page)
            cloud = _cloud_collection(page)
            if not settings.dry_run:
                _backup_if_due(page, settings, cloud)
            cloud, blocked = _converge(page, cloud)
            page.wait_for_timeout(SETTLE_MS)
            text = _body_text(page)
            stock = parse_stock(text)
            result.packs_before = stock[0] if stock else 0
            result.packs_max = stock[1] if stock else STOCK_CAP

            if settings.dry_run:
                state = "identique au cloud" if blocked is None else f"à vérifier : {blocked}"
                result.message = f"Simulation : rien n'a été ouvert. Collection {cloud}, {state}."
            elif not result.packs_before:
                wait = parse_countdown(text)
                when = f" (prochain dans {wait // 60}:{wait % 60:02d})" if wait is not None else ""
                result.message = f"Plus de pack{when} ; la recharge par publicité proposée par le site est ignorée."
            elif blocked:
                raise SiteError(f"Ouverture refusée pour protéger la collection : {blocked}.")
            else:
                self._open_packs(page, settings, result, cloud)
        return result

    def _open_packs(self, page: Page, settings: Settings, result: SiteResult, start: Collection) -> None:
        seen = _gacha_responses(page)
        drawn, stock, failure = 0, result.packs_before or 0, ""
        try:
            while stock > 0 and result.opened < settings.max_packs:
                cards = _open_one(page, seen)
                if cards is None:
                    stock = 0
                    break
                result.opened += 1
                result.cards.extend(cards)
                drawn += len(cards)
                # Une baisse du cloud, même passagère, arrête tout sur-le-champ.
                if judge_growth(start, _cloud_collection(page), 0) == "dropped":
                    self._alarm(result, f"le cloud a PERDU des cartes après {result.opened} pack(s) (départ : {start})")
                    return
                now = parse_stock(_body_text(page))
                stock = now[0] if now else 0
                if stock > 0 and result.opened < settings.max_packs:
                    pause(settings)
        except SiteError as exc:  # on garde ce qui a déjà été ouvert et on laisse la synchronisation finir
            failure = str(exc)
            result.auth_failed = isinstance(exc, AuthError)
        if not drawn:
            if failure:
                raise SiteError(failure)
            result.message = "Aucun pack n'a pu être ouvert (le stock affiché était vide)."
            return

        verdict, end, pending = _wait_for_sync(page, start, drawn)
        if verdict == "dropped":
            self._alarm(result, f"le cloud a PERDU des cartes : {start} avant, {end} après")
        elif verdict == "pending":
            self._alarm(result, f"le cloud n'a pas enregistré les {drawn} exemplaires tirés après {SYNC_WAIT_S} s "
                                f"({start} → {end}, {pending} changement(s) encore en attente) ; "
                                "ils seront rejoués au prochain run, vérifiez-le")
        elif failure:
            self._alarm(result, f"{failure} (après {result.opened} pack(s) ouvert(s), collection {start} → {end})")
        elif stock > 0:
            result.message = f"Limite de {settings.max_packs} packs atteinte, il en reste {stock}. Collection {start} → {end}."
        else:
            result.message = f"Tous les packs disponibles ont été ouverts. Collection {start} → {end}."

    @staticmethod
    def _alarm(result: SiteResult, detail: str) -> None:
        log.error("Wikipedia Gacha : ATTENTION, %s.", detail)
        result.ok = False
        result.message = f"ATTENTION : {detail}."
