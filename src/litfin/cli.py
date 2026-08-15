"""litfin command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .compliance.registry import POLICIES, get_policy
from .compliance.status import ComplianceError, ToSStatus
from .config import Config, load_config
from .connectors import (
    courtlistener, doj_cases, edgar_index, feeds, govinfo, jpml, sec_fts,
    state_ag,
)
from .connectors.claims import routing as claims_routing
from .connectors.claims import stretto as claims_stretto
from .net.budget import GlobalBudget
from .net.client import PoliteClient
from .runner.orchestrator import Orchestrator
from .store.artifacts import ArtifactStore
from .store.db import Database


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _open(cfg: Config) -> tuple[Database, PoliteClient, ArtifactStore]:
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    budget = GlobalBudget(
        db.conn,
        max_per_day=cfg.max_requests_per_day,
        warn_at_fraction=cfg.warn_at_fraction,
    )
    client = PoliteClient(cfg, budget=budget)
    artifacts = ArtifactStore(cfg.raw_dir, cfg.manifest_dir)
    return db, client, artifacts


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    db, client, artifacts = _open(cfg)
    try:
        connectors = [
            *feeds.all_connectors(),
            doj_cases.build(),
            sec_fts.build(lookback_days=args.lookback),
            courtlistener.build_search(lookback_days=args.cl_lookback),
            edgar_index.build(lookback_days=args.lookback),
            state_ag.build(),
            govinfo.build(lookback_days=args.lookback),
        ]
        if args.weekly:
            # JPML moves by a handful of entries a month and uscourts.gov
            # declares Crawl-delay: 10. Daily polling would spend budget for
            # nothing. The claims-agent lists move at the same pace -- a
            # handful of mega cases a month.
            connectors.append(jpml.build())
            connectors.append(claims_routing.build())
            # Phase 7. The gate lets this through only because its ToS review
            # cleared; every other Tier B vendor stays refused and this line
            # is the whole difference.
            connectors.append(claims_stretto.build())
        if args.source:
            connectors = [c for c in connectors if c.source_id in set(args.source)]
            if not connectors:
                print(f"No connector matches {args.source!r}", file=sys.stderr)
                return 2

        orch = Orchestrator(cfg, db, client, artifacts)
        report = orch.run(connectors)

        print(report.to_markdown())
        print(f"\nReport written to: {cfg.runs_dir / report.run_id / 'report.md'}")
        # Non-zero exit on failure, so a scheduled task surfaces the problem.
        return 0 if report.ok else 1
    finally:
        client.close()
        db.close()


def cmd_compliance_review(args: argparse.Namespace) -> int:
    """Read a source's terms, then emit the registry change to commit.

    Deliberately does NOT edit registry.py. Compliance state lives in version
    control, and a determination should arrive as a reviewed diff with a
    verbatim quote attached — not as something a command mutated on disk.
    """
    import textwrap

    cfg = load_config(Path(args.config) if args.config else None)
    policy = get_policy(args.source_id)

    print(f"source_id : {policy.source_id}")
    print(f"name      : {policy.display_name}")
    print(f"tier      : {policy.tier}")
    print(f"status    : {policy.status}")
    print(f"purpose   : {cfg.purpose} (declared in litfin.toml)")
    if policy.reviewed_by:
        print(f"reviewed  : {policy.reviewed_by} on {policy.reviewed_at}")
        print(f"expires   : {policy.expires_at}")
    print()

    if policy.status is ToSStatus.PROHIBITED:
        print("This source is PROHIBITED. Its terms are not re-litigated by "
              "re-reading them, and there is no configuration that enables "
              "it.")
        print(f"\nOn record:\n{textwrap.indent(policy.review_note or '', '  ')}")
        return 1

    if not policy.tos_urls:
        print("No terms URLs on record. Add them to the registry first.")
        return 2

    print("Terms to read:")
    for u in policy.tos_urls:
        print(f"  {u}")

    if args.fetch:
        from .net.budget import GlobalBudget
        from .net.client import PoliteClient

        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        budget = GlobalBudget(
            db.conn, max_per_day=cfg.max_requests_per_day,
            warn_at_fraction=cfg.warn_at_fraction,
        )
        client = PoliteClient(cfg, budget=budget)
        try:
            for url in policy.tos_urls:
                print(f"\n{'=' * 72}\n{url}\n{'=' * 72}")
                try:
                    # reading_terms restricts this to the policy's OWN
                    # declared tos_urls and still honors robots.
                    r = client.get(
                        url, source_id=policy.source_id,
                        reading_terms=True, conditional=False,
                    )
                except Exception as exc:
                    print(f"  {type(exc).__name__}: {str(exc)[:400]}")
                    continue
                print(_readable(r.body)[: args.chars])
        finally:
            client.close()
            db.close()

    if not args.record:
        print(
            "\nWhen you have read them, record the determination:\n"
            f"  litfin compliance review {policy.source_id} --record \\\n"
            f"      --verdict verified_permitted \\\n"
            f'      --by "Your Name" \\\n'
            f'      --quote "<verbatim text of the operative clause>"'
        )
        return 0

    if not (args.verdict and args.by and args.quote):
        print("--record needs --verdict, --by and --quote.", file=sys.stderr)
        return 2
    try:
        verdict = ToSStatus(args.verdict)
    except ValueError:
        print(f"--verdict must be one of: "
              f"{', '.join(str(s) for s in ToSStatus)}", file=sys.stderr)
        return 2

    from datetime import date, timedelta

    today = date.today()
    expiry = today + timedelta(days=365)
    quote = args.quote.replace('"', '\\"')

    print(f"\n{'=' * 72}")
    print("Paste this into src/litfin/compliance/registry.py, replacing the")
    print(f"existing {policy.source_id!r} entry's status/review fields, then")
    print("commit it. Reviews live in git, not in a database the app can")
    print("mutate.")
    print("=" * 72)
    print(f"""    status=ToSStatus.{verdict.name},
    reviewed_by="{args.by}",
    reviewed_at=date({today.year}, {today.month}, {today.day}),
    expires_at=date({expiry.year}, {expiry.month}, {expiry.day}),
    review_note=(
{textwrap.indent(_wrap_quote(quote), '        ')}
    ),""")
    if verdict is ToSStatus.UNVERIFIED:
        print(f"\nAlso add {policy.source_id!r} to [compliance].unverified_opt_in "
              f"in litfin.toml if you intend to enable it.")
    return 0


def _readable(raw: bytes) -> str:
    """HTML -> text, for reading terms in a terminal."""
    try:
        from lxml import html as LH

        doc = LH.fromstring(raw)
        for bad in doc.xpath("//script|//style|//nav|//header|//footer"):
            bad.getparent().remove(bad)
        text = doc.text_content()
    except Exception:
        text = raw.decode("utf-8", errors="replace")
    lines = [" ".join(l.split()) for l in text.splitlines()]
    return "\n".join(l for l in lines if l)


def _wrap_quote(quote: str) -> str:
    import textwrap

    return "\n".join(
        f'"{line} "' for line in textwrap.wrap(quote, width=62)
    )


def cmd_compliance(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)

    if getattr(args, "review_source", None):
        args.source_id = args.review_source
        return cmd_compliance_review(args)

    if args.source_id:
        policy = get_policy(args.source_id)
        print(f"source_id : {policy.source_id}")
        print(f"name      : {policy.display_name}")
        print(f"tier      : {policy.tier}")
        print(f"status    : {policy.status}")
        print(f"rate key  : {policy.rate_key}")
        print(f"purpose   : {cfg.purpose} (declared in litfin.toml)")
        try:
            policy.assert_enabled(cfg.purpose, cfg.unverified_opt_in)
            print("enabled   : YES")
        except ComplianceError as exc:
            print("enabled   : NO")
            print(f"\nreason:\n{exc}")
        if policy.tos_urls:
            print("\nTerms to read:")
            for u in policy.tos_urls:
                print(f"  {u}")
        if policy.review_note:
            print(f"\nOn record:\n  {policy.review_note}")
        if policy.robots_ai_signal:
            print(f"\nrobots AI signal: {policy.robots_ai_signal}")
        if policy.notes:
            print(f"\nNotes:\n  {policy.notes}")
        return 0

    print(f"Declared purpose: {cfg.purpose}\n")
    rows = []
    for pid, policy in sorted(POLICIES.items()):
        ok = policy.is_enabled(cfg.purpose, cfg.unverified_opt_in)
        rows.append((pid, policy.tier, str(policy.status), "YES" if ok else "no"))

    w0 = max(len(r[0]) for r in rows) + 2
    w2 = max(len(r[2]) for r in rows) + 2
    print(f"{'source_id':<{w0}}{'tier':<6}{'status':<{w2}}{'enabled'}")
    print("-" * (w0 + 6 + w2 + 8))
    for pid, tier, status, ok in rows:
        print(f"{pid:<{w0}}{tier:<6}{status:<{w2}}{ok}")

    blocked = [r for r in rows if r[3] == "no"]
    if blocked:
        print(
            f"\n{len(blocked)} source(s) disabled. Run "
            f"`litfin compliance <source_id>` for the specific reason."
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        print(f"data root : {cfg.data_root}")
        print(f"database  : {cfg.db_path}")
        print(f"purpose   : {cfg.purpose}")
        print(f"items     : {db.count_items()}")
        print()
        rows = list(db.conn.execute(
            "SELECT source_id, health, last_success_at, consecutive_failures "
            "FROM source ORDER BY source_id"
        ))
        if not rows:
            print("No sources have run yet.")
            return 0
        print(f"{'source_id':<26}{'health':<12}{'fails':<7}last success")
        print("-" * 78)
        for r in rows:
            print(
                f"{r['source_id']:<26}{r['health']:<12}"
                f"{r['consecutive_failures']:<7}{r['last_success_at'] or '-'}"
            )
        return 0
    finally:
        db.close()


def cmd_coverage(args: argparse.Namespace) -> int:
    from .connectors import coverage

    cfg = load_config(Path(args.config) if args.config else None)

    if args.refresh:
        db, client, _ = _open(cfg)
        try:
            stats = coverage.refresh(client, db)
            print(stats.to_markdown())
            print(f"\n({stats.pages_fetched} pages fetched)")
        finally:
            client.close()
            db.close()
        return 0

    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        summary = db.coverage_summary()
        if not summary:
            print("No coverage map yet. Run: litfin coverage --refresh")
            return 0
        print("Venue coverage (how much to trust an EMPTY result):\n")
        labels = {
            "high": "full RSS feed",
            "partial": "partial feed (orders/opinions only)",
            "low": "NO feed -- absence of signal != absence of activity",
            "not_applicable": "not a PACER court",
        }
        for r in summary:
            print(f"  {r['n']:>4}  {r['confidence']:<16} {labels.get(r['confidence'], '')}")
        weak = db.low_coverage_courts()
        if weak:
            print(f"\nVenues where an empty result is NOT evidence of quiet "
                  f"({len(weak)} shown):\n")
            for r in weak:
                extra = f" [{r['entry_types']}]" if r["entry_types"] else ""
                print(f"  {r['confidence']:<8} {r['court_id']:<10} "
                      f"{r['full_name'][:52]}{extra}")
        return 0
    finally:
        db.close()


def cmd_alerts(args: argparse.Namespace) -> int:
    from .connectors import cl_alerts

    cfg = load_config(Path(args.config) if args.config else None)

    if args.subscribe:
        db, client, _ = _open(cfg)
        try:
            report = cl_alerts.subscribe_new(cfg, db, client, limit=args.limit)
            print(report.to_markdown())
        finally:
            client.close()
            db.close()
        return 0

    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        rows = db.alerts()
        if not rows:
            cands = db.candidate_dockets(limit=args.limit)
            print(f"No docket alerts yet. {len(cands)} candidate dockets "
                  f"discovered and ready to subscribe.\n")
            for r in cands[:15]:
                print(f"  docket {r['docket_id']:<10} {r['hits']:>2} hits  "
                      f"{(r['case_name'] or '')[:56]}")
            print("\nSubscribe with: litfin alerts --subscribe")
            return 0
        print(f"{'docket':<10}{'status':<14}{'last event':<28}case")
        print("-" * 88)
        for r in rows:
            print(f"{r['docket_id']:<10}{r['status']:<14}"
                  f"{(r['last_event_at'] or '-'):<28}{(r['case_name'] or '')[:34]}")
        return 0
    finally:
        db.close()


def cmd_webhook(args: argparse.Namespace) -> int:
    import os

    from .net import webhook

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()

    if args.drain:
        db = Database(cfg.db_path)
        try:
            stats = webhook.drain(db, limit=args.limit)
            print(f"processed      : {stats['processed']}")
            print(f"docket alerts  : {stats['docket_alerts']}")
            print(f"skipped        : {stats['skipped']}")
            print(f"errors         : {stats['errors']}")
            print()
            print("queue:", db.webhook_stats())
            return 0
        finally:
            db.close()

    if args.status:
        db = Database(cfg.db_path)
        try:
            print("webhook queue:", db.webhook_stats())
            return 0
        finally:
            db.close()

    secret = os.environ.get("LITFIN_WEBHOOK_SECRET", "").strip()
    if not secret:
        print(
            "LITFIN_WEBHOOK_SECRET is not set. The receiver needs it: "
            "CourtListener webhooks carry NO HMAC signature, so a long random "
            "URL path plus an IP allowlist is the only authentication "
            "available.\n\n"
            "Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "then put it in .env as LITFIN_WEBHOOK_SECRET=...",
            file=sys.stderr,
        )
        return 2

    wcfg = webhook.WebhookConfig(
        secret_path=secret,
        db_path=str(cfg.db_path),
        host=args.host,
        port=args.port,
        enforce_ip_allowlist=not args.allow_any_ip,
    )
    if args.allow_any_ip:
        print(
            "WARNING: IP allowlist DISABLED. Local testing only -- with no "
            "HMAC available, the allowlist is half the authentication.",
            file=sys.stderr,
        )
    print(f"Register this URL with CourtListener:")
    print(f"  https://<your-public-host>/webhook/{secret}")
    print()
    webhook.serve(wcfg)
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    """Show what the pre-LLM screens would do, without calling the API."""
    from .extract.runner import select_candidates

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        candidates, report = select_candidates(db, cfg, limit=args.limit)
        print(report.to_markdown())
        print()
        print(f"Top {min(15, len(candidates))} candidates by signal strength:")
        for c in candidates[:15]:
            print(f"  [{c.strength:.2f}] {c.thesis:22} {c.title[:70]}")
        return 0
    finally:
        db.close()


def cmd_extract(args: argparse.Namespace) -> int:
    from .extract.runner import collect_batch, extract_sync, select_candidates, submit_batch

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        if args.collect:
            total = 0
            for bid in db.open_batches():
                total += collect_batch(bid, cfg, db, wait=args.wait)
            print(f"Collected {total} extractions.")
            return 0

        candidates, report = select_candidates(
            db, cfg, limit=args.limit, refresh=args.refresh
        )
        print(report.to_markdown())
        print()
        if not candidates:
            print("Nothing to extract.")
            return 0

        if args.sync:
            n = extract_sync(candidates, cfg, db, limit=args.limit or 5)
            print(f"Stored {n} extractions (synchronous).")
        else:
            bid = submit_batch(candidates, cfg, db)
            print(f"Submitted batch {bid} with {len(candidates)} requests.")
            print("Collect with: litfin extract --collect")
        return 0
    finally:
        db.close()


def cmd_rank(args: argparse.Namespace) -> int:
    from .score.scoring import rank_all

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        report = rank_all(db, limit=cfg.top_n_dashboard, cfg=cfg)
        print(report.to_markdown())
        print()
        rows = db.top_prospects(limit=args.limit)
        if not rows:
            print("No prospects yet -- run `litfin extract` first.")
            return 0
        for i, r in enumerate(rows, start=1):
            dmg = f"${r['damages_usd']:,.0f}" if r["damages_usd"] else "—"
            comps = json.loads(r["components_json"] or "{}")
            flag = " (imputed)" if comps.get("damages_imputed") else ""
            print(f"{i:>3}. [{r['score']:.3f}] {r['case_caption'] or r['item_title']}")
            print(f"      {r['deal_thesis']} / {r['event_type']} | "
                  f"{r['venue'] or r['court'] or '?'} | damages {dmg}{flag}")
            if r["summary"]:
                print(f"      {r['summary'][:150]}")
            print()
        return 0
    finally:
        db.close()


def cmd_etrack(args: argparse.Namespace) -> int:
    """NY eTrack: enrollment worklist, alert-parser check, gated ingestion."""
    from .connectors import etrack_email as et

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()

    # --check needs no database and no gate: it parses a file you hand it.
    if args.check:
        alert = et.check_file(args.check)
        print(f"file          : {args.check}")
        print(f"subject       : {alert.subject}")
        print(f"from          : {alert.sender}")
        print(f"index number  : {alert.index_number or '(NOT FOUND)'}")
        print(f"caption       : {alert.caption or '—'}")
        print(f"court         : {alert.court or '—'}")
        print(f"county        : {alert.county or '—'}")
        print(f"event kind    : {alert.event_kind or '—'}")
        print(f"event date    : {alert.event_date or '—'}")
        print(f"labelled fields ({len(alert.fields)}):")
        for k, v in alert.fields.items():
            print(f"    {k:<20} {v[:70]}")
        if not alert.ok:
            print(f"\nNOT PARSED: {alert.unparsed_reason}")
            print(
                "\nThe patterns in connectors/etrack_email.py were written "
                "from eTrack's documented notification content, not from a "
                "real sample. Adjust _FIELD_RE / _INDEX_RE to match what you "
                "see above, then re-run this."
            )
            return 1
        print("\nParsed OK.")
        return 0

    db = Database(cfg.db_path)
    try:
        if args.enroll:
            db.upsert_enrollment(
                index_number=args.enroll,
                caption=args.caption or "",
                reason="added by hand",
                score_hint=1.0,
            )
            db.mark_enrolled(args.enroll)
            print(f"Marked {args.enroll} as enrolled (self-reported).")
            print("It becomes 'confirmed' only when its first alert email "
                  "actually arrives — that is the only real proof.")
            return 0

        if args.ingest:
            try:
                stats = et.ingest(cfg, db, folder=args.folder, limit=args.limit)
            except et.EtrackDisabled as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(stats.to_markdown())
            return 0

        # Default: show the worklist and current enrollment state.
        gate_ok = True
        try:
            et.assert_enabled(cfg)
        except et.EtrackDisabled as exc:
            gate_ok = False
            gate_msg = str(exc).splitlines()[0]

        print(f"ingestion gate: {'OPEN' if gate_ok else 'CLOSED'}")
        if not gate_ok:
            print(f"  {gate_msg}")
        print()

        rows = db.enrollments()
        if rows:
            print(f"{'index':<16}{'status':<12}{'alerts':>7}  caption")
            print("-" * 80)
            for r in rows:
                print(f"{r['index_number']:<16}{r['status']:<12}"
                      f"{r['alert_count']:>7}  {(r['caption'] or '')[:44]}")
            print()

        entries = et.build_worklist(db, limit=args.limit)
        if not entries:
            print("No NY state matters in the corpus to suggest enrolling.")
            print("This is expected while coverage is federal-only — NY "
                  "Commercial Division cases cannot be discovered by "
                  "scraping, which is the whole reason eTrack exists.")
            return 0

        added = et.record_candidates(db, entries)
        print(f"Enrollment worklist ({len(entries)} suggested, "
              f"{added} newly recorded).")
        print("Enrollment is MANUAL: one UCS web form per case.\n")
        for e in entries:
            print(f"  [{e.score_hint:.3f}] {e.index_number or '(no index)':<16}"
                  f"{e.caption[:52]}")
            print(f"            {e.reason}")
        return 0
    finally:
        db.close()


def cmd_claims(args: argparse.Namespace) -> int:
    """The chapter 11 census and the claims-agent routing table."""
    from .connectors.claims.routing import (
        DEB_ASSIGNMENTS_REFUSED, load_routing_table,
    )

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        if args.table:
            table = load_routing_table()
            print(f"{'vendor':<16}{'ToS policy':<20}{'case index'}")
            print("-" * 78)
            for a in table.agents.values():
                policy = a.tos_source_id or "(none registered — fail-closed)"
                print(f"{a.id:<16}{policy:<20}{a.case_index or '—'}")
                if a.note:
                    print(f"                  note: {a.note}")
            print(f"\n{len(table.agents)} vendors, "
                  f"{sum(len(a.aliases) for a in table.agents.values())} aliases.")
            print("\nNothing here authorizes crawling a vendor site. Stage 2 "
                  "is Phase 7 and is gated per vendor on its own ToS review.")
            return 0

        rows = db.claims_assignments(limit=args.limit)
        if not rows:
            print("No claims-agent assignments collected yet.")
            print("Run: litfin run --weekly --source claims_routing")
            return 0

        counts = db.claims_vendor_counts()
        print(f"Chapter 11 claims-agent census — {len(rows)} cases\n")
        print(f"{'vendor':<16}{'cases':>6}")
        print("-" * 24)
        for c in counts:
            print(f"{c['vendor_id'] or '?':<16}{c['n']:>6}")

        unmapped = db.claims_unmapped()
        if unmapped:
            # Loud on purpose: an unrecognized agent is a new entrant or a
            # rename. The rows are kept either way -- this is an alert, not a
            # filter.
            print(f"\n!! {len(unmapped)} UNMAPPED agent name(s) — rows kept, "
                  f"routing unknown:")
            for u in unmapped:
                print(f"   {u['n']:>3}x  [{u['court']}] {u['agent_raw']}")
            print("   Add an alias to src/litfin/connectors/claims/agents.toml")

        print(f"\n{'case':<14}{'agent':<12}{'filed':<12}{'court':<30}debtor")
        print("-" * 108)
        for r in rows[:args.limit]:
            print(f"{r['case_number']:<14}{(r['vendor_id'] or '?'):<12}"
                  f"{(r['date_filed'] or '—'):<12}{(r['court'] or '?')[:28]:<30}"
                  f"{(r['debtor'] or '')[:36]}")

        print(f"\nD. Del. assignment list is NOT collected: "
              f"{DEB_ASSIGNMENTS_REFUSED} is robots-disallowed. Delaware "
              f"contributes its approved-vendor directory only.")
        return 0
    finally:
        db.close()


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .deliver import dashboard

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        path = dashboard.write(
            db, cfg,
            out_path=Path(args.out) if args.out else None,
            limit=args.limit,
        )
        print(f"Dashboard written to: {path}")
        print(f"Archived copy in:     {cfg.runs_dir}")
        if args.open:
            import webbrowser
            webbrowser.open(path.as_uri())
        return 0
    finally:
        db.close()


def cmd_digest(args: argparse.Namespace) -> int:
    """Render the digest. Sends nothing unless --send is passed AND the gate
    in litfin.toml is open."""
    from .deliver import dataset, digest, mailer

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        data = dataset.load(db, cfg)
        d = digest.render(
            data,
            top_n=args.top or cfg.top_n_email,
            dashboard_url=(cfg.data_root / "dashboard.html").as_uri(),
        )
        try:
            result = mailer.send(
                d, cfg,
                dry_run=not args.send,
                recipients=args.to or None,
            )
        except mailer.SendRefused as exc:
            print(f"{exc}", file=sys.stderr)
            return 2
        print(result.to_markdown())
        return 0
    finally:
        db.close()


def cmd_preflight(args: argparse.Namespace) -> int:
    """Is it safe and lawful to host this? Exits non-zero when not."""
    from .deploy import preflight

    cfg = load_config(Path(args.config) if args.config else None)
    report = preflight.run(cfg, hosted=not args.local, host=args.host)
    print(report.to_text())
    return 1 if report.failures else 0


def cmd_export(args: argparse.Namespace) -> int:
    """Export the ranked table to a formatted .xlsx."""
    from .deliver import dataset, excel

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        data = dataset.load(db, cfg, limit=args.limit)
        out = Path(args.out) if args.out else excel.default_path(cfg)
        path = excel.build(data, out, limit=args.limit)
        print(f"Exported {len(data.prospects)} matters to: {path}")
        print("Sheets: Prospects, Venue coverage, Sources")
        print()
        print(
            "Note: the Damages column holds STATED figures only. Rows with "
            "no figure are blank there and marked 'Not stated' in the claim "
            "size band -- an imputed value is deliberately NOT written into a "
            "numeric column, because it would be summed and charted as real."
        )
        if args.open:
            import os
            os.startfile(path)  # noqa: S606  -- local, user-initiated
        return 0
    finally:
        db.close()


def cmd_serve(args: argparse.Namespace) -> int:
    from .deliver import server

    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        try:
            server.serve(
                cfg, db, port=args.port, open_browser=not args.no_browser,
                host=args.host, read_only=args.read_only,
            )
        except Exception as exc:
            from .deliver.auth import AuthMisconfigured
            if isinstance(exc, AuthMisconfigured):
                print(str(exc), file=sys.stderr)
                return 2
            raise
        return 0
    finally:
        db.close()


def cmd_items(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        rows = db.recent_items(limit=args.limit)
        if not rows:
            print("No items yet. Run `litfin run` first.")
            return 0
        for r in rows:
            print(f"[{r['source_id']}] {r['published_at'] or '(no date)'}")
            print(f"  {r['title']}")
            print(f"  {r['source_url']}")
            print()
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="litfin",
        description="Litigation-finance de-risked case sourcing pipeline (research).",
    )
    parser.add_argument("-c", "--config", help="path to litfin.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="fetch, parse, and store")
    p_run.add_argument("--source", action="append", help="limit to source_id(s)")
    p_run.add_argument(
        "--lookback", type=int, default=2,
        help="days of EDGAR full-text search to sweep (default 2; EDGAR "
             "back-fills late filings, so 1 would miss them)",
    )
    p_run.add_argument(
        "--cl-lookback", type=int, default=3,
        help="days of CourtListener docket-entry search (default 3; RECAP "
             "ingests late, so a 1-day window misses a lot)",
    )
    p_run.add_argument(
        "--weekly", action="store_true",
        help="also run weekly-cadence sources (JPML)",
    )
    p_run.set_defaults(func=cmd_run)

    p_cov = sub.add_parser(
        "coverage",
        help="venue coverage map -- which courts publish a PACER RSS feed",
    )
    p_cov.add_argument(
        "--refresh", action="store_true",
        help="re-walk the CourtListener courts endpoint (weekly is plenty)",
    )
    p_cov.set_defaults(func=cmd_coverage)

    p_comp = sub.add_parser("compliance", help="inspect the compliance gate")
    comp_sub = p_comp.add_subparsers(dest="compliance_command")
    p_comp.add_argument("source_id", nargs="?", help="show one source in detail")
    p_comp.set_defaults(func=cmd_compliance, review_source=None)

    p_rev = comp_sub.add_parser(
        "review",
        help="read a source's terms and emit the registry change to commit",
    )
    p_rev.add_argument("review_source", metavar="source_id")
    p_rev.add_argument(
        "--fetch", action="store_true",
        help="fetch and print the terms pages. Restricted to the source's own "
             "declared tos_urls, still honors robots, and PROHIBITED sources "
             "still refuse.",
    )
    p_rev.add_argument("--chars", type=int, default=6000,
                       help="how much of each terms page to print")
    p_rev.add_argument("--record", action="store_true",
                       help="emit the registry diff for a determination")
    p_rev.add_argument("--verdict", help="new ToSStatus value")
    p_rev.add_argument("--by", help="reviewer name — goes in git")
    p_rev.add_argument("--quote", help="VERBATIM text of the operative clause")
    p_rev.set_defaults(func=cmd_compliance, source_id=None)

    p_status = sub.add_parser("status", help="show source health")
    p_status.set_defaults(func=cmd_status)

    p_items = sub.add_parser("items", help="show recently collected items")
    p_items.add_argument("--limit", type=int, default=20)
    p_items.set_defaults(func=cmd_items)

    p_screen = sub.add_parser(
        "screen", help="dry-run the pre-LLM screens (no API calls, no cost)"
    )
    p_screen.add_argument("--limit", type=int, default=None)
    p_screen.set_defaults(func=cmd_screen)

    p_extract = sub.add_parser("extract", help="run Opus extraction over candidates")
    p_extract.add_argument(
        "--sync", action="store_true",
        help="extract a few items synchronously (smoke test; full price)",
    )
    p_extract.add_argument(
        "--collect", action="store_true", help="collect results of open batches",
    )
    p_extract.add_argument(
        "--refresh", action="store_true",
        help="ALSO re-extract rows stored under an older schema version. "
             "COSTS MONEY. Use after adding a field to the extraction schema, "
             "so the new column is populated for the existing corpus instead "
             "of being blank for everything collected before today.",
    )
    p_extract.add_argument(
        "--wait", action="store_true", help="with --collect, poll until ready",
    )
    p_extract.add_argument("--limit", type=int, default=None)
    p_extract.set_defaults(func=cmd_extract)

    p_alerts = sub.add_parser(
        "alerts", help="CourtListener docket alerts (the monitoring half)"
    )
    p_alerts.add_argument(
        "--subscribe", action="store_true",
        help="subscribe to discovered dockets not yet alerted on",
    )
    p_alerts.add_argument("--limit", type=int, default=25)
    p_alerts.set_defaults(func=cmd_alerts)

    p_wh = sub.add_parser(
        "webhook", help="receive CourtListener webhook deliveries"
    )
    p_wh.add_argument("--host", default="0.0.0.0")
    p_wh.add_argument("--port", type=int, default=8787)
    p_wh.add_argument(
        "--drain", action="store_true",
        help="process stored deliveries (runs outside the request path)",
    )
    p_wh.add_argument("--status", action="store_true", help="show queue depth")
    p_wh.add_argument(
        "--allow-any-ip", action="store_true",
        help="DISABLE the source-IP allowlist. Local testing only.",
    )
    p_wh.add_argument("--limit", type=int, default=200)
    p_wh.set_defaults(func=cmd_webhook)

    p_rank = sub.add_parser("rank", help="score extractions and print the top list")
    p_rank.add_argument("--limit", type=int, default=25)
    p_rank.set_defaults(func=cmd_rank)

    p_et = sub.add_parser(
        "etrack",
        help="NY eTrack: enrollment worklist, parser check, gated ingestion",
    )
    p_et.add_argument(
        "--check", metavar="FILE.eml",
        help="parse one saved alert email and print what came out. Do this "
             "BEFORE enabling ingestion — the patterns have not been "
             "calibrated against a real alert.",
    )
    p_et.add_argument(
        "--ingest", action="store_true",
        help="fetch unseen alerts over IMAP. Requires [etrack].enabled AND "
             "[etrack].decision_recorded in litfin.toml.",
    )
    p_et.add_argument("--enroll", metavar="INDEX",
                      help="record that you submitted the UCS form for an index number")
    p_et.add_argument("--caption", help="caption to store with --enroll")
    p_et.add_argument("--folder", default="INBOX")
    p_et.add_argument("--limit", type=int, default=25)
    p_et.set_defaults(func=cmd_etrack)

    p_claims = sub.add_parser(
        "claims", help="chapter 11 claims-agent census and routing table"
    )
    p_claims.add_argument(
        "--table", action="store_true",
        help="show the vendor routing table instead of the census",
    )
    p_claims.add_argument("--limit", type=int, default=40)
    p_claims.set_defaults(func=cmd_claims)

    p_dash = sub.add_parser(
        "dashboard", help="write the self-contained HTML dashboard"
    )
    p_dash.add_argument("--out", help="output path (default <data_root>/dashboard.html)")
    p_dash.add_argument("--limit", type=int, default=None, help="rows to include")
    p_dash.add_argument("--open", action="store_true", help="open it afterwards")
    p_dash.set_defaults(func=cmd_dashboard)

    p_dig = sub.add_parser(
        "digest", help="render the top-N email digest (dry run by default)"
    )
    p_dig.add_argument("--top", type=int, default=None)
    p_dig.add_argument(
        "--to", action="append",
        help="recipient; must be in deliver.recipient_allowlist to send",
    )
    p_dig.add_argument(
        "--send", action="store_true",
        help="ACTUALLY SEND. Requires deliver.send_enabled = true AND every "
             "recipient in deliver.recipient_allowlist AND SMTP settings in "
             "the environment. Refuses loudly otherwise.",
    )
    p_dig.set_defaults(func=cmd_digest)

    p_pre = sub.add_parser(
        "preflight",
        help="check whether this is safe and lawful to host; non-zero if not",
    )
    p_pre.add_argument(
        "--local", action="store_true",
        help="relax the network checks (still runs every compliance check)",
    )
    p_pre.add_argument("--host", default="0.0.0.0",
                       help="bind address the deployment will use")
    p_pre.set_defaults(func=cmd_preflight)

    p_export = sub.add_parser(
        "export", help="export the ranked table to a formatted .xlsx"
    )
    p_export.add_argument("--out", help="output path (default <data_root>/litfin-prospects-<date>.xlsx)")
    p_export.add_argument("--limit", type=int, default=None)
    p_export.add_argument("--open", action="store_true", help="open it afterwards")
    p_export.set_defaults(func=cmd_export)

    p_serve = sub.add_parser(
        "serve", help="local control panel (loopback only)"
    )
    p_serve.add_argument("--port", type=int, default=8788)
    p_serve.add_argument("--no-browser", action="store_true")
    p_serve.add_argument(
        "--host", default="127.0.0.1",
        help="bind address. Anything other than loopback REFUSES to start "
             "without LITFIN_WEB_USER, LITFIN_WEB_PASSWORD and "
             "LITFIN_SESSION_SECRET -- this panel can spend money.",
    )
    p_serve.add_argument(
        "--read-only", action="store_true",
        help="serve the dashboard and export only. run/extract/collect are "
             "refused by the SERVER, not merely hidden from the page.",
    )
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
