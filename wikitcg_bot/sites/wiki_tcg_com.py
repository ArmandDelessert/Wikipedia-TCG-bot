"""wiki-tcg.com — pas encore implémenté.

Aucun projet de référence n'existe pour ce site et il n'a pas pu être inspecté. Lancez le
workflow « Probe » (cf. README) : il décrit les pages, les boutons et les appels réseau, ce
qui permet d'écrire ce module.
"""
from __future__ import annotations

from ..config import Settings
from ..models import SiteError, SiteResult


class WikiTcgCom:
    name = "wiki_tcg_com"
    label = "WikiTCG (wiki-tcg.com)"

    def configured(self, settings: Settings) -> bool:
        return False

    def run(self, settings: Settings) -> SiteResult:
        raise SiteError(
            "wiki-tcg.com n'est pas encore pris en charge : lancez le workflow « Probe »."
        )
