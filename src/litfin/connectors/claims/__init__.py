"""Claims-agent sources.

`routing.py` (Phase 5) reads the four COURT-published assignment lists. Those
are government pages with no terms to review, and they yield a standalone
chapter 11 census plus the vendor routing table.

`stretto.py` (Phase 7) reads ONE vendor's public case index -- the only one of
eight whose ToS review cleared. Seven refuse, prohibit automated access in
terms, publish no terms, or gate on a click-through agreement; the verbatim
clauses are in `compliance/registry.py`. Read them before adding a vendor
here, and note that `claims_kroll` and `nc_business_court` require affirmative
written permission that does not exist.
"""

from . import routing, stretto

__all__ = ["routing", "stretto"]
