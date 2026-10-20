"""wiki-masters.com — connexion par formulaire puis `POST /api/packs/open`.

L'endpoint d'ouverture est celui qu'utilisent les projets Sephiroth-ctrl/Wiki-Masters-Bot
et JeanMichelChien/project_wikimaster ; le formulaire de connexion est piloté par
libellés (français/anglais) plutôt que par classes CSS, qui changent à chaque déploiement.
"""
from __future__ import annotations

import logging
import re
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..browser import browser_page
from ..config import Settings
from ..models import AuthError, Card, SiteResult
from .base import pause

log = logging.getLogger(__name__)

BASE_URL = "https://www.wiki-masters.com"
PULLS_URL = f"{BASE_URL}/pulls"
PACK_STATUS = re.compile(r"(\d+)\s*/\s*(\d+)\s+paquets?\s+disponibles?", re.IGNORECASE)

_OPEN_PACK_JS = """
async () => {
  const r = await fetch('/api/packs/open', { method: 'POST', credentials: 'include' });
  let body = null;
  try { body = await r.json(); } catch (e) {}
  return { status: r.status, body };
}
"""


def parse_card(raw: dict) -> Card:
    """Les cartes arrivent à plat ou imbriquées sous `card` selon l'endpoint."""
    inner = raw.get("card") if isinstance(raw.get("card"), dict) else {}
    title = (raw.get("wikipedia_title") or inner.get("wikipedia_title")
             or raw.get("title") or inner.get("title") or raw.get("name") or "?")
    rarity = raw.get("rarity") or inner.get("rarity") or "?"
    return Card(title=str(title), rarity=str(rarity).upper())


def _first_visible(page: Page, locators, timeout_ms: int = 1500):
    for loc in locators:
        try:
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            return loc.first
        except PlaywrightError:
            continue
    return None


def _login_form(page: Page):
    email = _first_visible(page, [
        page.locator('input[type="email"]'),
        page.get_by_label(re.compile("courriel|e-?mail", re.I)),
        page.locator('input[name*="mail"]'),
    ])
    password = _first_visible(page, [page.locator('input[type="password"]')], 500)
    return (email, password) if email and password else None


def _wait_for_page_state(page: Page, timeout_s: float = 12.0) -> str:
    """'login' si le site demande de se connecter, 'app' si la page des packs est affichée."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if "/login" in page.url:
            return "login"
        try:
            body = page.locator("body").inner_text(timeout=1000)
        except PlaywrightError:
            body = ""
        if PACK_STATUS.search(body) or re.search(r"\bouvrir\b|aucun paquet", body, re.I):
            return "app"
        page.wait_for_timeout(300)
    return "login" if _login_form(page) else "app"


def _login(page: Page, email: str, password: str) -> None:
    form = _login_form(page)
    if form is None:
        raise AuthError("Formulaire de connexion WikiMasters introuvable (le site a-t-il changé ?).")
    email_input, password_input = form
    email_input.fill(email)
    password_input.fill(password)
    submit = _first_visible(page, [
        page.get_by_role("button", name=re.compile("^(connexion|se connecter|log ?in)$", re.I)),
        page.locator('button[type="submit"]'),
    ])
    if submit is None:
        password_input.press("Enter")
    else:
        submit.click()
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)
    except PlaywrightError as exc:
        text = ""
        try:
            text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=1000))[:200]
        except PlaywrightError:
            pass
        raise AuthError(f"Connexion WikiMasters refusée ({text or 'aucun message'}).") from exc


class WikiMasters:
    name = "wikimasters"
    label = "WikiMasters"

    def configured(self, settings: Settings) -> bool:
        return bool(
            (settings.get("WIKIMASTERS_EMAIL") and settings.get("WIKIMASTERS_PASSWORD"))
            or settings.get("WIKIMASTERS_STORAGE_STATE")
        )

    def run(self, settings: Settings) -> SiteResult:
        result = SiteResult(self.name, self.label)
        email, password = settings.get("WIKIMASTERS_EMAIL"), settings.get("WIKIMASTERS_PASSWORD")
        state = settings.storage_state("WIKIMASTERS_STORAGE_STATE")
        with browser_page(settings, storage_state=state) as page:
            page.goto(PULLS_URL, wait_until="domcontentloaded")
            if _wait_for_page_state(page) == "login":
                if not (email and password):
                    raise AuthError("Session WikiMasters expirée et aucun identifiant fourni.")
                _login(page, email, password)
                page.goto(PULLS_URL, wait_until="domcontentloaded")
                _wait_for_page_state(page)

            self._read_status(page, result)
            if settings.dry_run:
                result.message = "Simulation : rien n'a été ouvert."
                return result

            while result.opened < settings.max_packs:
                reply = page.evaluate(_OPEN_PACK_JS)
                body = reply.get("body") if isinstance(reply.get("body"), dict) else {}
                cards = body.get("cards") or []
                if reply["status"] != 200 or not cards:
                    if "/login" in page.url:
                        raise AuthError("Session WikiMasters perdue en cours d'exécution.")
                    detail = body.get("error") or f"HTTP {reply['status']}"
                    result.message = f"Plus de pack à ouvrir ({detail})."
                    break
                result.opened += 1
                result.cards.extend(parse_card(c) for c in cards)
                pause(settings)
            else:
                result.message = f"Limite de {settings.max_packs} packs atteinte."
        return result

    @staticmethod
    def _read_status(page: Page, result: SiteResult) -> None:
        try:
            match = PACK_STATUS.search(page.locator("body").inner_text(timeout=2000))
        except PlaywrightError:
            return
        if match:
            result.packs_before, result.packs_max = int(match.group(1)), int(match.group(2))
