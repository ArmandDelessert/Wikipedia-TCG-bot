from __future__ import annotations

from collections import Counter

from .models import SiteResult

# Raretés connues des différents jeux, de la plus rare à la plus commune.
RARITY_ORDER = ["LR", "L", "UR", "SSR", "SR", "R", "UC", "PC", "C"]


def _rank(rarity: str) -> int:
    return RARITY_ORDER.index(rarity) if rarity in RARITY_ORDER else len(RARITY_ORDER)


def rarity_counts(result: SiteResult) -> str:
    counts = Counter(c.rarity for c in result.cards)
    return ", ".join(f"{n}×{r}" for r, n in sorted(counts.items(), key=lambda kv: _rank(kv[0])))


def render_summary(results: list[SiteResult]) -> str:
    if not results:
        return "## Ouverture de packs\n\nAucun site configuré : ajoutez les secrets décrits dans le README.\n"
    lines = [
        "## Ouverture de packs",
        "",
        "| Site | État | Packs ouverts | Stock avant | Détail |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        stock = "—"
        if r.packs_before is not None:
            stock = f"{r.packs_before}/{r.packs_max}" if r.packs_max else str(r.packs_before)
        detail = (rarity_counts(r) + " — " if r.cards else "") + r.message
        lines.append(
            f"| {r.label} | {'✅' if r.ok else '❌'} | {r.opened} | {stock} | {detail.strip(' —')} |"
        )
    for r in results:
        if not r.cards:
            continue
        lines += ["", f"<details><summary>Cartes tirées — {r.label}</summary>", ""]
        for card in sorted(r.cards, key=lambda c: _rank(c.rarity)):
            lines.append(f"- **{card.rarity}** {card.title}")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"

