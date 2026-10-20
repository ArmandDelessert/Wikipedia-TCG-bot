"""Ouvre les boosters gratuits de 4 jeux de cartes tirés de Wikipédia, depuis votre poste.

    python -m wikitcg_bot                  ouvre les packs disponibles (commande « run »)
    python -m wikitcg_bot status           lit l'état de chaque site sans rien ouvrir
    python -m wikitcg_bot login SITE       connexion manuelle dans Chrome (session conservée)

Codes de sortie : 0 tout va bien, 1 erreur, 2 session expirée ou refusée, 3 run déjà en cours.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from .config import Settings, load_env_file
from .history import record
from .locking import AlreadyRunning, run_lock
from .login import login
from .models import AuthError, SiteResult
from .report import render_summary
from .sites import SITES

log = logging.getLogger("wikitcg_bot")

EXIT_OK, EXIT_ERROR, EXIT_AUTH, EXIT_BUSY = 0, 1, 2, 3


def run_site(name: str, settings: Settings) -> SiteResult:
    site = SITES[name]
    try:
        return site.run(settings)
    except Exception as exc:  # un site en échec ne doit pas bloquer les autres
        auth = isinstance(exc, AuthError)
        log.error("%s : %s : %s", site.label, "authentification" if auth else "erreur", exc)
        log.debug("détail de l'échec", exc_info=True)
        kind = "Session refusée" if auth else "Erreur"
        return SiteResult(name, site.label, ok=False, auth_failed=auth, message=f"{kind} : {exc}")


def setup_logging(settings: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        settings.logs_dir / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # httpx journalise chaque URL en INFO : inutile et jamais utile à lire.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wikitcg_bot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="ouvre les packs disponibles (par défaut)")
    run.add_argument("--sites", default="",
                     help=f"liste séparée par des virgules ({', '.join(SITES)}) ; "
                          "par défaut : les sites dont une session est présente")
    run.add_argument("--dry-run", action="store_true", help="ne rien ouvrir")
    status = sub.add_parser("status", help="état des sessions et des stocks, sans rien ouvrir")
    status.add_argument("--sites", default="")
    log_in = sub.add_parser("login", help="connexion manuelle dans Chrome")
    log_in.add_argument("site", choices=sorted(SITES))
    return parser


def _site_names(parser: argparse.ArgumentParser, raw: str, settings: Settings) -> list[str]:
    requested = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in requested if s not in SITES]
    if unknown:
        parser.error(f"site(s) inconnu(s) : {', '.join(unknown)}")
    return requested or [n for n, site in SITES.items() if site.configured(settings)]


def do_run(parser: argparse.ArgumentParser, args, settings: Settings) -> int:
    names = _site_names(parser, args.sites, settings)
    if not names:
        log.warning("Aucune session trouvée : placez vos exports de cookies dans %s/ "
                    "ou lancez « login <site> ».", settings.cookies_dir)
        return EXIT_OK
    try:
        with run_lock(settings.state_dir / "run.lock"):
            results = []
            for name in names:
                result = run_site(name, settings)
                record(settings, result)
                results.append(result)
            (settings.state_dir / "last_run.json").write_text(
                json.dumps({"at": int(time.time()), "sites": names}), encoding="utf-8")
    except AlreadyRunning as exc:
        log.warning("Run ignoré : %s.", exc)
        return EXIT_BUSY
    print(render_summary(results))
    if any(r.auth_failed for r in results):
        return EXIT_AUTH
    return EXIT_OK if all(r.ok for r in results) else EXIT_ERROR


def do_status(parser: argparse.ArgumentParser, args, settings: Settings) -> int:
    names = _site_names(parser, args.sites, settings)
    for name, site in SITES.items():
        state = site.session_summary(settings) if site.configured(settings) else "aucune session"
        print(f"- {site.label} : {state}")
    print()
    settings.dry_run = True
    results = [run_site(name, settings) for name in names]
    print(render_summary(results))
    return EXIT_AUTH if any(r.auth_failed for r in results) else (
        EXIT_OK if all(r.ok for r in results) else EXIT_ERROR)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv.insert(0, "run")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_OK

    env = {**load_env_file(Path(".env")), **os.environ}
    settings = Settings.from_env(env)
    setup_logging(settings)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "login":
        return login(settings, SITES[args.site])
    if args.command == "status":
        return do_status(parser, args, settings)
    settings.dry_run = settings.dry_run or args.dry_run
    return do_run(parser, args, settings)


if __name__ == "__main__":
    sys.exit(main())
