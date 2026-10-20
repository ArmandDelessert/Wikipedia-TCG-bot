"""Décrit la structure publique des sites (boutons, champs, appels réseau).

Sert à écrire/réparer les modules de `wikitcg_bot/sites/` sans accès direct aux sites.
Rien de ce qui est affiché ne contient de cookie, d'en-tête ou de corps de requête.

    python -m wikitcg_bot.probe [--screenshots]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page

from .browser import browser_page
from .config import Settings
from .report import write_github_summary

TARGETS = {
    "wikimasters": ("https://www.wiki-masters.com/pulls", "WIKIMASTERS_STORAGE_STATE"),
    "wikigacha": ("https://wikigacha.com/", "WIKIGACHA_STORAGE_STATE"),
    "wiki_tcg_com": ("https://wiki-tcg.com/", "WIKI_TCG_COM_STORAGE_STATE"),
    "wikitcg_net": ("https://wikitcg.net/", "WIKITCG_NET_STORAGE_STATE"),
}

_DESCRIBE_JS = """
() => {
  const text = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || '')
      .trim().replace(/\\s+/g, ' ').slice(0, 60);
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight);
  return {
    buttons: [...document.querySelectorAll('button,[role=button]')].filter(visible).map(text).filter(Boolean),
    links: [...document.querySelectorAll('a[href]')].filter(visible)
        .map((a) => text(a) + ' -> ' + new URL(a.href, location.href).pathname).slice(0, 40),
    inputs: [...document.querySelectorAll('input,select,textarea')].map((i) =>
        [i.type, i.name, i.placeholder, i.getAttribute('autocomplete')].filter(Boolean).join('|')),
    text: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 600),
  };
}
"""


def probe_one(settings: Settings, key: str, out_dir: Path, screenshots: bool) -> dict:
    url, state_var = TARGETS[key]
    calls: list[str] = []
    info: dict = {"site": key, "requested": url, "authenticated": False}
    try:
        state = settings.storage_state(state_var)
        info["authenticated"] = state is not None
        with browser_page(settings, storage_state=state) as page:
            page.on("request", lambda r: calls.append(f"{r.method} {urlsplit(r.url).path}")
                    if r.resource_type in ("fetch", "xhr") else None)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            _settle(page)
            info.update(final_url=page.url, title=page.title(), **page.evaluate(_DESCRIBE_JS))
            if screenshots:
                out_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out_dir / f"{key}.png"), full_page=True)
    except Exception as exc:  # une sonde ne doit jamais interrompre les autres
        info["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    info["api_calls"] = sorted(set(calls))
    return info


def _settle(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def to_markdown(infos: list[dict]) -> str:
    out = ["## Sonde des sites", ""]
    for i in infos:
        out.append(f"### {i['site']} ({'connecté' if i['authenticated'] else 'non connecté'})")
        if "error" in i:
            out += [f"Erreur : `{i['error']}`", ""]
            continue
        out += [f"- URL finale : `{i['final_url']}` — titre : {i['title']!r}",
                f"- Boutons : {json.dumps(i['buttons'], ensure_ascii=False)}",
                f"- Champs : {json.dumps(i['inputs'], ensure_ascii=False)}",
                f"- Appels réseau : {json.dumps(i['api_calls'])}",
                f"- Liens : {json.dumps(i['links'], ensure_ascii=False)}",
                f"- Texte : {re.sub(r'[`|]', ' ', i['text'])}", ""]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--out", default="probe-output")
    args = parser.parse_args()
    settings = Settings.from_env()
    infos = [probe_one(settings, key, Path(args.out), args.screenshots) for key in TARGETS]
    markdown = to_markdown(infos)
    print(markdown)
    write_github_summary(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
