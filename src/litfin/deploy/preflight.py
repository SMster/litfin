"""Deployment readiness checks.

`litfin preflight` answers one question: is it safe and lawful to run this on
a host other people can reach? It exits non-zero when the answer is no.

WHY THIS EXISTS RATHER THAN A PARAGRAPH IN A README. Everything this project
does about compliance is enforced in code, because a rule that only lives in
prose is a rule that gets skipped at 2am. Hosting changes two things at once
-- the security surface and the legal posture -- and both of them fail
silently. An unauthenticated panel boots fine. A source used outside its
permitted purpose returns 200.

THE CENTRAL CHECK IS THE COURTLISTENER SCOPE QUESTION, and it is the one no
amount of engineering can answer. Free Law Project permits "personal,
educational, research, journalistic, and exploratory use" but bars building
"tools for for-profit or non-profit organizations, even if those tools aren't
sold." A personal research project on a laptop clears that clause. The same
code on an always-on host serving a team is, at best, arguable.

So preflight will not let a hosted deployment start until a human has recorded
an answer in litfin.toml:

    [deployment]
    courtlistener_scope_resolved = "emailed FLP 2026-08-20, confirmed OK for
                                    our use -- see docs/tos/flp-reply.eml"

The field takes free text on purpose. There is no boolean that could honestly
represent "we asked and they said yes", and a boolean is exactly what somebody
would flip without asking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Level(StrEnum):
    FAIL = "FAIL"     # refuses to deploy
    WARN = "WARN"     # deploy proceeds, but somebody should look
    OK = "OK"


@dataclass(slots=True)
class Check:
    level: Level
    name: str
    detail: str
    fix: str = ""


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)
    hosted: bool = True

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.level is Level.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level is Level.WARN]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_text(self) -> str:
        icon = {Level.FAIL: "FAIL", Level.WARN: "WARN", Level.OK: "  ok"}
        lines = [
            f"Preflight — {'HOSTED deployment' if self.hosted else 'local use'}",
            "=" * 68,
        ]
        for c in self.checks:
            lines.append(f"[{icon[c.level]}] {c.name}")
            if c.level is not Level.OK:
                for para in c.detail.split("\n"):
                    lines.append(f"        {para}")
                if c.fix:
                    lines.append(f"        FIX: {c.fix}")
        lines.append("=" * 68)
        if self.failures:
            lines.append(
                f"{len(self.failures)} blocking problem(s). "
                f"Not safe to deploy."
            )
        elif self.warnings:
            lines.append(f"Clear to deploy, with {len(self.warnings)} warning(s).")
        else:
            lines.append("All checks passed.")
        return "\n".join(lines)


def run(cfg, *, hosted: bool = True, host: str = "0.0.0.0") -> Report:
    """Check deployment readiness. `hosted=False` relaxes the network checks
    but keeps every compliance one."""
    import os

    from ..compliance.registry import POLICIES
    from ..compliance.status import Purpose, ToSStatus
    from ..config import is_placeholder_contact
    from ..deliver import auth as auth_mod

    r = Report(hosted=hosted)
    add = r.checks.append

    # -- 1. identity -------------------------------------------------------
    if is_placeholder_contact(cfg.identity.contact_email):
        add(Check(
            Level.FAIL, "Contact address is a placeholder",
            f"The User-Agent would announce "
            f"{cfg.identity.contact_email!r} on every request.\n"
            f"An unedited placeholder is worse than declaring nothing: it "
            f"looks like a real contact to a site operator and reaches no "
            f"one.",
            "Set LITFIN_CONTACT_EMAIL in .env to a mailbox you actually read.",
        ))
    else:
        add(Check(Level.OK, f"Contact address: {cfg.identity.contact_email}", ""))

    # -- 2. THE scope question --------------------------------------------
    cl = POLICIES.get("courtlistener")
    research_only_enabled = [
        p.source_id for p in POLICIES.values()
        if p.status is ToSStatus.RESEARCH_ONLY
        and p.is_enabled(cfg.purpose, cfg.unverified_opt_in)
    ]
    resolved = (getattr(cfg, "courtlistener_scope_resolved", "") or "").strip()

    if hosted and research_only_enabled and not resolved:
        add(Check(
            Level.FAIL,
            "CourtListener scope question is unresolved",
            "RESEARCH_ONLY source(s) enabled on a HOSTED deployment: "
            f"{', '.join(research_only_enabled)}.\n"
            "Free Law Project permits \"personal, educational, research, "
            "journalistic, and exploratory use\" but bars building \"tools "
            "for for-profit or non-profit organizations, even if those tools "
            "aren't sold.\"\n"
            "A research project on your laptop clears that. An always-on host "
            "serving a team is at best arguable, and this is not a question "
            "code can answer.",
            "Email partnerships@free.law describing the deployment, then "
            "record the reply in litfin.toml:\n"
            "             [deployment]\n"
            "             courtlistener_scope_resolved = \"<who, when, what "
            "they said>\"\n"
            "        Or set purpose = \"commercial\", which disables "
            "CourtListener loudly rather than using it under terms that may "
            "not apply.",
        ))
    elif research_only_enabled and resolved:
        add(Check(Level.OK, f"CourtListener scope recorded: {resolved[:60]}", ""))
    elif not research_only_enabled:
        add(Check(
            Level.OK,
            f"No RESEARCH_ONLY source is enabled (purpose={cfg.purpose})", ""
        ))

    # -- 3. purpose is still honest ---------------------------------------
    if hosted and cfg.purpose is Purpose.RESEARCH:
        add(Check(
            Level.WARN, "purpose is still \"research\" on a hosted deployment",
            "That may well be correct -- a hosted research project is still "
            "research. But it is the declaration every source policy is "
            "evaluated against, so it should be a decision rather than a "
            "leftover.",
            "If this becomes firm infrastructure, set purpose = "
            "\"commercial\" and let the affected sources disable loudly.",
        ))

    # -- 4. expiring reviews ----------------------------------------------
    from datetime import date, timedelta

    soon = date.today() + timedelta(days=60)
    expiring = [
        (p.source_id, p.expires_at) for p in POLICIES.values()
        if p.expires_at and p.expires_at <= soon
        and p.is_enabled(cfg.purpose, cfg.unverified_opt_in)
    ]
    if expiring:
        add(Check(
            Level.WARN, f"{len(expiring)} ToS review(s) expire within 60 days",
            "\n".join(f"{sid}: {exp}" for sid, exp in expiring)
            + "\nAn expired review disables its source, mid-schedule.",
            "Re-read the terms and run: litfin compliance review <source_id>",
        ))

    machine_read = [
        p.source_id for p in POLICIES.values()
        if (p.reviewed_by or "").startswith("machine-assisted")
        and p.is_enabled(cfg.purpose, cfg.unverified_opt_in)
    ]
    if machine_read:
        add(Check(
            Level.WARN,
            f"{len(machine_read)} enabled source(s) rest on a machine-assisted "
            f"terms read",
            f"{', '.join(machine_read)} — recorded with verbatim quotes, but "
            f"a model reading a contract is not counsel signing off.\n"
            f"The refusals are the safe direction; a permission is the one to "
            f"confirm before hosting.",
            "Have a human confirm, then update reviewed_by in registry.py.",
        ))

    # -- 5. web authentication --------------------------------------------
    if hosted:
        acfg = auth_mod.from_environment()
        try:
            auth_mod.require_for_public_bind(acfg, host)
            add(Check(Level.OK, "Web authentication configured", ""))
        except auth_mod.AuthMisconfigured as exc:
            add(Check(
                Level.FAIL, "Web authentication not configured",
                str(exc).split("\n\n")[0]
                + "\nThe panel can spend money on the Anthropic API and shows "
                  "case analysis about named parties in real litigation.",
                "Set LITFIN_WEB_USER, LITFIN_WEB_PASSWORD (16+ chars) and "
                "LITFIN_SESSION_SECRET in .env, or run with --read-only "
                "behind an identity proxy.",
            ))

    # -- 6. secrets --------------------------------------------------------
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        add(Check(
            Level.WARN, "ANTHROPIC_API_KEY is not set",
            "Collection and ranking work without it; extraction does not.",
            "Set it in .env, or accept that scheduled extract runs will fail.",
        ))

    # -- 7. the send gate --------------------------------------------------
    smtp_ready = all(
        os.environ.get(k, "").strip()
        for k in ("LITFIN_SMTP_HOST", "LITFIN_SMTP_FROM")
    )
    if cfg.send_enabled:
        if not cfg.recipient_allowlist:
            add(Check(
                Level.FAIL, "send_enabled is true with an EMPTY allowlist",
                "The mailer would refuse every send at runtime, so a "
                "scheduled digest would fail silently every week.",
                "Set LITFIN_RECIPIENTS in .env.",
            ))
        elif not smtp_ready:
            add(Check(
                Level.FAIL, "send_enabled is true but SMTP is not configured",
                "Every scheduled send would raise SendRefused.",
                "Set LITFIN_SMTP_HOST and LITFIN_SMTP_FROM in .env.",
            ))
        else:
            add(Check(
                Level.OK,
                f"Live send ARMED to: {', '.join(cfg.recipient_allowlist)}", ""
            ))
    else:
        add(Check(
            Level.WARN, "Digest send is in DRY RUN",
            "Scheduled digests will render to runs/<date>/digest.html and "
            "transmit nothing."
            + ("" if smtp_ready else "\nSMTP is also not configured yet."),
            "When ready: set SMTP in .env, send one test to yourself, then "
            "set deliver.send_enabled = true in litfin.toml.",
        ))

    # -- 8. data root ------------------------------------------------------
    from pathlib import Path

    root = Path(cfg.data_root)
    if hosted and not root.is_absolute():
        add(Check(
            Level.FAIL, "paths.data_root is relative",
            f"{cfg.data_root!r} would resolve against the working directory "
            f"and land inside the container on every restart.",
            "Use an absolute path mounted on a persistent volume.",
        ))
    else:
        add(Check(Level.OK, f"Data root: {cfg.data_root}", ""))

    return r
