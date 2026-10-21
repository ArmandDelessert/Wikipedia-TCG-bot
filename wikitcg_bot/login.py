"""Connexion manuelle : ouvre le vrai Chrome sur le site, vous vous connectez, le bot garde la session."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

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


CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


def _browser_exe(settings: Settings) -> str | None:
    if settings.chromium_path:
        return settings.chromium_path
    paths = CHROME_PATHS[2:] if settings.browser_channel == "msedge" else CHROME_PATHS
    return next((p for p in paths if Path(p).is_file()), None)


def _login(settings: Settings, site) -> int:
    """Chrome est lancé **sans automatisation** : Playwright y ajoute des marqueurs (navigator.webdriver,
    bandeau « contrôlé par un logiciel de test ») que le Turnstile de Cloudflare détecte et fait échouer.
    Vous vous connectez donc dans un Chrome ordinaire, avec le profil dédié du bot ; une fois la fenêtre
    fermée, le bot rouvre ce profil en silence pour en relever les cookies."""
    exe = _browser_exe(settings)
    if not exe:
        print("Chrome introuvable : installez-le ou renseignez CHROMIUM_PATH.")
        return 2
    store = session_store(settings, site)
    directory = settings.profiles_dir / site.name
    directory.mkdir(parents=True, exist_ok=True)
    print(f"Chrome s'ouvre sur {site.label}. Connectez-vous, puis FERMEZ la fenêtre Chrome.")
    subprocess.run([exe, f"--user-data-dir={directory.resolve()}", "--no-first-run",
                    "--no-default-browser-check", site.login_url], check=False)
    time.sleep(2)  # Chrome écrit encore ses cookies à la fermeture
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(directory), **launch_options(settings, headless=True))
        try:
            store.save(context.cookies())
        finally:
            context.close()
    schedule.clear(settings, site.name)  # le prochain tour repasse sur ce site sans attendre
    print(f"Session enregistrée : {store.describe()}")
    return 0
