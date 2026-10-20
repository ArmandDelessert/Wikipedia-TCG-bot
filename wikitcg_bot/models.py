from __future__ import annotations

from dataclasses import dataclass, field


class SiteError(Exception):
    """Échec d'un site (réseau, réponse inattendue…)."""


class AuthError(SiteError):
    """Session/identifiants refusés : l'utilisateur doit renouveler son secret."""


@dataclass(frozen=True)
class Card:
    title: str
    rarity: str = "?"


@dataclass
class SiteResult:
    site: str
    label: str
    ok: bool = True
    opened: int = 0
    cards: list[Card] = field(default_factory=list)
    packs_before: int | None = None
    packs_max: int | None = None
    message: str = ""
    # La session est refusée ou a expiré : il faut se reconnecter (`login`) ou refaire un export.
    auth_failed: bool = False
