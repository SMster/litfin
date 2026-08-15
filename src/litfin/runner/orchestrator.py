"""Run orchestration.

Responsibilities:
  * enumerate tasks BEFORE execution and journal them (so a killed run resumes)
  * isolate each connector so one failure cannot take down the run
  * apply watermark filtering OUTSIDE parse(), which is what makes the
    rows_parsed vs rows_new canary comparison possible
  * commit items + watermark + task status atomically
  * emit a run report where FAILURES appear at the TOP

A BROKEN canary deliberately does NOT advance the watermark, so once the
parser is fixed the data it failed to read is re-read rather than skipped
forever.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..canary.framework import CanaryFailure, Verdict, check_staleness, classify
from ..compliance.registry import get_policy
from ..compliance.status import ComplianceError
from ..config import Config
from ..connectors.base import Connector, FetchTask
from ..net.breaker import CircuitOpen
from ..net.budget import BudgetExceeded
from ..net.client import ConsentRefused, FetchBlocked, PoliteClient
from ..net.ratelimit import HostDailyCapExceeded, HostHourlyCapExceeded
from ..store.artifacts import ArtifactStore
from ..store.db import Database, Item

log = logging.getLogger("litfin.runner")


@dataclass(slots=True)
class TaskOutcome:
    source_id: str
    task_key: str
    verdict: Verdict
    rows_parsed: int = 0
    rows_new: int = 0
    inserted: int = 0
    note: str = ""
    error: str | None = None
    partial: bool = False
    coverage_note: str = ""

    @property
    def failed(self) -> bool:
        return self.verdict in (Verdict.BROKEN, Verdict.DEGRADED)


@dataclass(slots=True)
class RunReport:
    run_id: str
    started_at: str
    purpose: str
    outcomes: list[TaskOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[TaskOutcome]:
        return [o for o in self.outcomes if o.failed]

    @property
    def partials(self) -> list[TaskOutcome]:
        return [o for o in self.outcomes if o.partial]

    @property
    def total_new(self) -> int:
        return sum(o.inserted for o in self.outcomes)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append(f"# LitFin run {self.run_id}")
        lines.append("")
        lines.append(f"- started: {self.started_at}")
        # The declared purpose appears on EVERY report, so the assumption that
        # gates source availability is visible rather than buried in config.
        lines.append(f"- declared purpose: **{self.purpose}**")
        lines.append(f"- new items: **{self.total_new}**")
        lines.append("")

        if self.failures:
            lines.append("## FAILURES")
            lines.append("")
            for o in self.failures:
                lines.append(f"- **{o.verdict}** `{o.source_id}` / `{o.task_key}`")
                if o.error:
                    lines.append(f"  - {o.error}")
                if o.note:
                    lines.append(f"  - {o.note}")
            lines.append("")

        if self.partials:
            # Loud, but NOT a failure: the retrieved rows are good and were
            # stored. This exists so a truncated slice is never mistaken for
            # a complete one.
            lines.append("## PARTIAL COVERAGE")
            lines.append("")
            lines.append(
                "These slices hit a hard API page cap. The rows retrieved "
                "were kept (newest first); older entries in the same slice "
                "were not retrieved."
            )
            lines.append("")
            for o in self.partials:
                lines.append(f"- `{o.source_id}` / `{o.task_key}`")
                if o.coverage_note:
                    lines.append(f"  - {o.coverage_note}")
            lines.append("")

        if self.blocked:
            lines.append("## Blocked by compliance gate")
            lines.append("")
            lines.extend(f"- {b}" for b in self.blocked)
            lines.append("")

        if self.warnings:
            lines.append("## Warnings")
            lines.append("")
            lines.extend(f"- {w}" for w in self.warnings)
            lines.append("")

        lines.append("## Results")
        lines.append("")
        lines.append("| source | task | verdict | parsed | new | inserted |")
        lines.append("|---|---|---|---:|---:|---:|")
        for o in self.outcomes:
            lines.append(
                f"| {o.source_id} | {o.task_key} | {o.verdict} | "
                f"{o.rows_parsed} | {o.rows_new} | {o.inserted} |"
            )
        lines.append("")
        return "\n".join(lines)


class Orchestrator:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        client: PoliteClient,
        artifacts: ArtifactStore,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.client = client
        self.artifacts = artifacts

    def run(
        self,
        connectors: list[Connector],
        *,
        run_id: str | None = None,
    ) -> RunReport:
        run_id = run_id or f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
        started = datetime.now(timezone.utc).isoformat()
        self.db.start_run(run_id, str(self.cfg.purpose))

        report = RunReport(run_id=run_id, started_at=started, purpose=str(self.cfg.purpose))

        for connector in connectors:
            policy = get_policy(connector.source_id)
            enabled = policy.is_enabled(self.cfg.purpose, self.cfg.unverified_opt_in)
            self.db.upsert_source(
                connector.source_id,
                display_name=policy.display_name or connector.source_id,
                tier=policy.tier,
                status=str(policy.status),
                base_confidence=policy.base_confidence,
                enabled=enabled,
            )
            if not enabled:
                try:
                    policy.assert_enabled(self.cfg.purpose, self.cfg.unverified_opt_in)
                except ComplianceError as exc:
                    report.blocked.append(f"`{connector.source_id}`: {exc}")
                continue

            # Per-connector isolation: one bad connector cannot end the run.
            try:
                self._run_connector(connector, run_id, report)
            except Exception as exc:  # noqa: BLE001 - deliberate catch-all
                log.exception("connector %s crashed", connector.source_id)
                report.outcomes.append(
                    TaskOutcome(
                        source_id=connector.source_id,
                        task_key="(connector)",
                        verdict=Verdict.BROKEN,
                        error=f"{type(exc).__name__}: {exc}",
                        note=traceback.format_exc(limit=3),
                    )
                )

            # Let a connector persist something plan() needs next run (e.g. a
            # discovered document URL). Opt-in: only connectors that define
            # plan_watermark() participate.
            plan_hint = getattr(connector, "plan_watermark", None)
            if callable(plan_hint):
                hint = plan_hint()
                if hint:
                    self.db.commit_task(
                        run_id=run_id,
                        task_id=f"{connector.source_id}:_plan",
                        source_id=connector.source_id,
                        task_key="_plan",
                        items=[],
                        watermark_value=hint,
                        seen_keys=[],
                        rows_parsed=0, rows_new=0,
                    )

            warning = check_staleness(self.db.conn, connector.source_id)
            if warning:
                report.warnings.append(warning)

        for source_id, signal in self.client.stats.ai_signals.items():
            report.warnings.append(
                f"`{source_id}` robots.txt expresses an automated-access "
                f"preference ({signal}). The '*' group permits us; surfacing "
                f"it so a human knows it exists."
            )

        self.db.finish_run(run_id, "ok" if report.ok else "failed")
        self._write_report(report)
        return report

    def _run_connector(
        self, connector: Connector, run_id: str, report: RunReport
    ) -> None:
        # Pass the connector-level watermark into plan(). Previously this was
        # hardcoded to None, which made the declared interface a lie and ruled
        # out any connector whose task URLs are DISCOVERED rather than known
        # upfront -- JPML publishes its MDL list as a dated PDF whose filename
        # changes monthly, so it stores the discovered URL here and fetches it
        # on the following run.
        plan_wm, _ = self.db.get_watermark(connector.source_id, "_plan")
        for task in connector.plan(plan_wm):
            outcome = self._run_task(connector, task, run_id)
            report.outcomes.append(outcome)
            self.db.set_health(
                connector.source_id,
                run_id=run_id,
                verdict=str(outcome.verdict),
                rows_parsed=outcome.rows_parsed,
                rows_new=outcome.rows_new,
                note=outcome.note or outcome.error,
            )

    def _run_task(
        self, connector: Connector, task: FetchTask, run_id: str
    ) -> TaskOutcome:
        source_id = connector.source_id

        # --- fetch -------------------------------------------------------
        try:
            resp = self.client.get(
                task.url,
                source_id=source_id,
                accept=task.accept,
                conditional=task.conditional,
            )
        except ConsentRefused as exc:
            return TaskOutcome(
                source_id, task.task_key, Verdict.BROKEN,
                error=str(exc),
                note="Treated as refusal of consent. Do not work around it.",
            )
        except (CircuitOpen, BudgetExceeded, HostDailyCapExceeded,
                HostHourlyCapExceeded) as exc:
            # The host is down or we are out of budget -- NOT a parser fault,
            # and the watermark stays put so the next run backfills.
            return TaskOutcome(
                source_id, task.task_key, Verdict.DEGRADED, error=str(exc)
            )
        except (FetchBlocked, ComplianceError) as exc:
            return TaskOutcome(
                source_id, task.task_key, Verdict.DEGRADED, error=str(exc)
            )
        except Exception as exc:  # noqa: BLE001
            return TaskOutcome(
                source_id, task.task_key, Verdict.DEGRADED,
                error=f"{type(exc).__name__}: {exc}",
            )

        # --- persist the raw artifact BEFORE parsing ---------------------
        stored = None
        if resp.body and resp.sha256:
            stored = self.artifacts.put(
                sha256=resp.sha256,
                body=resp.body,
                source_id=source_id,
                url=task.url,
                content_type=resp.content_type,
            )
            self.db.register_artifact(
                sha256=resp.sha256,
                source_id=source_id,
                url=task.url,
                http_status=resp.status,
                content_type=resp.content_type,
                byte_size=len(resp.body),
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
                run_id=run_id,
                ext=stored.ext,
                compressed=stored.compressed,
            )

        # --- structural canary -------------------------------------------
        expectation = connector.expectation(task)
        if not resp.not_modified:
            try:
                expectation.assert_ok(
                    source_id, body=resp.body, content_type=resp.content_type
                )
            except CanaryFailure as exc:
                return TaskOutcome(
                    source_id, task.task_key, Verdict.BROKEN, error=str(exc)
                )

        # Optional per-connector semantic canary. Used where a structural check
        # is not enough -- e.g. sec_fts asserting a slice did not saturate the
        # 10k result cap, which returns a perfectly valid 200 while silently
        # truncating.
        custom_canary = getattr(connector, "canary", None)
        if callable(custom_canary) and resp.body:
            try:
                custom_canary(resp.body, task)
            except CanaryFailure as exc:
                return TaskOutcome(
                    source_id, task.task_key, Verdict.BROKEN, error=str(exc)
                )

        # --- parse (pure) -------------------------------------------------
        try:
            parsed = connector.parse(resp.body, task.url)
        except Exception as exc:  # noqa: BLE001
            return TaskOutcome(
                source_id, task.task_key, Verdict.BROKEN,
                error=f"parse raised {type(exc).__name__}: {exc}",
            )

        for it in parsed.items:
            it.artifact_sha256 = resp.sha256 or None

        # --- watermark filtering (OUTSIDE parse) -------------------------
        prev_value, seen = self.db.get_watermark(source_id, task.task_key)
        new_items = [i for i in parsed.items if i.natural_key not in seen]

        verdict = classify(
            source_id,
            rows_parsed=parsed.rows_parsed,
            rows_new=len(new_items),
            not_modified=resp.not_modified,
            has_body=bool(resp.body),
            server_reported_empty=parsed.server_reported_empty,
            byte_size=len(resp.body),
        )

        status = "OK" if verdict.verdict is Verdict.HEALTHY else "BROKEN"
        merged_seen = list(seen) + [i.natural_key for i in new_items]
        new_wm = connector.watermark_for(parsed.items) or prev_value

        inserted = self.db.commit_task(
            run_id=run_id,
            task_id=task.task_id,
            source_id=source_id,
            task_key=task.task_key,
            items=new_items if status == "OK" else [],
            watermark_value=new_wm,
            seen_keys=merged_seen,
            rows_parsed=parsed.rows_parsed,
            rows_new=len(new_items),
            status=status,
        )

        return TaskOutcome(
            source_id=source_id,
            task_key=task.task_key,
            verdict=verdict.verdict,
            rows_parsed=verdict.rows_parsed,
            rows_new=verdict.rows_new,
            inserted=inserted,
            note=verdict.note,
            partial=parsed.partial_coverage,
            coverage_note=parsed.coverage_note,
        )

    def _write_report(self, report: RunReport) -> Path:
        d = self.cfg.runs_dir / report.run_id
        d.mkdir(parents=True, exist_ok=True)
        path = d / "report.md"
        path.write_text(report.to_markdown(), encoding="utf-8")
        return path
