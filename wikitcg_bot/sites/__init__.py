from __future__ import annotations

from .base import Site
from .wiki_tcg_com import WikiTcgCom
from .wikigacha import WikiGacha
from .wikimasters import WikiMasters
from .wikitcg_net import WikiTcgNet

SITES: dict[str, Site] = {
    site.name: site for site in (WikiMasters(), WikiGacha(), WikiTcgNet(), WikiTcgCom())
}

__all__ = ["SITES", "Site"]
