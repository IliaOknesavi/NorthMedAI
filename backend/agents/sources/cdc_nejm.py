"""
agents/sources/cdc_nejm.py — крупные медицинские издания через Yandex Search.

Whitelist: CDC, NEJM, JAMA, BMJ, Lancet. У большинства из них контент за
пейволлом — берём только snippet'ы выдачи (без отдельного fetch'а).
"""

from __future__ import annotations

from ..types import SourceTier
from ._whitelist import WhitelistAdapter


class CDCNEJMAdapter(WhitelistAdapter):
    tier: SourceTier = "cdc_nejm"
    base_weight: float = 0.80
    WHITELIST_DOMAINS = [
        "cdc.gov",
        "nejm.org",
        "bmj.com",
        "jamanetwork.com",
        "thelancet.com",
    ]
    DEFAULT_LANG = "en"
    FETCH_PDF = False
    MAX_PER_ADAPTER = 3


ADAPTER = CDCNEJMAdapter()
