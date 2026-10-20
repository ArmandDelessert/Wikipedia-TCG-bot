from __future__ import annotations

import contextlib
import logging
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import Settings
from .sessions import SessionStore

log = logging.getLogger(__name__)


def launch_options(settings: Settings, headless: bool | None = None) -> dict:
    """Options communes de lancement : le vrai Chrome installé, sauf `CHROMIUM_PATH`."""
    options: dict = {"headless": settings.headless if headless is None else headless}
    if settings.chromium_path:
        options["executable_path"] = settings.chromium_path
        options["args"] = ["--no-sandbox"]
    elif settings.browser_channel:
        options["channel"] = settings.browser_channel
    return options


def _add_cookies(context: BrowserContext, cookies: list[dict]) -> None:
    try:
        context.add_cookies(cookies)
        return
    except Exception:  # un cookie mal formé ne doit pas empêcher d'injecter les autres
        log.debug("add_cookies en bloc refusé, essai cookie par cookie")
    for cookie in cookies:
        try:
            context.add_cookies([cookie])
        except Exception:
            log.warning("Cookie %s ignoré (refusé par le navigateur).", cookie.get("name"))


@contextlib.contextmanager
def browser_page(settings: Settings, store: SessionStore | None = None, profile: str | None = None,
                 storage_state: dict | None = None, locale: str = "fr-FR",
                 timezone: str = "Europe/Paris", headless: bool | None = None) -> Iterator[Page]:
    """Ouvre le navigateur et rend une page ; tout est refermé à la sortie.

    - `store` : les cookies de la session y sont lus au départ et **réécrits à la fin**, ce qui
      conserve les renouvellements faits par le site (même si le run échoue).
    - `profile` : nom d'un profil de navigateur persistant (`<state_dir>/profiles/<nom>`), pour les
      sites dont la session ou les données locales (IndexedDB…) doivent survivre d'un run à l'autre.
      Avec `store`, le profil n'est complété que des cookies qui lui manquent.
    """
    options = launch_options(settings, headless)
    common = {"locale": locale, "timezone_id": timezone, "viewport": {"width": 1440, "height": 900}}
    with sync_playwright() as pw:
        if profile:
            directory = settings.profiles_dir / profile
            directory.mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(str(directory), **options, **common)
            browser = None
        else:
            browser = pw.chromium.launch(**options)
            context = browser.new_context(**common, storage_state=storage_state)
        try:
            if store is not None:
                # cf_clearance appartient à l'IP et au navigateur qui ont passé le défi Cloudflare :
                # le réinjecter dans un autre navigateur ne sert à rien et présenterait un jeton incohérent.
                cookies = [c for c in store.load() if c["name"] != "cf_clearance"]
                if profile:
                    # Le profil garde déjà ses propres cookies, éventuellement plus récents que
                    # l'état sauvegardé : on ne lui ajoute que ceux qu'il n'a pas.
                    present = {(c["name"], c["domain"].lstrip("."), c["path"]) for c in context.cookies()}
                    cookies = [c for c in cookies
                               if (c["name"], c["domain"].lstrip("."), c["path"]) not in present]
                _add_cookies(context, cookies)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15_000)
            yield page
        finally:
            try:
                if store is not None:
                    cookies = context.cookies()
                    if cookies:
                        store.save(cookies)
            except Exception:
                log.warning("Impossible de sauvegarder les cookies de %s.", store.site if store else "?")
            finally:
                context.close()
                if browser is not None:
                    browser.close()
