from wikitcg_bot.models import Card, SiteResult
from wikitcg_bot.report import render_summary


def test_summary_sorts_by_rarity_and_flags_failures():
    ok = SiteResult("a", "Site A", opened=2, packs_before=2, packs_max=5,
                    cards=[Card("Seine", "C"), Card("Paris", "UR"), Card("Rhône", "C")])
    ko = SiteResult("b", "Site B", ok=False, message="Authentification : expirée")
    md = render_summary([ok, ko])
    assert "| Site A | ✅ | 2 | 2/5 | 1×UR, 2×C" in md
    assert "| Site B | ❌ |" in md
    assert md.index("**UR** Paris") < md.index("**C** Seine")


def test_summary_without_sites():
    assert "Aucun site configuré" in render_summary([])
