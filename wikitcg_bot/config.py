from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "oui", "on"}


@dataclass
class Settings:
    env: Mapping[str, str] = field(default_factory=dict)
    max_packs: int = 10
    dry_run: bool = False
    state_dir: Path = Path(".state")
    # Pause (min, max) en secondes entre deux ouvertures de packs.
    delay: tuple[float, float] = (2.0, 4.0)
    chromium_path: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            env=env,
            max_packs=max(1, int(env.get("MAX_PACKS_PER_RUN") or 10)),
            dry_run=_truthy(env.get("DRY_RUN")),
            state_dir=Path(env.get("STATE_DIR") or ".state"),
            chromium_path=env.get("CHROMIUM_PATH") or None,
        )

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
