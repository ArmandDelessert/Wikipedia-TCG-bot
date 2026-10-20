from __future__ import annotations

import contextlib
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import Settings


@contextlib.contextmanager
def browser_page(settings: Settings, storage_state: dict | None = None,
                 locale: str = "fr-FR", timezone: str = "Europe/Paris") -> Iterator[Page]:
    """Ouvre Chromium (headless) et rend une page ; tout est refermé à la sortie."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=settings.chromium_path,
            args=["--no-sandbox"] if settings.chromium_path else [],
        )
        try:
            context: BrowserContext = browser.new_context(
                locale=locale,
                timezone_id=timezone,
                viewport={"width": 1440, "height": 900},
                storage_state=storage_state,
            )
            page = context.new_page()
            page.set_default_timeout(15_000)
            yield page
        finally:
            browser.close()
