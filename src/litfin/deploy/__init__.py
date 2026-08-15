"""Deployment support: readiness checks, publishing, and scheduling.

`preflight.py` is the gate. It refuses a hosted deployment whose security or
compliance posture is not actually resolved -- chiefly the CourtListener
RESEARCH_ONLY scope question, which hosting makes live and which no amount of
code can answer.

`publish.py` builds the dashboard bundle for a static host. The hosted
artifact fetches nothing, so the purpose question does not arise for it -- but
it names real parties, so publishing refuses to target anywhere obviously
world-readable.
"""

from . import preflight, publish

__all__ = ["preflight", "publish"]
