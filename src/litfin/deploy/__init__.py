"""Deployment support: readiness checks and scheduling.

`preflight.py` is the gate. It refuses a hosted deployment whose security or
compliance posture is not actually resolved -- chiefly the CourtListener
RESEARCH_ONLY scope question, which hosting makes live and which no amount of
code can answer.
"""

from . import preflight

__all__ = ["preflight"]
