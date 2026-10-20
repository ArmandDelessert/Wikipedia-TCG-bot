from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .config import Settings
from .models import AuthError, SiteResult
from .report import render_summary, write_github_summary
from .sites import SITES

log = logging.getLogger("wikitcg_bot")


def run_site(name: str, settings: Settings) -> SiteResult:
    site = SITES[name]
    try:
        return site.run(settings)
    except Exception as exc:  # un site en échec ne doit pas bloquer les autres
        kind = "Authentification" if isinstance(exc, AuthError) else "Erreur"
        print(f"::error title={site.label}::{kind} : {exc}")
        log.debug("détail de l'échec", exc_info=True)
        return SiteResult(name, site.label, ok=False, message=f"{kind} : {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wikitcg_bot", description=__doc__)
    parser.add_argument("--sites", default="",
                        help=f"liste séparée par des virgules ({', '.join(SITES)}) ; "
                             "par défaut : les sites dont les secrets sont définis")
    parser.add_argument("--dry-run", action="store_true", help="ne rien ouvrir")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if args.dry_run:
        settings.dry_run = True

    requested = [s.strip() for s in args.sites.split(",") if s.strip()]
    unknown = [s for s in requested if s not in SITES]
    if unknown:
        parser.error(f"site(s) inconnu(s) : {', '.join(unknown)}")
    names = requested or [n for n, site in SITES.items() if site.configured(settings)]

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    results = [run_site(name, settings) for name in names]
    (settings.state_dir / "last_run.json").write_text(
        json.dumps({"at": int(time.time()), "sites": names})
    )

    summary = render_summary(results)
    print(summary)
    write_github_summary(summary)
    if not names:
        print("::warning::Aucun site configuré (secrets manquants).")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
