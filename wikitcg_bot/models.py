from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger("wikitcg_bot")


class SiteError(Exception):
    """Échec d'un site (réseau, réponse inattendue…)."""


class AuthError(SiteError):
    """Session refusée ou expirée : il faut se reconnecter (« login ») ou refaire un export de cookies."""


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

    def add_pack(self, cards: "list[Card]") -> None:
        """Compte un pack ouvert et le signale dans le journal (c'est ce qui permet de suivre un run)."""
        self.opened += 1
        self.cards.extend(cards)
        counts = Counter(c.rarity for c in cards)
        detail = ", ".join(f"{n}×{r}" for r, n in counts.most_common()) or "cartes non lues"
        stock = "" if self.packs_before is None else f" (stock au départ : {self.packs_before})"
        log.info("%s : pack n°%d ouvert — %s%s", self.label, self.opened, detail, stock)
