"""Phase 5: the claims-agent routing table and chapter 11 census.

Fixtures are the four courts' real pages, captured 2026-08-15. Two of these
tests pin bugs found by running against them, and two pin the design decisions
that look like over-engineering until you see the four different layouts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from litfin.canary.framework import CanaryFailure
from litfin.connectors.base import FetchTask
from litfin.connectors.claims import routing

FIX = Path(__file__).parent / "fixtures" / "claims"

URLS = {
    "ohsb": "https://www.ohsb.uscourts.gov/claims-agents",
    "nysb_assign": "https://www.nysb.uscourts.gov/megaCases",
    "nysb": "https://www.nysb.uscourts.gov/claims-agents",
    "njb_assign": (
        "https://www.njb.uscourts.gov/content/"
        "claims-agent-case-assignments-district-new-jersey"
    ),
    "deb_vendors": "https://www.deb.uscourts.gov/claims-agency-list",
    "deb": "https://www.deb.uscourts.gov/claims-agents-and-assignments",
}


def load(name: str) -> bytes:
    return (FIX / f"{name}.html").read_bytes()


def parse(name: str):
    return routing.build().parse(load(name), URLS[name])


# ---------------------------------------------------------------------------
# The routing table
# ---------------------------------------------------------------------------

class TestRoutingTable:
    def test_resolves_the_same_vendor_across_four_spellings(self):
        """Each court prints the same vendor differently. Resolution is the
        entire point of the table."""
        t = routing.load_routing_table()
        assert t.resolve("Epiq") == "epiq"
        assert t.resolve("Epiq Corporate Restructuring, LLC") == "epiq"
        assert t.resolve("EPIQ CORPORATE RESTRUCTURING, LLC") == "epiq"
        assert t.resolve("Donlin, Recano & Co.,LLC") == "angeion"
        assert t.resolve("Donlin Recano & Company, LLC") == "angeion"

    def test_prime_clerk_still_resolves_to_kroll(self):
        """Prime Clerk was RENAMED, not retired. njb still prints
        'Prime Clerk (now Kroll Restructuring)' on cases retained under the
        old name; dropping the alias would orphan every pre-2022 assignment."""
        t = routing.load_routing_table()
        assert t.resolve("Prime Clerk (now Kroll Restructuring)") == "kroll"
        assert t.resolve("Kroll Restructuring Administration") == "kroll"

    def test_longest_alias_wins(self):
        """'kcc' is a substring of nothing useful, but 'kurtzman carson
        consultants' must not lose to a shorter accidental match."""
        t = routing.load_routing_table()
        assert t.resolve("Kurtzman Carson Consultants LLC dba Verita Global") == "verita"

    def test_unknown_vendor_is_unmapped_not_guessed(self):
        t = routing.load_routing_table()
        assert t.resolve("Some New Claims Co LLC") == routing.UNMAPPED

    def test_gcg_is_not_folded_into_epiq(self):
        """GCG was acquired by Epiq in 2018, but the census records who the
        court actually retained, and a pre-2018 GCG case is not reachable from
        Epiq's current case index. Mapping it to epiq would produce a routing
        entry that looks usable and is not."""
        t = routing.load_routing_table()
        assert t.resolve("GCG, Inc.") == "gcg"
        assert t.get("gcg").case_index == ""

    def test_unregistered_vendors_have_no_tos_policy(self):
        """Fail-closed: a vendor with no registry entry must not carry one."""
        t = routing.load_routing_table()
        for vid in ("logan", "reliable", "american_legal", "cpt", "gcg"):
            assert t.get(vid).tos_source_id == ""


# ---------------------------------------------------------------------------
# Column resolution -- the trap
# ---------------------------------------------------------------------------

class TestColumnResolution:
    def test_columns_resolve_by_header_not_position(self):
        """The three courts do NOT agree on column order or meaning:

            ohsb  case | debtor    | agent | date
            nysb  case | title     | agent | date
            njb   case | vicinage  | title | agent      <- no date at all

        A positional parser writes 'Newark' into the debtor field and looks
        like it worked.
        """
        njb = parse("njb_assign")
        assert njb.rows_parsed > 30
        row = njb.items[0].payload
        assert row["debtor"] == "Hudson Healthcare, Inc."
        assert row["vicinage"] == "Newark"
        # njb has no date column; the parser must leave it empty rather than
        # borrowing a neighbouring cell.
        assert row["date_filed"] == ""

    def test_case_title_maps_to_debtor(self):
        """BUG PINNED: the role loop used to `break` when a needle matched a
        role that was already taken. njb's header is 'Case Title' -- the
        'case' needle matched first, found case_number already assigned, and
        gave up. Every njb row then carried an empty debtor while the parse
        reported complete success."""
        roles = routing._roles_for(
            ["Case Number and Judge Initials", "Vicinage", "Case Title",
             "Link to Claims Agent Website"]
        )
        assert roles == {0: "case_number", 1: "vicinage", 2: "debtor", 3: "agent"}

    def test_ohsb_and_nysb_headers_also_resolve(self):
        assert routing._roles_for(
            ["Case No.", "Debtor", "Claims Agent", "Date Filed"]
        ) == {0: "case_number", 1: "debtor", 2: "agent", 3: "date_filed"}
        assert routing._roles_for(
            ["Case Number", "Title", "Claims Agent", "Date Filed"]
        ) == {0: "case_number", 1: "debtor", 2: "agent", 3: "date_filed"}


# ---------------------------------------------------------------------------
# Parsing the live shapes
# ---------------------------------------------------------------------------

class TestAssignmentParsing:
    def test_ohsb_inline_table(self):
        r = parse("ohsb")
        assert r.rows_parsed == 5
        first = r.items[0].payload
        assert first["case_number"] == "26-11937"
        assert first["vendor_id"] == "stretto"
        assert first["date_filed"] == "2026-07-22"
        assert first["debtor"].startswith("Magellan Aerospace")

    def test_nysb_mega_cases(self):
        r = parse("nysb_assign")
        assert r.rows_parsed > 50
        vendors = {i.payload["vendor_id"] for i in r.items}
        assert {"epiq", "stretto", "kroll"} <= vendors

    def test_a_mega_case_with_no_agent_is_not_unmapped(self):
        """nysb lists mega cases where no claims agent was retained. That is a
        real fact about a large chapter 11, not a vendor the table failed to
        recognize, and conflating them would fire the alarm every run."""
        r = parse("nysb_assign")
        none_retained = [
            i for i in r.items if i.payload["vendor_id"] == "none_retained"
        ]
        assert none_retained
        assert all(not i.payload["agent_raw"] for i in none_retained)
        assert "UNMAPPED" not in r.note or "none_retained" not in r.note

    def test_jointly_administered_cases_keep_the_raw_string(self):
        """njb prints '19-12809-JKS and 19-12812-JKS' in one cell. The lead
        case keys the row; the companion must not be silently lost."""
        r = parse("njb_assign")
        joint = [
            i for i in r.items
            if " and " in (i.payload.get("case_number_raw") or "")
        ]
        assert joint, "expected at least one jointly-administered row"
        p = joint[0].payload
        assert p["case_number"] in p["case_number_raw"]
        assert p["case_number_raw"] != p["case_number"]

    def test_dates_parse_from_both_court_formats(self):
        assert routing._parse_date("July 22, 2026") == "2026-07-22"
        assert routing._parse_date("06/15/2026") == "2026-06-15"
        assert routing._parse_date("") == ""
        assert routing._parse_date("sometime last spring") == ""


class TestVendorDirectory:
    def test_nysb_directory_parses_without_being_mistaken_for_assignments(self):
        r = parse("nysb")
        kinds = {i.payload["record_kind"] for i in r.items}
        assert kinds == {"claims_agent_directory"}
        assert r.rows_parsed >= 9

    def test_deb_directory_parses(self):
        r = parse("deb_vendors")
        assert r.rows_parsed >= 8
        vendors = {i.payload["vendor_id"] for i in r.items}
        assert {"epiq", "stretto", "kroll", "bmc"} <= vendors

    def test_directory_carries_the_tos_source_id_for_stage_two(self):
        r = parse("deb_vendors")
        epiq = next(i for i in r.items if i.payload["vendor_id"] == "epiq")
        assert epiq.payload["vendor_tos_source_id"] == "claims_epiq"


class TestLandingPage:
    def test_deb_landing_reports_empty_by_design(self):
        """A pointer page yields no rows on purpose. Saying so affirmatively
        is what stops the canary calling it BROKEN."""
        r = parse("deb")
        assert r.rows_parsed == 0
        assert r.server_reported_empty is True


# ---------------------------------------------------------------------------
# The unmapped alarm
# ---------------------------------------------------------------------------

class TestUnmappedAlarm:
    def test_unknown_agent_is_kept_and_reported_not_dropped(self):
        """A vendor missing from the table is either a new entrant or a
        rename. Dropping the row makes the census quietly wrong in exactly the
        direction that is hardest to notice."""
        empty = routing.RoutingTable()          # nothing resolves
        c = routing.build(empty)
        r = c.parse(load("ohsb"), URLS["ohsb"])

        assert r.rows_parsed == 5, "rows must be KEPT, not dropped"
        assert all(i.payload["vendor_id"] == routing.UNMAPPED for i in r.items)
        assert "UNMAPPED AGENT(S)" in r.note
        assert "Stretto" in r.note
        assert c.unmapped_seen


# ---------------------------------------------------------------------------
# Census records must not look like deal signals
# ---------------------------------------------------------------------------

class TestNoSyntheticEventLanguage:
    def test_bodies_carry_no_outcome_language(self):
        """Same discipline as sec_daily_index and govinfo. These rows say who
        was retained in which case; writing 'settlement' or 'judgment' into
        them would manufacture a signal the source does not have and burn
        extraction budget on it."""
        banned = (
            "settlement", "judgment", "verdict", "damages", "awarded",
            "consent decree", "plan confirmation",
        )
        for name in ("ohsb", "nysb_assign", "njb_assign"):
            for item in parse(name).items:
                low = item.body.lower()
                for word in banned:
                    assert word not in low, f"{name}: '{word}' in {item.body!r}"

    def test_screen_drops_every_claims_item(self):
        """The real check: none of these should ever reach the LLM."""
        from litfin.score.taxonomy import classify_text

        for name in ("ohsb", "nysb_assign", "njb_assign", "nysb", "deb_vendors"):
            for item in parse(name).items:
                hit = classify_text(f"{item.title}\n{item.body}")
                assert not hit.is_signal, (
                    f"{name}: {item.title!r} would cost extraction budget"
                )


# ---------------------------------------------------------------------------
# Canaries
# ---------------------------------------------------------------------------

class TestCanary:
    def test_assignment_canary_passes_on_real_pages(self):
        c = routing.build()
        for name in ("ohsb", "nysb_assign", "njb_assign"):
            c.canary(load(name), FetchTask(task_key="t", url=URLS[name]))

    def test_assignment_canary_fails_when_headers_change(self):
        c = routing.build()
        html = (
            b"<html><body><table><tr><th>Something</th><th>Else</th></tr>"
            b"<tr><td>26-11937</td><td>x</td></tr></table></body></html>"
        )
        with pytest.raises(CanaryFailure):
            c.canary(html, FetchTask(task_key="t", url=URLS["ohsb"]))

    def test_deb_landing_canary_watches_for_the_list_moving(self):
        """Delaware's assignment list is robots-disallowed today. The landing
        page earns its request by telling us if the court ever moves it."""
        c = routing.build()
        c.canary(load("deb"), FetchTask(task_key="t", url=URLS["deb"]))

        with pytest.raises(CanaryFailure, match="ClaimsAgentCases"):
            c.canary(
                b"<html><body>nothing here</body></html>",
                FetchTask(task_key="t", url=URLS["deb"]),
            )

    def test_vendor_canary_fails_on_an_empty_layout(self):
        c = routing.build()
        with pytest.raises(CanaryFailure):
            c.canary(
                b"<html><body><p>no tables</p></body></html>",
                FetchTask(task_key="t", url=URLS["deb_vendors"]),
            )


class TestCompliance:
    def test_delaware_assignment_host_is_scoped_but_the_list_is_refused(self):
        """The registry permits the directory; robots refuses the list. Both
        facts have to survive, and the refusal must be visible rather than
        looking like an oversight."""
        from litfin.compliance.registry import get_policy

        policy = get_policy("claims_routing")
        policy.assert_url_in_scope(routing.DEB_ASSIGNMENTS_REFUSED)
        assert "media.deb.uscourts.gov" in routing.DEB_ASSIGNMENTS_REFUSED
        assert "robots" in routing.build().coverage_note.lower()

    def test_plan_covers_every_court(self):
        tasks = routing.build().plan(None)
        courts = {t.task_key.split(":")[1] for t in tasks}
        assert courts == {"ohsb", "nysb", "njb", "deb"}

    def test_no_vendor_site_is_ever_planned(self):
        """Stage 2 is Phase 7 and is gated per vendor. Nothing in Phase 5
        fetches a claims agent."""
        for t in routing.build().plan(None):
            assert ".uscourts.gov" in t.url
