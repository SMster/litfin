"""The compliance registry: one SourcePolicy per source, under version control.

This file is the record of every legal determination made about every data
source in the pipeline. It lives in git, not in a database, so that changing a
source's status is a reviewable commit rather than a silent runtime mutation.

`review_note` carries VERBATIM quotes where a determination rests on specific
terms language. Do not paraphrase these -- the quote is the evidence.

Statuses were set from live reconnaissance performed 2026-08-14. Where a
determination rests on observed HTTP behavior rather than on read terms, the
note says so explicitly and the status is marked provisional.
"""

from __future__ import annotations

from datetime import date

from .status import SourcePolicy, ToSStatus

# Reviews expire one year out. A read done today does not authorize a crawl
# in 2029.
_DEFAULT_EXPIRY = date(2027, 8, 14)


POLICIES: dict[str, SourcePolicy] = {}


def _register(p: SourcePolicy) -> SourcePolicy:
    POLICIES[p.source_id] = p
    return p


# ---------------------------------------------------------------------------
# Tier A -- US Government works. Not subject to copyright (17 U.S.C. s.105).
# robots.txt is still honored; Crawl-delay values observed live are encoded in
# net/ratelimit.py RATES.
# ---------------------------------------------------------------------------

_register(SourcePolicy(
    source_id="sec_litrel",
    display_name="SEC Litigation Releases (RSS)",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="sec.gov",
    base_confidence=0.95,
    allowed_url_patterns=(
        "https://www.sec.gov/enforcement-litigation/litigation-releases*",
        "https://www.sec.gov/litigation/litreleases*",
        "https://www.sec.gov/files/litigation/*",
    ),
    notes=(
        "Official RSS feed confirmed live. Two parsing gotchas found during "
        "reconnaissance: (1) guid isPermaLink='false' is an opaque UUID -- do "
        "NOT use it as a stable key; use dc:creator, which carries the "
        "monotonic LR-##### release number. (2) <link> values carry a "
        "trailing newline that must be stripped or every URL 404s."
    ),
))

_register(SourcePolicy(
    source_id="doj_atr",
    display_name="DOJ Antitrust Division press releases (RSS)",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="justice.gov",
    base_confidence=0.95,
    allowed_url_patterns=(
        "https://www.justice.gov/news/rss*",
        "https://www.justice.gov/atr/*",
        "https://www.justice.gov/opa/pr/*",
    ),
    notes=(
        "The 'three separate feeds' premise was wrong. DOJ runs ONE "
        "parameterized Drupal view; field_component=376 selects the Antitrust "
        "Division. robots.txt declares Crawl-delay: 10, so 0.1 req/s ceiling. "
        "Feed window is a fixed 25 items -- during a merger wave DOJ can "
        "exceed that between polls, so poll 4x/day and reconcile daily "
        "against the HTML listing."
    ),
))

_register(SourcePolicy(
    source_id="ftc",
    display_name="FTC press releases (RSS)",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="ftc.gov",
    base_confidence=0.95,
    allowed_url_patterns=(
        "https://www.ftc.gov/feeds/*",
        "https://www.ftc.gov/news-events/*",
        "https://www.ftc.gov/legal-library/*",
    ),
    notes=(
        "robots.txt declares Crawl-delay: 5 -> 0.2 req/s ceiling. FTC 403s "
        "undeclared user agents; a descriptive UA gets 200. Whether Cases & "
        "Proceedings has its own dedicated feed is UNCONFIRMED -- the "
        "press-release feed is confirmed."
    ),
))

_register(SourcePolicy(
    source_id="doj_atr_case_filings",
    display_name="DOJ Antitrust case filings index (HTML)",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="justice.gov",
    base_confidence=0.95,
    allowed_url_patterns=(
        "https://www.justice.gov/atr/antitrust-case-filings*",
        "https://www.justice.gov/atr/case-document/*",
        "https://www.justice.gov/d9/*",
    ),
    notes=(
        "The Tunney Act goldmine: proposed final judgments, competitive "
        "impact statements, and entered final judgments. Because the Tunney "
        "Act requires civil antitrust settlements be publicly filed as "
        "proposed final judgments with a comment period, this is the earliest "
        "free settlement signal available anywhere. ~327 KB HTML table, no "
        "feed. Phase 2."
    ),
))

_register(SourcePolicy(
    source_id="sec_fts",
    display_name="SEC EDGAR full-text search",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="sec.gov",
    base_confidence=0.9,
    allowed_url_patterns=(
        "https://efts.sec.gov/LATEST/search-index*",
        "https://www.sec.gov/Archives/edgar/data/*",
    ),
    robots_unavailable="allow",
    reviewed_at=date(2026, 8, 15),
    review_note=(
        "efts.sec.gov returns 403 on /robots.txt (23-byte body) while serving "
        "its API normally to an identified client. Determination: this is "
        "'no robots file published on an API subdomain', NOT a refusal of "
        "consent. Three things distinguish it from the Kroll pattern, where a "
        "robots 403 IS treated as refusal: (1) the content endpoint returns "
        "200 to the same identified client, whereas Kroll 403s both; (2) SEC "
        "publishes an explicit written fair-access policy governing automated "
        "EDGAR access -- declare a UA with contact info, stay under 10 req/s "
        "-- which is stronger evidence of consent than the absence of a "
        "robots file on one subdomain; (3) www.sec.gov's robots.txt "
        "affirmatively carries 'Allow: /Archives/edgar/data'. We run at 6 "
        "req/s against the shared sec.gov bucket, 40% under the published cap."
    ),
    notes=(
        "UNDOCUMENTED but public endpoint; SEC reserves the right to change "
        "it without notice, so this connector carries the strictest canary in "
        "the system (a canned query over a fixed date window must return a "
        "known accession -- that validates query SEMANTICS, not just "
        "reachability). hits.total.value saturates at 10000 with "
        "relation='gte' and deep pagination is capped, so ALWAYS slice "
        "queries to a single day, never a range. Phase 2."
    ),
))

_register(SourcePolicy(
    source_id="govinfo",
    display_name="govinfo.gov USCOURTS collection",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="api.govinfo.gov",
    base_confidence=0.95,
    allowed_url_patterns=("https://api.govinfo.gov/*",),
    notes=(
        "Natively incremental: lastModified is the watermark, no hashing "
        "needed. Measured ~1,600 packages/day, so filter hard by packageId "
        "court prefix before fetching granules. DEMO_KEY works for dev but is "
        "globally throttled; register a free api.data.gov key. Phase 4."
    ),
))

_register(SourcePolicy(
    source_id="jpml",
    display_name="Judicial Panel on Multidistrict Litigation",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="uscourts.gov",
    base_confidence=0.9,
    allowed_url_patterns=("https://www.jpml.uscourts.gov/*",),
    notes=(
        "Crawl-delay: 10. Weekly, not daily -- MDL dockets move slowly. "
        "/pending-mdls is a landing page; the list is at /pending-mdls-0. "
        "The host closed the connection during repeated probing, so the "
        "parser handles both table and flat-text layouts and characterises "
        "itself on the first rate-limited run."
    ),
))

_register(SourcePolicy(
    source_id="sec_daily_index",
    display_name="SEC EDGAR daily form index",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="sec.gov",
    base_confidence=0.95,
    allowed_url_patterns=("https://www.sec.gov/Archives/edgar/daily-index/*",),
    notes=(
        "The complete-coverage counterpart to sec_fts. FTS can only find "
        "filings whose text matches a phrase we thought to search for; this "
        "is the authoritative list of EVERY filing disseminated that day. "
        "Measured: form.20260814.idx is 2.1 MB / 11,151 rows, 283 of them "
        "8-K. Fixed-width columns; offsets are derived from the header row "
        "rather than hardcoded because EDGAR has shifted them historically. "
        "Carries no document text, so its role is coverage accounting, not "
        "detection."
    ),
))

_register(SourcePolicy(
    source_id="state_ag",
    display_name="State Attorney General press releases",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="_default",
    base_confidence=0.75,
    allowed_url_patterns=(
        "https://oag.ca.gov/*", "https://coag.gov/*", "https://ago.mo.gov/*",
        "https://ncdoj.gov/*", "https://www.doj.state.or.us/*",
        "https://www.attorneygeneral.gov/*",
        "https://attorneygeneral.utah.gov/*",
    ),
    notes=(
        "COVERAGE IS 7 OF 50 STATES (measured 2026-08-15 across 40 candidate "
        "URLs). The prior estimate of 30-40 working feeds was optimistic by a "
        "wide margin. NY 403s an identified client on every path tried -- "
        "treated as refusal of consent, not a puzzle -- and TX 404s "
        "everywhere, so the two highest-value states are dark. Feed list and "
        "the full probe record live in state_ag_feeds.toml. Each feed is its "
        "own task so one dead feed degrades one task, never the run."
    ),
))

_register(SourcePolicy(
    source_id="govinfo",
    display_name="govinfo USCOURTS opinion index",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="api.govinfo.gov",
    base_confidence=0.9,
    allowed_url_patterns=("https://api.govinfo.gov/*",),
    notes=(
        "An INDEX, not an event source, and the connector is built that way "
        "deliberately. The collection endpoint gives packageId, case name, "
        "and dates -- no text describing what happened. The rich metadata "
        "(caseType, parties, documentType) is one request PER PACKAGE, and at "
        "~2,000 packages/day against api.data.gov's 1,000/hour that is not "
        "affordable. So items are stored with record_kind='opinion_index' and "
        "no synthetic event language: the screen correctly drops them from "
        "extraction while the dashboard can still join on them. Free win: the "
        "case number embeds '-cv-' vs '-cr-', so civil-only filtering costs "
        "nothing and removes about half the volume."
    ),
))

_register(SourcePolicy(
    source_id="claims_routing",
    display_name="Bankruptcy court claims-agent assignment lists",
    tier="A",
    status=ToSStatus.PUBLIC_DOMAIN_GOV,
    rate_key="uscourts.gov",
    base_confidence=0.9,
    allowed_url_patterns=(
        "https://www.deb.uscourts.gov/*",
        # MEASURED 2026-08-15: Delaware does not host its assignment list on
        # the main site. /claims-agents-and-assignments links out to
        # media.deb.uscourts.gov, which is the same court's own file host.
        # Scoped to the one directory rather than the whole host.
        "https://media.deb.uscourts.gov/moveit/*",
        "https://www.nysb.uscourts.gov/*",
        "https://www.njb.uscourts.gov/*",
        "https://www.ohsb.uscourts.gov/*",
    ),
    notes=(
        "Stage 1 of the claims-agent crawl. These are government sites, so "
        "the routing table needs NO ToS review and can ship before any "
        "claims-agent vendor review clears. Yields a standalone chapter 11 "
        "census even with zero docket crawling. Phase 5.\n\n"
        "MEASURED 2026-08-15 -- the four courts publish in four shapes, and "
        "only one of them is the obvious one:\n"
        "  ohsb  /claims-agents  -> assignment TABLE inline\n"
        "  nysb  /claims-agents  -> vendor DIRECTORY inline; assignments "
        "live behind /megaCases\n"
        "  deb   /claims-agents-and-assignments -> links only; assignments at "
        "media.deb.uscourts.gov/moveit/ClaimsAgentCases.html, vendor list at "
        "/claims-agency-list\n"
        "  njb   /claim-agent-cases-and-protocols -> links only; assignments "
        "at /content/claims-agent-case-assignments-district-new-jersey"
    ),
))


# ---------------------------------------------------------------------------
# CourtListener / RECAP -- the single highest-value source, unlocked by the
# research purpose declaration.
# ---------------------------------------------------------------------------

_register(SourcePolicy(
    source_id="courtlistener",
    display_name="CourtListener / RECAP (Free Law Project)",
    tier="A",
    status=ToSStatus.RESEARCH_ONLY,
    rate_key="courtlistener.com",
    base_confidence=0.85,
    tos_urls=(
        "https://www.courtlistener.com/terms/",
        "https://free.law/membership/allowed-api-usage/",
    ),
    review_note=(
        'Free Law Project permits API use for "personal, educational, '
        'research, journalistic, and exploratory use." A research project '
        "sits inside that. HOWEVER the same terms state the API \"may not be "
        'used to build tools for for-profit or non-profit organizations, even '
        'if those tools aren\'t sold." A personal research project clears '
        "this; the same code running as firm infrastructure does NOT. This is "
        "the open scope question flagged in the plan -- resolve it with FLP's "
        "partnerships team before relying on this source at volume."
        "\n\n"
        "ROBOTS DETERMINATION (2026-08-15): www.courtlistener.com/robots.txt "
        "returns 403 from CloudFront ('Request blocked'), while the API "
        "endpoints under /api/rest/v4/ return 200 to the SAME identified "
        "client. This is the efts.sec.gov pattern, not the Kroll pattern, and "
        "the case here is stronger than either: (1) the content endpoints "
        "serve us, whereas Kroll 403s both robots AND content; (2) Free Law "
        "Project PUBLISHES a documented public REST API with published "
        "per-tier rate limits, which is an affirmative invitation to call it "
        "programmatically rather than mere silence; (3) their terms of use "
        "govern API access directly and permit research use. The robots 403 "
        "reads as a CDN configuration artifact on a static file, not as a "
        "refusal of consent. We run at 0.06 req/s with hourly and daily caps "
        "held under the published anonymous ceiling."
    ),
    reviewed_at=date(2026, 8, 15),
    expires_at=_DEFAULT_EXPIRY,
    allowed_url_patterns=("https://www.courtlistener.com/api/rest/v4/*",),
    robots_unavailable="allow",
    notes=(
        "Gives full-text search over docket ENTRY text (type=rd, description: "
        "field) -- the thing PACER's own API cannot do. Take the $10/mo "
        "membership for unlimited docket alerts (free tier caps at 5). "
        "Default REST limits (5/min, 50/hr, 125/day as of May 2026) cannot "
        "sustain polling: use webhooks. Webhooks have NO HMAC signature -- "
        "mitigate with IP allowlist 34.210.230.218 / 54.189.59.91 and a long "
        "random secret URL. date_created is INGESTION time, not filing time "
        "(content can arrive weeks/months late) -- key events off "
        "entry_date_filed. nature_of_suit is dirty free text: use "
        "__startswith=410, never __exact. Phase 3."
    ),
))


# ---------------------------------------------------------------------------
# Tier B -- REVIEWED 2026-08-15. Phase 7.
#
# The plan expected these reviews to clear under a research purpose. SEVEN OF
# EIGHT DID NOT, and the reasons are recorded verbatim below rather than
# summarized. Two refuse an identified client outright, two prohibit automated
# access in terms, one publishes no terms at all, one is blocked on a
# click-through disclaimer, and one could not be read.
#
# `claims_stretto` is the single source that cleared.
#
# Reviews were performed by reading each site's own published terms via
# `litfin compliance review --fetch`. That command restricts itself to a
# source's declared tos_urls and still honors robots -- reading a terms page
# to decide whether you may use a site is a different act from harvesting it,
# but it is not a licence to fetch anything else.
# ---------------------------------------------------------------------------

_register(SourcePolicy(
    source_id="stanford_ssc",
    display_name="Stanford Securities Class Action Clearinghouse",
    tier="B",
    status=ToSStatus.PROHIBITED,
    rate_key="securities.stanford.edu",
    base_confidence=0.9,
    tos_urls=(
        "https://securities.stanford.edu/",
        "https://securities.stanford.edu/disclaimer.html",
    ),
    reviewed_by="machine-assisted read, 2026-08-15 -- confirm before relying",
    reviewed_at=date(2026, 8, 15),
    review_note=(
        "PROVISIONAL, on observed behavior rather than read terms, and the "
        "behavior is the same pattern as Kroll. MEASURED 2026-08-15: "
        "securities.stanford.edu returns HTTP 403 to an honest identified "
        "client on BOTH the homepage and the disclaimer page. The terms "
        "cannot be read because the host will not serve them to us. "
        "A 403 to an identified client is treated as refusal of consent -- "
        "and refusing to serve its own disclaimer is a refusal that cannot "
        "be argued around. Do NOT work around this with a headless browser "
        "or an unidentified UA. "
        "The plan named this the FIRST Tier B source to review and the "
        "easiest win, because its robots.txt is permissive. Robots said yes "
        "and the server said no; the server wins."
    ),
    notes=(
        "Stanford is a PRIVATE UNIVERSITY, not a government body, so this "
        "could never have been treated as public-domain material regardless. "
        "Carries settlement AMOUNTS, which is rare and valuable -- if you "
        "want this source, the route is an email to the Clearinghouse asking "
        "for permission or a data extract, not a workaround."
    ),
))

_register(SourcePolicy(
    source_id="de_courtconnect",
    display_name="Delaware CourtConnect (Superior Court / CCLD)",
    tier="B",
    status=ToSStatus.UNVERIFIED,
    rate_key="courts.delaware.gov",
    base_confidence=0.85,
    tos_urls=(
        "https://courtconnect.courts.delaware.gov/cc/cconnect/"
        "ck_public_qry_main.cp_main_disclaimer?search_option=docket",
        "https://courts.delaware.gov/",
    ),
    reviewed_by="machine-assisted read, 2026-08-15 -- INCONCLUSIVE",
    reviewed_at=date(2026, 8, 15),
    review_note=(
        "REVIEW CANNOT BE COMPLETED BY READING. Stays UNVERIFIED and "
        "disabled.\n\n"
        "MEASURED 2026-08-15: the disclaimer URL returns 200 but its body is "
        "the single word 'Disclaimer' -- the text is injected client-side, "
        "so there is no served document to quote. More to the point, the "
        "blocker here is structural rather than textual: CourtConnect gates "
        "access behind a CLICK-THROUGH disclaimer, and clicking 'I accept' "
        "is the formation of an agreement. That is a legal act, and this "
        "pipeline does not perform legal acts on the user's behalf -- no "
        "matter how easy the Oracle OWA URL structure makes it to skip.\n\n"
        "Delaware is a state court and its records are public, so the "
        "underlying data is almost certainly usable. The route is a human "
        "reading and accepting the disclaimer and recording that here, or an "
        "email to the AOC -- not a programmatic accept."
    ),
    notes=(
        "Oracle OWA frameset, NOT ASP.NET -- stateless GET params, no "
        "__VIEWSTATE, genuinely scriptable IF permitted. Second blocker: the "
        "'judgment search' feature described in secondary sources was NOT "
        "found -- the public menu offers only docket/judge/party. Verify by "
        "hand before scoping. Court of Chancery is NOT here (paywalled via "
        "File & ServeXpress) and remains a known gap."
    ),
))

_register(SourcePolicy(
    source_id="claims_epiq",
    display_name="Epiq bankruptcy docket mirror",
    tier="B",
    status=ToSStatus.PROHIBITED,
    rate_key="claims-agent",
    base_confidence=0.85,
    tos_urls=(
        "https://dm.epiq11.com/",
        "https://www.epiqglobal.com/en-us/terms-of-use",
    ),
    reviewed_by="machine-assisted read, 2026-08-15 -- confirm before relying",
    reviewed_at=date(2026, 8, 15),
    review_note=(
        "PROHIBITED on the operative clause, read 2026-08-15 at "
        "epiqglobal.com/en-us/terms-of-use. VERBATIM:\n\n"
        '  "Any use of direct or indirect automated searches or data '
        'queries with respect to this Web Site or the Materials is '
        'strictly prohibited without Epiq\'s prior written consent."\n\n'
        "That is a flat prohibition on exactly what this pipeline does, and "
        "it is not qualified by purpose -- a research purpose does not reach "
        "it. The same page also bars reproduction 'without the prior express "
        "written permission of Epiq'. "
        "There is a lawful route and it is not technical: ask Epiq for "
        "written consent. Until that exists in writing, this source stays "
        "off, and PROHIBITED has no configuration escape hatch."
    ),
    notes=(
        "Angular SPA -- root is ~3.3 KB with <app-controller>, so there is no "
        "HTML to parse anyway. The XHR endpoint was never pinned and must not "
        "be: pinning it would only be useful for doing the thing the terms "
        "forbid."
    ),
))

_register(SourcePolicy(
    source_id="claims_stretto",
    display_name="Stretto bankruptcy docket mirror",
    tier="B",
    status=ToSStatus.VERIFIED_PERMITTED,
    rate_key="claims-agent",
    base_confidence=0.85,
    tos_urls=(
        "https://cases.stretto.com/",
        # CORRECTED 2026-08-15: /terms-of-use 404s, and the bare stretto.com
        # host 403s its own robots.txt (so that host is treated as refusing).
        # The readable copy is on the www host, which serves robots.txt 200
        # with an empty Disallow.
        "https://www.stretto.com/legal-policies/",
    ),
    reviewed_by="machine-assisted read, 2026-08-15 -- confirm before relying",
    reviewed_at=date(2026, 8, 15),
    expires_at=date(2027, 8, 15),
    review_note=(
        "THE ONLY TIER B SOURCE THAT CLEARED. Read 2026-08-15 from "
        "'STRETTO Web Sites Terms and Conditions v.2025.1' (PDF linked from "
        "www.stretto.com/legal-policies/).\n\n"
        "The document contains NO general prohibition on automated access. "
        "Its only scraping clause sits inside section 21, 'Additional Terms "
        "and Conditions of Use for the AI TOOLS', and is scoped to those "
        "tools. VERBATIM:\n\n"
        '  "You shall not ... use web scraping, web harvesting, web data '
        'extraction or any other method to extract data from the AI TOOLS '
        'or any OUTPUTS"\n\n'
        "Every restriction in the document was enumerated; none of the "
        "others reaches ordinary automated access to the public case "
        "dockets, and there is no 'personal, non-commercial use only' clause "
        "of the kind Angeion carries. robots.txt at www.stretto.com returns "
        "200 with an empty Disallow (plus /cgi-bin/ and /wp-admin/); "
        "cases.stretto.com serves no robots.txt at all (404 = nothing "
        "disallowed, which is NOT the same as the 403 refusals seen "
        "elsewhere).\n\n"
        "HARD BOUNDARY, enforced by allowed_url_patterns below: the "
        "connector must never touch 'Stretto Conductor', the AI assistant "
        "advertised on cases.stretto.com. That is precisely what section 21 "
        "protects, and the one clause in this document that would be "
        "violated by getting it wrong."
    ),
    # Scoped to the public case-docket surface. wp-content is here because
    # the case list is rendered client-side and its endpoint is only
    # discoverable by reading the site's own served JavaScript.
    allowed_url_patterns=(
        "https://cases.stretto.com/",
        "https://cases.stretto.com/?*",
        "https://cases.stretto.com/api/*",
        "https://cases.stretto.com/wp-json/*",
        "https://cases.stretto.com/wp-content/*",
        "https://cases.stretto.com/wp-admin/admin-ajax.php*",
    ),
    notes=(
        "WordPress (api.w.org, wp/v2/pages, admin-ajax.php in page source). "
        "Phase 7."
    ),
))

_register(SourcePolicy(
    source_id="claims_verita",
    display_name="Verita / KCC bankruptcy docket mirror",
    tier="B",
    status=ToSStatus.UNVERIFIED,
    rate_key="claims-agent",
    base_confidence=0.85,
    tos_urls=(
        "https://www.veritaglobal.net/",
        # The docket host is .net; the terms live on the .com corporate site.
        "https://veritaglobal.com/terms-conditions/",
    ),
    reviewed_by="machine-assisted read, 2026-08-15 -- INCONCLUSIVE",
    reviewed_at=date(2026, 8, 15),
    review_note=(
        "REVIEW ATTEMPTED AND NOT COMPLETED -- stays UNVERIFIED and disabled, "
        "which is the correct resting state for a question nobody has "
        "answered.\n\n"
        "The terms at veritaglobal.com/terms-conditions/ carry a broad "
        "reproduction restriction. VERBATIM:\n\n"
        '  "Except as stated herein, none of the Resources on this website '
        'may be copied, reproduced, distributed, republished, downloaded, '
        'displayed, posted or transmitted"\n\n'
        "but NO clause addressing automated access specifically. Whether a "
        "general reproduction bar reaches machine reading of public court "
        "dockets is exactly the judgment a human should make, not one to "
        "infer. Nothing here is a refusal; nothing here is permission "
        "either.\n\n"
        "SEPARATE TECHNICAL NOTE, so it is not mistaken for a refusal: "
        "www.veritaglobal.net fails TLS verification under certifi with "
        "'unable to get local issuer certificate'. A direct socket handshake "
        "against the OS trust store succeeds (GoDaddy G2 chain), so the "
        "server is omitting an intermediate that Windows happens to have "
        "cached. That is a trust-chain gap on our side, NOT the host turning "
        "us away."
    ),
    notes="Formerly Kurtzman Carson Consultants (KCC). Legacy kccllc.net may still serve.",
))

_register(SourcePolicy(
    source_id="claims_omni",
    display_name="Omni Agent Solutions bankruptcy docket mirror",
    tier="B",
    status=ToSStatus.PROHIBITED,
    rate_key="claims-agent",
    base_confidence=0.85,
    tos_urls=("https://www.omniagentsolutions.com/",),
    robots_ai_signal="search=yes, ai-train=no, use=reference",
    reviewed_by="machine-assisted read, 2026-08-15 -- confirm before relying",
    reviewed_at=date(2026, 8, 15),
    review_note=(
        "PROVISIONAL, on observed behavior. MEASURED 2026-08-15: "
        "www.omniagentsolutions.com returns HTTP 403 to an honest identified "
        "client. Treated as refusal of consent.\n\n"
        "THIS REVERSES THE EARLIER PROVISIONAL READING, and the reversal is "
        "the point worth keeping. The prior note reasoned from robots.txt: "
        "the Content-Signal says 'use=reference' (yes) and 'ai-train=no', "
        "named AI crawlers are Disallow: / but our agent is not one of them, "
        "and the '*' group permits us -- so the file leaned permissive. "
        "Then the server refused the request anyway.\n\n"
        "When a site's robots.txt says yes and its server says no, the "
        "server wins. A permissive robots file is not consent; it is a "
        "statement about crawlers, and the operator is free to refuse a "
        "specific client regardless. Reasoning from the file alone would "
        "have had us retrying against a host that had already turned us "
        "away."
    ),
    notes=(
        "Appears in the Phase 5 census as the retained agent on 5 chapter 11 "
        "cases, so the routing entry stays useful even though the docket "
        "mirror is off-limits."
    ),
))

_register(SourcePolicy(
    source_id="claims_angeion",
    display_name="Angeion Group bankruptcy docket mirror (incl. Donlin Recano)",
    tier="B",
    status=ToSStatus.PROHIBITED,
    rate_key="claims-agent",
    base_confidence=0.85,
    tos_urls=(
        "https://bankruptcy.angeiongroup.com/",
        # CORRECTED 2026-08-15: www.angeiongroup.com/terms-of-use 404s. The
        # docket site publishes its own terms.
        "https://bankruptcy.angeiongroup.com/Home/Terms",
    ),
    reviewed_by="machine-assisted read, 2026-08-15 -- confirm before relying",
    reviewed_at=date(2026, 8, 15),
    review_note=(
        "PROHIBITED on the operative clauses, read 2026-08-15 at "
        "bankruptcy.angeiongroup.com/Home/Terms. TWO independent bars, "
        "either one sufficient. VERBATIM:\n\n"
        '  "Use any robot, spider or other automatic device, process or '
        'means to access this Website for any purpose, including '
        'monitoring or copying any of the material on this Website"\n\n'
        '  "These Terms of Use permit you to use this Website for your '
        'personal, non-commercial use only."\n\n'
        "The robot clause is UNCONDITIONAL -- 'for any purpose' -- which is "
        "the same construction as the NY UCS bot clause. A research purpose "
        "does not reach an unconditional prohibition; that is what makes it "
        "unconditional. Note also that the site served us content happily "
        "and its robots.txt '*' group permits us: technical accessibility "
        "again is not permission."
    ),
    notes=(
        "donlinrecano.com now 301s to bankruptcy.angeiongroup.com -- the two "
        "vendors are one adapter, not two, and one prohibition covers both. "
        "Appears in the Phase 5 census on 5 cases; the routing entry stays."
    ),
))

_register(SourcePolicy(
    source_id="claims_bmc",
    display_name="BMC Group bankruptcy docket mirror",
    tier="B",
    status=ToSStatus.UNVERIFIED,
    rate_key="claims-agent",
    base_confidence=0.85,
    # www.bmcgroup.com is a 348-byte redirect stub to www3. MEASURED 2026-08-15.
    tos_urls=("https://www.bmcgroup.com/", "https://www3.bmcgroup.com/"),
    reviewed_by="machine-assisted read, 2026-08-15 -- INCONCLUSIVE",
    reviewed_at=date(2026, 8, 15),
    review_note=(
        "REVIEW CANNOT BE COMPLETED: BMC PUBLISHES NO TERMS OF USE. Stays "
        "UNVERIFIED and disabled.\n\n"
        "MEASURED 2026-08-15: www.bmcgroup.com is a 348-byte redirect stub "
        "to www3.bmcgroup.com, which serves fine (200, 62 KB) and links only "
        "a Privacy Policy at /privacy/ plus a TRUSTe seal. There is no terms "
        "-of-use document anywhere on the site.\n\n"
        "The absence of terms is NOT permission. A site that has published "
        "no rules has not thereby agreed to anything, and the gate's default "
        "for an unanswered question is off. If this source is wanted, ask "
        "BMC in writing -- there is no document to read our way to a yes."
    ),
))


# ---------------------------------------------------------------------------
# Blocked -- observed refusal of consent, or explicit prohibition in terms.
# ---------------------------------------------------------------------------

_register(SourcePolicy(
    source_id="claims_kroll",
    display_name="Kroll Restructuring Administration",
    tier="B",
    status=ToSStatus.PROHIBITED,
    rate_key="claims-agent",
    tos_urls=(
        "https://www.kroll.com/en/terms-and-conditions",
        "https://cases.ra.kroll.com/",
    ),
    review_note=(
        "PROVISIONAL, on observed behavior rather than read terms: "
        "cases.ra.kroll.com returns 403 on BOTH /robots.txt and / to an "
        "honest, identified, descriptive user agent. A site that will not "
        "serve its own robots.txt to an identified crawler is refusing "
        "consent. This is not merely 'terms unverified'. Downgrade to "
        "UNVERIFIED only if a human reads the terms and finds them "
        "permissive. Do NOT resolve this with a headless browser -- that "
        "converts a technical block into deliberate circumvention, a "
        "materially worse posture."
    ),
    reviewed_at=date(2026, 8, 14),
    notes="Largest share of mega-cases, which makes this a real coverage loss.",
))

_register(SourcePolicy(
    source_id="nc_business_court",
    display_name="North Carolina Business Court",
    tier="B",
    status=ToSStatus.PROHIBITED,
    rate_key="_default",
    tos_urls=(
        "https://www.nccourts.gov/terms-of-use",
        "https://www.nccourts.gov/robots.txt",
    ),
    review_note=(
        "PROVISIONAL, on observed behavior: nccourts.gov returns 403 on "
        "/robots.txt to an identified client. Same reasoning as Kroll. "
        "Volume here was always low, so this is not worth WAF-fighting."
    ),
    reviewed_at=date(2026, 8, 14),
))

_register(SourcePolicy(
    source_id="ny_iapps_scrape",
    display_name="NY Unified Court System e-Courts / WebCivil / NYSCEF",
    tier="C",
    status=ToSStatus.PROHIBITED,
    rate_key="_default",
    tos_urls=("https://iapps.courts.state.ny.us/webcivilLocal/TermsOfUse",),
    review_note=(
        'VERBATIM from the NY UCS Terms of Use: "Data may not be mined or '
        "sold, or used in any pay-for-use application, without the express "
        "written permission of UCS. This site may not be accessed by any "
        'automated program ("bot") for the purpose of extracting data for any '
        'use."  The bot clause is UNCONDITIONAL -- "for any use" -- so the '
        "research purpose does not reach it. Note the site returns HTTP 200 "
        "to automated clients, which makes it a trap: technical accessibility "
        "is not permission. PERMANENT. Email ingestion (source_id "
        "'etrack_email') is the only lawful path to this data."
    ),
    reviewed_at=date(2026, 8, 14),
))

_register(SourcePolicy(
    source_id="pacer",
    display_name="PACER / CM-ECF",
    tier="A",
    status=ToSStatus.PROHIBITED,
    rate_key="_default",
    review_note=(
        "Seam defined, not implemented. Structural limits make it unsuitable "
        "for discovery regardless of permission: the Case Locator API returns "
        "case METADATA ONLY (no docket text), search results bill $0.10/page "
        "with NO cap, and charges apply even on zero results. Scripting "
        "free-look or fee-free pages to avoid billing is the clause carrying "
        "criminal/civil liability language and is permanently out of scope. "
        "Revisit ONLY as targeted enrichment, and only if an academic-"
        "researcher fee exemption is actually granted."
    ),
    reviewed_at=date(2026, 8, 14),
))


# ---------------------------------------------------------------------------
# Tier C -- push / email ingestion. Not scraping.
# ---------------------------------------------------------------------------

_register(SourcePolicy(
    source_id="etrack_email",
    display_name="NY eTrack alert emails (IMAP ingestion)",
    tier="C",
    status=ToSStatus.RESTRICTED,
    rate_key="_default",
    base_confidence=0.9,
    tos_urls=("https://iapps.courts.state.ny.us/webcivilLocal/TermsOfUse",),
    review_note=(
        "RESTRICTED pending a deliberate decision. Receiving and parsing "
        "alert emails you subscribed to is not 'accessing the site by an "
        "automated program' -- UCS sends these to enrolled subscribers, and "
        "eTrack enrollment is open to non-parties. The residual question is "
        'the separate "may not be mined" clause, which is broad enough that a '
        "conservative reading could reach automated parsing of the alerts. "
        "Lower stakes under a research purpose, but decide deliberately "
        "before enabling."
    ),
    notes=(
        "Only free route to NY Commercial Division decisions, since scraping "
        "is permanently barred. Requires a dedicated mailbox and MANUAL "
        "per-case enrollment via a web form -- the pipeline's job is to "
        "produce a short ranked enrollment worklist and auto-confirm "
        "enrollment when the first alert arrives for an index number."
    ),
))


def get_policy(source_id: str) -> SourcePolicy:
    """Look up a policy. Unknown sources are treated as UNVERIFIED, not allowed.

    Fail-closed: a connector that forgets to register itself is disabled, not
    silently permitted.
    """
    policy = POLICIES.get(source_id)
    if policy is None:
        return SourcePolicy(
            source_id=source_id,
            display_name=f"(unregistered) {source_id}",
            tier="?",
            status=ToSStatus.UNVERIFIED,
            notes="Not present in the compliance registry. Fail-closed.",
        )
    return policy
