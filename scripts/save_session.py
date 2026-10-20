#!/usr/bin/env python3
"""Se connecter à la main puis exporter la session pour GitHub Actions.

    python scripts/save_session.py wikigacha

Ouvre un vrai navigateur. Connectez-vous (Google, e-mail, peu importe), puis revenez dans le
terminal et appuyez sur Entrée. Le fichier `<site>.state.b64` contient la session ; collez son
contenu dans le secret GitHub indiqué. Ce fichier donne accès au compte : ne le partagez pas.
"""
import base64
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SITES = {
    "wikigacha": ("https://wikigacha.com/", "WIKIGACHA_STORAGE_STATE"),
    "wikimasters": ("https://www.wiki-masters.com/", "WIKIMASTERS_STORAGE_STATE"),
    "wiki_tcg_com": ("https://wiki-tcg.com/", "WIKI_TCG_COM_STORAGE_STATE"),
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in SITES:
        print(__doc__)
        print("Sites :", ", ".join(SITES))
        return 2
    site = sys.argv[1]
    url, secret = SITES[site]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        context.new_page().goto(url)
        input("Connectez-vous dans la fenêtre, puis appuyez sur Entrée ici… ")
        state = context.storage_state()
        browser.close()
    out = Path(f"{site}.state.b64")
    out.write_text(base64.b64encode(json.dumps(state).encode()).decode())
    out.chmod(0o600)
    print(f"Session écrite dans {out}. Collez son contenu dans le secret GitHub {secret}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
