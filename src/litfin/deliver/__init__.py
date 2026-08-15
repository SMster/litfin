"""Delivery: the dashboard, the email digest, and the local control panel.

One assembly step (`dataset.load`) feeds three renderers, so the email and the
dashboard can never disagree about what ranked first.

The send gate lives in `mailer.py` and defaults closed. Read the module
docstring there before changing anything about it.
"""

from . import dashboard, dataset, digest, excel, mailer, server

__all__ = ["dashboard", "dataset", "digest", "excel", "mailer", "server"]
