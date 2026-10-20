from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "oui", "on"}


def load_env_file(path: Path) -> dict[str, str]:
    """Lit un fichier `.env` simple (`CLE=valeur`, `#` pour les commentaires)."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass
class Settings:
    env: Mapping[str, str] = field(default_factory=dict)
    max_packs: int = 10
    dry_run: bool = False
    # État d'exécution local (sessions, profils de navigateur, historique, journaux) : jamais versionné.
    state_dir: Path = Path(".state")
    # Exports de cookies au format Netscape (« <hôte>_cookies.txt »), faits depuis votre navigateur.
    cookies_dir: Path = Path("Cookies")
    # Pause (min, max) en secondes entre deux ouvertures de packs.
    delay: tuple[float, float] = (2.0, 4.0)
    chromium_path: str | None = None
    # Navigateur piloté : le vrai Chrome installé (« chrome »), « msedge », ou None pour le Chromium de Playwright.
    browser_channel: str | None = "chrome"
    headless: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        channel = (env.get("BROWSER_CHANNEL") if "BROWSER_CHANNEL" in env else "chrome") or ""
        channel = channel.strip().lower()
        return cls(
            env=env,
            max_packs=max(1, int(env.get("MAX_PACKS_PER_RUN") or 10)),
            dry_run=_truthy(env.get("DRY_RUN")),
            state_dir=Path(env.get("STATE_DIR") or ".state"),
            cookies_dir=Path(env.get("COOKIES_DIR") or "Cookies"),
            chromium_path=env.get("CHROMIUM_PATH") or None,
            browser_channel=None if channel in {"", "chromium", "none"} else channel,
            headless=env.get("HEADLESS", "1").strip().lower() not in {"0", "false", "no", "non", "off"},
        )

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def profiles_dir(self) -> Path:
        return self.state_dir / "profiles"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def history_file(self) -> Path:
        return self.state_dir / "history.jsonl"

    def get(self, key: str) -> str:
        return (self.env.get(key) or "").strip()

    def storage_state(self, key: str) -> dict | None:
        """Décode un état de session Playwright stocké en base64 dans une variable."""
        raw = self.get(key)
        if not raw:
            return None
        try:
            return json.loads(base64.b64decode(raw))
        except ValueError as exc:
            raise ValueError(f"{key} n'est pas un JSON encodé en base64 valide") from exc
