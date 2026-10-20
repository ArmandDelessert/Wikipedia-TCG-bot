"""wikigacha.com — pilotage de l'interface, d'après le bookmarklet Clifeee/wikigacha-auto-opener.

Seuls les libellés « TAP TO OPEN » / « BACK TO PACKS » sont connus ; la connexion ne l'est
pas, d'où le recours à un état de session exporté depuis un vrai navigateur
(`scripts/save_session.py wikigacha`).

Le bouton « Watch Ad to Refill » est volontairement ignoré : le bot s'arrête quand il
apparaît, il ne simule pas de visionnage de publicité.
"""
from __future__ import annotations

import logging
import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..browser import browser_page
from ..config import Settings
from ..models import AuthError, SiteResult
from .base import pause

log = logging.getLogger(__name__)

URL = "https://wikigacha.com/"
TAP = re.compile(r"tap to open", re.I)
BACK = re.compile(r"back to packs", re.I)
REFILL = re.compile(r"watch ad to refill|no packs left", re.I)


def _visible(page: Page, locator, timeout_ms: int) -> bool:
    try:
        locator.first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightError:
        return False


class WikiGacha:
    name = "wikigacha"
    label = "Wikipedia Gacha"

    def configured(self, settings: Settings) -> bool:
        return bool(settings.get("WIKIGACHA_STORAGE_STATE"))

    def run(self, settings: Settings) -> SiteResult:
        result = SiteResult(self.name, self.label)
        state = settings.storage_state("WIKIGACHA_STORAGE_STATE")
        with browser_page(settings, storage_state=state, locale="en-US") as page:
            page.goto(URL, wait_until="domcontentloaded")
            tap = page.get_by_text(TAP)
            if not _visible(page, tap, 6000):
                # L'écran d'ouverture est derrière l'onglet « Gacha ».
                gacha_tab = page.get_by_role("button", name=re.compile(r"^\W*gacha\W*$", re.I))
                if _visible(page, gacha_tab, 1500):
                    gacha_tab.first.click()
            if settings.dry_run:
                result.message = "Simulation : rien n'a été ouvert."
                return result

            while result.opened < settings.max_packs:
                if not _visible(page, tap, 4000):
                    if _visible(page, page.get_by_text(REFILL), 500):
                        result.message = "Plus de pack (le site propose une recharge par publicité)."
                    elif page.get_by_text(re.compile(r"sign in|log ?in", re.I)).count():
                        raise AuthError("Session Wikipedia Gacha expirée : réexportez-la.")
                    else:
                        result.message = "Écran d'ouverture introuvable : l'interface a peut-être changé."
                    break
                tap.first.click()
                back = page.get_by_role("button", name=BACK)
                if not _visible(page, back, 15_000):
                    result.message = "Le bouton « Back to packs » n'est pas apparu après l'ouverture."
                    break
                result.opened += 1
                back.first.click()
                pause(settings)
            else:
                result.message = f"Limite de {settings.max_packs} packs atteinte."
        return result
