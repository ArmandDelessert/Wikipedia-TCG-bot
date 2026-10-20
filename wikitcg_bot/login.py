"""Connexion manuelle : ouvre le vrai Chrome sur le site, vous vous connectez, le bot garde la session."""
from __future__ import annotations

from playwright.sync_api import sync_playwright

from .browser import launch_options
from .config import Settings
from .sessions import SessionStore


def login(settings: Settings, site) -> int:
    url = getattr(site, "login_url", "")
    hosts = tuple(getattr(site, "hosts", ()))
    if not url or not hosts:
        print(f"{site.label} : connexion manuelle non prise en charge.")
        return 2
    store = SessionStore(settings, site.name, hosts)
    directory = settings.profiles_dir / site.name
    directory.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(directory), **launch_options(settings, headless=False), no_viewport=True,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url)
            input(f"Connectez-vous à {site.label} dans la fenêtre, puis appuyez sur Entrée ici… ")
            cookies = context.cookies()
            store.save(cookies)
        finally:
            context.close()
    print(f"Session enregistrée : {store.describe()}")
    return 0
