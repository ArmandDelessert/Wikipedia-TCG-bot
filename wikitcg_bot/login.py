"""Connexion manuelle : ouvre le vrai Chrome sur le site, vous vous connectez, le bot garde la session."""
from __future__ import annotations

from playwright.sync_api import sync_playwright

from . import schedule
from .browser import launch_options
from .config import Settings
from .locking import AlreadyRunning, run_lock
from .sites.base import session_store


def login(settings: Settings, site) -> int:
    url = getattr(site, "login_url", "")
    hosts = tuple(getattr(site, "hosts", ()))
    if not url or not hosts:
        print(f"{site.label} : connexion manuelle non prise en charge.")
        return 2
    try:
        # Le profil Chrome ne supporte pas deux navigateurs à la fois : pas de login pendant un run planifié.
        with run_lock(settings.state_dir / "run.lock"):
            return _login(settings, site)
    except AlreadyRunning:
        print("Un run est en cours : réessayez dans quelques minutes.")
        return 3


def _login(settings: Settings, site) -> int:
    store = session_store(settings, site)
    directory = settings.profiles_dir / site.name
    directory.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(directory), **launch_options(settings, headless=False), no_viewport=True,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(site.login_url)
            input(f"Connectez-vous à {site.label} dans la fenêtre, puis appuyez sur Entrée ici… ")
            cookies = context.cookies()
            store.save(cookies)
        finally:
            context.close()
    schedule.clear(settings, site.name)  # le prochain tour repasse sur ce site sans attendre
    print(f"Session enregistrée : {store.describe()}")
    return 0
