-- LitFin SQLite schema.
--
-- Why SQLite: single user, single writer, ~10^4-10^5 rows/year, must run
-- offline on a laptop, zero install, and needs full-text search -- FTS5 and
-- JSON1 ship with Python's bundled SQLite. Postgres adds a service to babysit
-- for no benefit at this scale. Revisit only past ~5 GB.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- Compliance + health state per source. The authoritative POLICY lives in
-- version control (compliance/registry.py); this table is the runtime mirror
-- plus observed health.
CREATE TABLE IF NOT EXISTS source (
  source_id            TEXT PRIMARY KEY,
  display_name         TEXT NOT NULL DEFAULT '',
  tier                 TEXT NOT NULL DEFAULT '?',
  status               TEXT NOT NULL DEFAULT 'unverified',
  base_confidence      REAL NOT NULL DEFAULT 0.8
                         CHECK (base_confidence BETWEEN 0 AND 1),
  enabled              INTEGER NOT NULL DEFAULT 0,
  last_success_at      TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  health               TEXT NOT NULL DEFAULT 'unknown',
  health_note          TEXT
);

-- Global daily request budget, persisted so a crash-restart loop cannot
-- silently reset it.
CREATE TABLE IF NOT EXISTS request_budget (
  day TEXT PRIMARY KEY,
  n   INTEGER NOT NULL DEFAULT 0
);

-- Content-addressed raw artifact index. Every response body -- HTTP or email
-- -- is registered here before parsing, so any extracted value can be traced
-- back to the exact bytes it came from, and so parser regressions can be
-- replayed against stored input.
CREATE TABLE IF NOT EXISTS artifact (
  sha256        TEXT PRIMARY KEY,
  source_id     TEXT NOT NULL,
  url           TEXT NOT NULL,
  http_status   INTEGER,
  content_type  TEXT,
  byte_size     INTEGER NOT NULL DEFAULT 0,
  etag          TEXT,
  last_modified TEXT,
  fetched_at    TEXT NOT NULL,
  run_id        TEXT,
  ext           TEXT NOT NULL DEFAULT 'bin',
  compressed    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_artifact_source ON artifact(source_id, fetched_at);

-- A normalized observation extracted from an artifact. item_uid is
-- deterministic -- sha256(source_id || natural_key) -- which is what makes
-- redoing a task a no-op and gives us exactly-once effect from at-least-once
-- delivery.
CREATE TABLE IF NOT EXISTS item (
  item_uid        TEXT PRIMARY KEY,
  source_id       TEXT NOT NULL,
  natural_key     TEXT NOT NULL,
  artifact_sha256 TEXT REFERENCES artifact(sha256),
  extract_locator TEXT,
  source_url      TEXT,
  title           TEXT,
  body            TEXT,
  published_at    TEXT,
  observed_at     TEXT NOT NULL,
  payload_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_item_source ON item(source_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_observed ON item(observed_at DESC);

-- Full-text index over item text, for the dashboard's search box and for
-- taxonomy pattern sweeps.
CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5(
  title, body, content='item', content_rowid='rowid'
);

-- Per (source, task) watermark. Advanced in the SAME transaction as the
-- items it covers -- see store/db.py commit_task().
CREATE TABLE IF NOT EXISTS watermark (
  source_id  TEXT NOT NULL,
  task_key   TEXT NOT NULL,
  value      TEXT,
  seen_keys  TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT,
  PRIMARY KEY (source_id, task_key)
);

CREATE TABLE IF NOT EXISTS run (
  run_id      TEXT PRIMARY KEY,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL DEFAULT 'running',
  purpose     TEXT NOT NULL DEFAULT 'research',
  note        TEXT
);

CREATE TABLE IF NOT EXISTS run_task (
  run_id    TEXT NOT NULL,
  task_id   TEXT NOT NULL,
  source_id TEXT NOT NULL,
  task_key  TEXT NOT NULL,
  status    TEXT NOT NULL DEFAULT 'PENDING'
              CHECK (status IN ('PENDING','RUNNING','OK','FAILED','SKIPPED','BROKEN','DEGRADED')),
  attempts  INTEGER NOT NULL DEFAULT 0,
  rows_parsed INTEGER NOT NULL DEFAULT 0,
  rows_new    INTEGER NOT NULL DEFAULT 0,
  error     TEXT,
  started_at  TEXT,
  finished_at TEXT,
  PRIMARY KEY (run_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_run_task_status ON run_task(run_id, status);

-- Result of the cheap pre-LLM screens, kept so the exclusion filter can be
-- audited. A screen you cannot audit is a screen you cannot trust.
CREATE TABLE IF NOT EXISTS screen_result (
  item_uid   TEXT PRIMARY KEY,
  verdict    TEXT NOT NULL,          -- 'excluded' | 'no_signal'
  reason     TEXT,
  screened_at TEXT NOT NULL
);

-- One Opus extraction per item.
CREATE TABLE IF NOT EXISTS extraction (
  item_uid       TEXT PRIMARY KEY REFERENCES item(item_uid),
  model          TEXT NOT NULL,
  extracted_at   TEXT NOT NULL,
  payload_json   TEXT NOT NULL,
  case_caption   TEXT,
  court          TEXT,
  venue          TEXT,
  jurisdiction   TEXT,
  practice_area  TEXT,
  deal_thesis    TEXT,
  event_type     TEXT,
  event_date     TEXT,
  is_excluded    INTEGER NOT NULL DEFAULT 0,
  damages_usd    REAL,
  damages_conf   TEXT,
  summary        TEXT,
  -- An exclusion the PIPELINE applied after extraction (e.g. the in rem
  -- forfeiture caption convention, which only becomes visible once the model
  -- has produced a caption). Kept separate from the model's own
  -- excluded_reason so the audit trail shows who decided what.
  excluded_reason_late TEXT NOT NULL DEFAULT '',
  -- Which extraction schema produced this row, so a new field can be
  -- back-filled with `extract --refresh` instead of re-extracting everything.
  schema_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_extraction_thesis ON extraction(deal_thesis, event_date DESC);

-- Batch submissions, so a run can be resumed and results collected later.
CREATE TABLE IF NOT EXISTS extract_batch (
  batch_id     TEXT PRIMARY KEY,
  submitted_at TEXT NOT NULL,
  n_requests   INTEGER NOT NULL DEFAULT 0,
  n_stored     INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'submitted'
);

-- Computed score per item. Recomputed offline from stored extractions, so
-- tuning weights costs seconds and no API calls.
CREATE TABLE IF NOT EXISTS prospect (
  item_uid     TEXT PRIMARY KEY REFERENCES item(item_uid),
  scored_at    TEXT NOT NULL,
  score        REAL NOT NULL,
  rank_in_run  INTEGER,
  components_json TEXT NOT NULL DEFAULT '{}',
  -- Entity resolution, recomputed on every rank. One matter often arrives as
  -- several documents (a DOJ press release AND its case-filing page; four
  -- RECAP entries from one week of a bankruptcy). All rows are KEPT -- each
  -- is a real observation of a real source -- and only the primary is shown.
  -- See score/cluster.py for why this is not done at ingestion.
  cluster_key  TEXT NOT NULL DEFAULT '',
  cluster_size INTEGER NOT NULL DEFAULT 1,
  is_primary   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_prospect_score ON prospect(score DESC);
-- idx_prospect_cluster is created in db.py AFTER migrations run. An index on
-- a migrated column cannot live here: on an existing database CREATE TABLE IF
-- NOT EXISTS is a no-op, so the column does not exist yet when this script
-- executes and the index statement fails the whole schema load.

-- Per-venue RECAP coverage. The `confidence` column is what stops an empty
-- venue from being read as a quiet venue: in a court with no PACER RSS feed,
-- absence of signal is not absence of activity.
CREATE TABLE IF NOT EXISTS court_coverage (
  court_id       TEXT PRIMARY KEY,
  full_name      TEXT NOT NULL DEFAULT '',
  jurisdiction   TEXT,
  pacer_court_id TEXT,
  has_rss        INTEGER,            -- 1 / 0 / NULL (not a PACER court)
  entry_types    TEXT NOT NULL DEFAULT '',
  confidence     TEXT NOT NULL DEFAULT 'not_applicable',
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coverage_conf ON court_coverage(confidence);

-- CourtListener docket alerts. Subscribing is how monitoring happens: the
-- read API caps at 50/hour so polling cannot work, but alerts are unlimited
-- on a $10 membership AND subscribing causes CourtListener to actively scrape
-- that docket, which improves coverage as a side effect.
CREATE TABLE IF NOT EXISTS docket_alert (
  docket_id     INTEGER PRIMARY KEY,
  cl_alert_id   INTEGER,
  status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','active','failed','unsubscribed')),
  case_name     TEXT NOT NULL DEFAULT '',
  docket_url    TEXT NOT NULL DEFAULT '',
  reason        TEXT NOT NULL DEFAULT '',
  score_hint    REAL NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  last_event_at TEXT,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_status ON docket_alert(status);

-- Inbound webhook deliveries. The receiver's ONLY job is to write a row here
-- and return 2xx in under a second -- CourtListener auto-disables an endpoint
-- after 8 consecutive failures, and it retries for ~54 hours, so processing
-- inline would be a self-inflicted outage.
--
-- Keyed on the Idempotency-Key header because retries re-deliver the same
-- event; there is NO HMAC signature available, so payloads are untrusted.
CREATE TABLE IF NOT EXISTS webhook_event (
  idempotency_key TEXT PRIMARY KEY,
  received_at     TEXT NOT NULL,
  event_type      INTEGER,
  remote_addr     TEXT,
  payload_json    TEXT NOT NULL,
  processed_at    TEXT,
  process_error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_unprocessed
  ON webhook_event(processed_at) WHERE processed_at IS NULL;

-- NY eTrack enrollment worklist (Phase 6).
--
-- Enrollment is a MANUAL act: a human fills in a web form on the UCS site,
-- one case at a time. The pipeline cannot do it and must not try -- scraping
-- NY UCS is permanently PROHIBITED on an unconditional bot clause. What the
-- pipeline CAN do is produce a short, well-ranked worklist of index numbers
-- worth enrolling, and then confirm enrollment automatically when the first
-- alert email arrives for that index number.
--
-- `status` therefore tracks a human's progress, not a fetch:
--   candidate -> the pipeline suggests it
--   enrolled  -> the human says they submitted the form
--   confirmed -> an alert email actually arrived (the only proof that works)
CREATE TABLE IF NOT EXISTS etrack_enrollment (
  index_number TEXT PRIMARY KEY,
  caption      TEXT NOT NULL DEFAULT '',
  court        TEXT NOT NULL DEFAULT '',
  county       TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'candidate'
                 CHECK (status IN ('candidate','enrolled','confirmed','dropped')),
  reason       TEXT NOT NULL DEFAULT '',
  score_hint   REAL NOT NULL DEFAULT 0,
  added_at     TEXT NOT NULL,
  enrolled_at  TEXT,
  confirmed_at TEXT,
  last_alert_at TEXT,
  alert_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_etrack_status ON etrack_enrollment(status);

-- Observed health history per source, used by the staleness alarm to learn
-- each source's normal gap between non-empty runs.
CREATE TABLE IF NOT EXISTS source_health (
  source_id   TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  verdict     TEXT NOT NULL,
  rows_parsed INTEGER NOT NULL DEFAULT 0,
  rows_new    INTEGER NOT NULL DEFAULT 0,
  byte_size   INTEGER NOT NULL DEFAULT 0,
  note        TEXT,
  PRIMARY KEY (source_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_health_source ON source_health(source_id, observed_at DESC);
