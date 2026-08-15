"""CourtListener docket alerts -- the monitoring half of Phase 3.

Search (courtlistener.py) is DISCOVERY: it finds dockets carrying a deal
signal. This module is MONITORING: it subscribes to those dockets so future
activity arrives by push instead of by polling.

Why push is the only viable design: the read API caps at 50/hour (75 on a $10
membership). Polling even a few hundred dockets daily would exhaust that
before breakfast. Docket alerts are unlimited on a paid membership, they cost
no read quota, and -- a genuinely useful side effect -- subscribing causes
CourtListener to actively scrape that docket, which improves the data quality
for the case you just decided you care about.

Requires an API token. Without one the create call returns 401/403 and the
alert is recorded as failed with a clear reason rather than silently skipped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx

from ..config import Config
from ..net.client import PoliteClient
from ..store.db import Database

log = logging.getLogger("litfin.alerts")

ALERTS_URL = "https://www.courtlistener.com/api/rest/v4/docket-alerts/"

# alert_type on the docket-alerts endpoint: 1 = subscribe, 0 = unsubscribe.
SUBSCRIBE = 1
UNSUBSCRIBE = 0


@dataclass(slots=True)
class SubscribeReport:
    considered: int = 0
    created: int = 0
    already: int = 0
    failed: int = 0
    skipped_no_token: bool = False
    errors: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        if self.skipped_no_token:
            return (
                "**No CourtListener token configured.** Docket alerts require "
                "authentication.\n\n"
                "Set `COURTLISTENER_TOKEN` in `.env` (get one at "
                "https://www.courtlistener.com/profile/api/). The free tier "
                "allows 5 docket alerts; a $10/mo membership makes them "
                "unlimited and also raises the read ceiling from 50 to 75 "
                "requests/hour."
            )
        lines = [
            "| docket alerts | count |", "|---|---:|",
            f"| candidates considered | {self.considered} |",
            f"| subscribed | {self.created} |",
            f"| already subscribed | {self.already} |",
            f"| failed | {self.failed} |",
        ]
        if self.errors:
            lines += ["", "Errors (first 5):", ""]
            lines += [f"- {e}" for e in self.errors[:5]]
        return "\n".join(lines)


def subscribe_new(
    cfg: Config, db: Database, client: PoliteClient, *, limit: int = 25,
) -> SubscribeReport:
    """Subscribe to the highest-signal dockets we have not yet alerted on."""
    report = SubscribeReport()

    token = (cfg.courtlistener_token or "").strip()
    if not token:
        report.skipped_no_token = True
        return report

    candidates = db.candidate_dockets(limit=limit)
    report.considered = len(candidates)
    if not candidates:
        return report

    # POST is not routed through PoliteClient.get(), so the compliance gate is
    # asserted explicitly here. Every outbound call in this project must pass
    # it, without exception.
    from ..compliance.registry import get_policy
    from ..compliance.status import assert_fetch_allowed

    policy = get_policy("courtlistener")
    assert_fetch_allowed(
        policy, ALERTS_URL, purpose=cfg.purpose, opt_in=cfg.unverified_opt_in
    )

    headers = {
        "User-Agent": cfg.identity.user_agent,
        # The literal word "Token" is required.
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    with httpx.Client(timeout=cfg.timeout_seconds, follow_redirects=True) as http:
        for row in candidates:
            docket_id = row["docket_id"]
            if docket_id is None:
                continue
            # Share the same rate bucket as every other CourtListener call --
            # alerts may be unlimited in quota terms, but the host is not.
            key = client.governor.acquire(ALERTS_URL)
            try:
                resp = http.post(
                    ALERTS_URL,
                    headers=headers,
                    content=json.dumps(
                        {"docket": int(docket_id), "alert_type": SUBSCRIBE}
                    ),
                )
            except httpx.HTTPError as exc:
                report.failed += 1
                report.errors.append(f"docket {docket_id}: {type(exc).__name__}")
                db.record_alert(
                    docket_id=int(docket_id), status="failed",
                    case_name=row["case_name"] or "",
                    docket_url=row["docket_url"] or "",
                    error=str(exc)[:300],
                )
                continue
            finally:
                client.governor.release(key)

            if resp.status_code in (200, 201):
                body = _json(resp)
                db.record_alert(
                    docket_id=int(docket_id),
                    status="active",
                    cl_alert_id=body.get("id"),
                    case_name=row["case_name"] or "",
                    docket_url=row["docket_url"] or "",
                    reason=f"{row['hits']} matching docket entries discovered",
                )
                report.created += 1
            elif resp.status_code == 400 and "already" in resp.text.lower():
                db.record_alert(
                    docket_id=int(docket_id), status="active",
                    case_name=row["case_name"] or "",
                    docket_url=row["docket_url"] or "",
                    reason="already subscribed",
                )
                report.already += 1
            else:
                report.failed += 1
                detail = resp.text[:180].replace("\n", " ")
                report.errors.append(
                    f"docket {docket_id}: HTTP {resp.status_code} {detail}"
                )
                db.record_alert(
                    docket_id=int(docket_id), status="failed",
                    case_name=row["case_name"] or "",
                    docket_url=row["docket_url"] or "",
                    error=f"HTTP {resp.status_code}: {detail}",
                )
                # 403 on the FIRST attempt almost always means the free tier's
                # 5-alert limit, or a token without alert scope. Stop rather
                # than hammering the endpoint with 24 more doomed requests.
                if resp.status_code in (401, 403):
                    report.errors.append(
                        "stopping early: 401/403 suggests the token is invalid "
                        "or the free-tier 5-alert limit is reached"
                    )
                    break

    return report


def _json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
