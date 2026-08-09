# SQL Dialect — SQLite

> Loaded by the `sql` skill for SQLite work. Engine semantics and SQL only — the Rust
> embedding templates (connection factory, pooling, the pragma contract expressed in code)
> live in `../../rust/references/sqlite-rusqlite.md`; the two files cross-link and do not
> duplicate each other.

## 1. Version status

- Baseline **3.51.3+** (2026-03) — contains the fix for a WAL-reset database-corruption
  bug; do not target older releases in new work. Verify current at write time rather than
  trusting a pinned number here.
- **Bundled copies rule.** Language drivers pin their own SQLite: rusqlite's `bundled`
  feature compiles a specific release into the binary, better-sqlite3 compiles its own,
  `node:sqlite` ships whatever the Node runtime bundles, sqlite-jdbc carries a native
  build. Know which version *your driver* ships — run `SELECT sqlite_version();` **through
  the driver**, not the system CLI; the two routinely differ.
- **Volatility note.** Features below carry their minimum version (STRICT 3.37, RETURNING
  3.35, `contentless_delete` 3.43). Check them against the driver-shipped version, not the
  documentation of the newest release.

## 2. Single-writer reality

SQLite has **one writer at a time** — per database file, across all processes. WAL mode
lets readers coexist with the writer (readers see a stable snapshot while a write is in
flight, and the writer never waits for readers), but two writes never run concurrently;
the second waits or fails. Design for this instead of discovering it under load.

| SQLite is the right tool | SQLite is the wrong tool |
|---|---|
| Local-first desktop/mobile app | Writers in more than one process |
| Embedded single-process daemon | Multi-host access to one database |
| Cache / derived-data store | Row-level authorization needs |
| Single-host CLI or dev tool | Sustained concurrent OLTP writes |

A web app with one app-server process and modest write rates sits in the left column more
often than folklore admits — measure the write contention before graduating (§9), and
graduate on evidence.

**`BEGIN IMMEDIATE` for write transactions.** A deferred `BEGIN` that reads first and then
writes must *upgrade* its lock mid-transaction; if another writer got there first, the
upgrade fails with `SQLITE_BUSY` after work has been done, and retrying means replaying
the whole unit. `BEGIN IMMEDIATE` takes the write lock up front — it fails fast at
`BEGIN`, where a retry is a trivial loop.

```sql
BEGIN IMMEDIATE;
UPDATE accounts SET balance = balance - 100 WHERE id = 1;
UPDATE accounts SET balance = balance + 100 WHERE id = 2;
COMMIT;
```

**`busy_timeout` is mandatory.** Without it, any lock contention returns `SQLITE_BUSY`
immediately instead of waiting. `PRAGMA busy_timeout = 5000;` is the baseline; raise it
for write-heavy daemons.

**Long reads pin the WAL.** A checkpoint cannot advance past the oldest open read
transaction, so a long-running reader makes the `-wal` file grow without bound while it
sits there. Keep read transactions short too — snapshot-for-the-duration is a cost, not
a free lunch.

**`SQLITE_BUSY` vs `SQLITE_LOCKED`** — they demand opposite responses:
- `SQLITE_BUSY` — a *different connection* holds a conflicting lock. Waiting
  (`busy_timeout`) and retrying helps.
- `SQLITE_LOCKED` — a conflict *inside your own connection* (or shared cache), e.g.
  writing a table while an unfinalized read cursor is still walking it. No amount of
  waiting helps; the code is wrong.

## 3. Per-connection pragma contract

`PRAGMA foreign_keys` is **per-connection and OFF by default** — the single most common
SQLite integrity bug is a codebase that believes its foreign keys are enforced when no
connection ever turned them on. The fix is structural, not disciplinary: every connection
is opened through **one choke point** (a connection factory) that applies the contract,
and a test opens a connection through that factory and asserts the values.

```sql
-- Applied on EVERY connection open, through the one connection factory:
PRAGMA foreign_keys = ON;      -- per-connection; OFF by default — the big one
PRAGMA busy_timeout = 5000;    -- per-connection; without it, contention = instant SQLITE_BUSY
PRAGMA journal_mode = WAL;     -- persists in the file, but verify: the pragma RETURNS the active mode
PRAGMA synchronous = NORMAL;   -- under WAL — see the tradeoff below

-- Optional, workload-dependent:
PRAGMA cache_size = -32000;    -- negative = KiB, so 32 MiB page cache
PRAGMA temp_store = MEMORY;
```

- `journal_mode = WAL` is stored in the database file and survives reopens — but apply it
  anyway and check the returned value: on a network filesystem or a read-only open it can
  silently come back `delete`.
- **`synchronous = NORMAL` under WAL** — the stated tradeoff: fsync happens at checkpoint
  rather than at every commit, so a power cut can lose the most recent commits but can
  never corrupt the database. `FULL` pays an fsync per commit. Choose per database *role*
  — user-of-record data vs rebuildable cache — and write the choice down next to the
  pragma.
- The verifying test: open a connection via the factory and assert the values the engine
  reports back — not the values the factory *sent*:

```sql
PRAGMA foreign_keys;    -- must return 1
PRAGMA journal_mode;    -- must return wal
PRAGMA busy_timeout;    -- must return the contract value
```

  Without the test, the contract decays the first time someone adds a second code path
  that opens a connection.

## 4. `user_version` migrations

The standard SQL-level pattern — no framework required. The applied-set marker is
`PRAGMA user_version`, an integer stored in the database header.

```sql
-- The runner (app startup, or a migrate subcommand) does:
--   1. open a connection through the factory (pragma contract applied)
--   2. read the current version:
PRAGMA user_version;
--   3. for each migration N greater than user_version, in ascending order:
BEGIN;
-- ... migration N statements — DDL is transactional in SQLite, use that ...
ALTER TABLE users ADD COLUMN locale TEXT;
PRAGMA user_version = 3;   -- N: the version bump rides in the SAME transaction
COMMIT;
```

- **Each migration and its version bump commit atomically.** A crash mid-migration leaves
  the database at the previous version with the previous schema — rerunnable, never
  half-applied. This is a real advantage over engines whose DDL auto-commits; do not waste
  it by bumping the version outside the transaction.
- **Forward-only.** There is no down-migration story worth rehearsing for an embedded
  database; recovery is restore-from-backup (§8).
- **The schema-equivalence test:** migrate an empty database from 0 to head, and compare
  its schema against a from-scratch reference (or a golden snapshot) — normalize both
  sides via `sqlite_master` and diff:

```sql
SELECT type, name, sql FROM sqlite_master
WHERE name NOT LIKE 'sqlite_%'
ORDER BY type, name;
```

- **`CREATE TABLE IF NOT EXISTS` is bootstrap-only.** As a migration story it guarantees
  divergence: existing installs never get altered, and SQLite has no rich `ALTER` to
  paper over the difference later. New tables arrive in numbered migrations like
  everything else.
- If you need *named, checksummed* migrations rather than an integer high-water mark,
  use a `schema_migrations` table exactly as in `../SKILL.md` — `user_version` is the
  zero-dependency default, not the only legal shape.

## 5. STRICT tables & type discipline

**`STRICT` (3.37+) on all new tables.** Without it, column types are *affinities*, not
constraints — and the affinity rules bite:

- A `TEXT` column stores the integer `42` **as an integer** (`typeof()` says `integer`).
- An `INTEGER` column accepts `'abc'` and stores it as text.
- Comparisons and sorts then behave per-row, by stored type, not by declared type.

`STRICT` turns a type mismatch into an error at insert time, where it is cheap. Audit a
legacy table before trusting it:

```sql
-- What is actually stored, per declared column:
SELECT typeof(created_at) AS stored_type, count(*) FROM users GROUP BY stored_type;
```

```sql
CREATE TABLE users (
    id         INTEGER PRIMARY KEY,   -- rowid alias; see AUTOINCREMENT note below
    email      TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,      -- unix seconds: one datetime policy per database
    status     TEXT NOT NULL DEFAULT 'active'
               CHECK (status IN ('active', 'suspended', 'deleted'))
) STRICT;
```

- **One datetime encoding per database** — `INTEGER` unix seconds or `TEXT` RFC 3339. Pick
  one, document it, and never mix: SQLite has no datetime type, so a mixed column sorts
  and compares nonsense without erroring.
- **`CHECK` constraints are cheap — use them.** Enumerations, ranges, non-empty strings:
  the constraint runs at insert, the bug report runs at 2 a.m.
- **`WITHOUT ROWID`** when the primary key *is* the access path and rows are small
  (roughly: fits well inside a page) — composite-key lookup tables, join tables. Note that
  FTS5 external-content indexing needs the base table's rowid, so content tables keep
  theirs.
- **`AUTOINCREMENT` is usually unnecessary.** Plain `INTEGER PRIMARY KEY` aliases the
  rowid; the only difference is that after you delete the row holding the maximum rowid,
  its id can be reused. `AUTOINCREMENT` prevents that reuse (via `sqlite_sequence`) at a
  small cost — justified only when a reused id would be visible harm, e.g. external
  systems still referencing the deleted row.

## 6. FTS5

Use the **external-content** form: the searchable text lives once, in the base table; the
FTS index carries only the index.

```sql
CREATE VIRTUAL TABLE docs_fts USING fts5(
    title, body,
    content = 'docs',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);
-- The tokenizer is baked in at CREATE time — changing it later means dropping and
-- rebuilding the index. 'porter' adds English stemming: more recall, less precision.

-- The complete trigger set — all three, or the index silently drifts from the table:
CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
END;

CREATE TRIGGER docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO docs_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

-- After bulk-loading the base table, or if the triggers were ever disabled:
INSERT INTO docs_fts(docs_fts) VALUES ('rebuild');
```

The `'delete'` insert is not a typo — external-content deletion is expressed as an INSERT
naming the old values; a plain `DELETE FROM docs_fts` corrupts the index.

**MATCH input is an expression language** — operators, quotes, `col:` filters, `NEAR()`,
`*`. Raw user input passed to `MATCH` is at best a syntax-error generator and at worst a
query the user never meant. Sanitise through an allowlist:

```text
tokenize:  split the user's input on whitespace; drop characters outside [A-Za-z0-9_-]
quote:     wrap every surviving term in double quotes: "term"
prefix:    for search-as-you-type, append * after the closing quote: "term"*
join:      explicit AND (or OR) between quoted terms — never pass raw operators through
bind:      the assembled string is ONE bind parameter:  ... WHERE docs_fts MATCH ?
test:      hostile inputs go in the suite:  "   (   NEAR(   col:x   -term   *   ""
```

**Ranking and display:**

```sql
SELECT d.id, d.title,
       snippet(docs_fts, 1, '<b>', '</b>', '…', 12) AS excerpt
FROM docs_fts
JOIN docs d ON d.id = docs_fts.rowid
WHERE docs_fts MATCH ?
ORDER BY bm25(docs_fts, 10.0, 1.0)   -- title weighted 10x body; smaller = better match
LIMIT 20;
```

`bm25()` returns more-negative-is-better, so ascending `ORDER BY` puts the best match
first. `highlight()` marks matches in a full column; `snippet()` extracts a window.

| | External-content | Contentless (`content = ''`) |
|---|---|---|
| Text storage | Once, in the base table | Not stored at all |
| `highlight()` / `snippet()` | Work (read via `content_rowid`) | Do not work — no text to read |
| Sync burden | Trigger set must be complete | Effectively append-only; deletes need `contentless_delete = 1` (3.43+) |
| Use when | Default choice | Rowid-lookup-only search over text you keep elsewhere |

## 7. Query patterns

```sql
-- Upsert
INSERT INTO settings (key, value) VALUES (?1, ?2)
ON CONFLICT (key) DO UPDATE SET value = excluded.value;

-- Idempotent insert: DO NOTHING, not INSERT OR IGNORE (which swallows every
-- conflict on the table, not just the one you named)
INSERT INTO seen_ids (id) VALUES (?1)
ON CONFLICT (id) DO NOTHING;

-- RETURNING (3.35+): claim-and-fetch in one statement
UPDATE jobs SET claimed_at = unixepoch()
WHERE id = (SELECT id FROM jobs WHERE claimed_at IS NULL ORDER BY id LIMIT 1)
RETURNING id, payload;

-- Indexable JSON path: generated column + index, not json_extract in every WHERE
ALTER TABLE events ADD COLUMN kind TEXT
    GENERATED ALWAYS AS (json_extract(payload, '$.kind')) VIRTUAL;
CREATE INDEX idx_events_kind ON events (kind);

-- Window functions work as expected
SELECT id, amount,
       sum(amount) OVER (PARTITION BY account_id ORDER BY created_at) AS running_total
FROM ledger;
```

**`LIKE` is case-insensitive for ASCII only.** `'A' LIKE 'a'` is true; `'Ä' LIKE 'ä'` is
false. State the intent explicitly — a `COLLATE NOCASE` column/index for identifier-style
matching, or normalize case in the application for real text.

**Read the plan with `EXPLAIN QUERY PLAN`** — SQLite's plan output is terse enough to
check on every new query shape:

```sql
EXPLAIN QUERY PLAN
SELECT id FROM events WHERE kind = ? ORDER BY created_at DESC LIMIT 20;
-- Want:  SEARCH events USING INDEX idx_events_kind_created (kind=?)
-- Smell: SCAN events                  → index missing or unusable for the predicate
-- Smell: USE TEMP B-TREE FOR ORDER BY → the index serves the filter but not the ordering
```

## 8. Backup & maintenance

- **Never `cp` a live WAL database.** The `-wal` and `-shm` sidecar files are part of the
  database state; copying the main file alone captures a torn snapshot. Consistent copies
  come from the backup API (every driver exposes it) or:

```bash
sqlite3 app.db "VACUUM INTO 'backup-$(date +%F).db'"
```

  The CLI's `.backup` command uses the online backup API and is equally safe; `VACUUM
  INTO` additionally compacts. Either one produces a single self-contained file with no
  sidecars — that file is what goes to backup storage, on a schedule, with a restore
  actually rehearsed.
- Checkpoint deliberately on long-running daemons — the WAL grows until someone folds it
  back (`wal_autocheckpoint` defaults to checkpointing around 1000 pages, but a busy
  reader can starve it — see §2):

```sql
PRAGMA wal_checkpoint(TRUNCATE);   -- fold the WAL into the main file and truncate it
PRAGMA quick_check;                -- fast structural check — fits in a health endpoint
PRAGMA integrity_check;            -- thorough pass, for scheduled maintenance
PRAGMA optimize;                   -- run on connection close; cheap, updates stats
```

- **Never put a SQLite database on a network filesystem** (NFS, SMB). File locking there
  is unreliable, and unreliable locking is how a second writer corrupts the file.

## 9. When to graduate to a server database

Move to Postgres/MySQL when any of these becomes true — not before:

- [ ] Writers in more than one process, or on more than one host
- [ ] Row-level authorization needs (SQLite has no users — access is file access)
- [ ] Replication or high-availability requirements
- [ ] Sustained concurrent OLTP writes (the single writer is now a measured bottleneck)
- [ ] Online schema-change needs beyond `ADD COLUMN`/`RENAME`

**Migration prep note:** clean up loose types *first*. Pre-STRICT tables can hold integers
in TEXT columns and text in INTEGER columns; the target engine's strict types will reject
or mangle them on import. Audit with `typeof()` per column, fix in place, then export.
Target-engine semantics: `postgres.md` / `mysql.md` in this directory.

## 10. Footguns

| Anti-Pattern | Problem | Solution |
|---|---|---|
| Assuming foreign keys are enforced | `foreign_keys` is per-connection and OFF by default — constraints exist in DDL and enforce nothing | Pragma contract at one connection choke point (§3) plus a test asserting it |
| Double-quoted string literals (`WHERE name = "alice"`) | The DQS misfeature: an unrecognized identifier silently becomes a string literal — behaviour depends on compile flags, so verify your build | Single quotes for strings, double quotes for identifiers, everywhere |
| Trusting declared types on non-STRICT tables | Affinity stores almost anything; a TEXT column holding integers compares and sorts per stored type | `STRICT` on all new tables (§5); `typeof()` audits on legacy ones |
| Assuming `:memory:` is shared between connections | Each plain `:memory:` open is a *separate* database; shared-cache mode changes locking semantics in surprising ways | One connection, or a deliberate `file:name?mode=memory&cache=shared` URI — chosen, not stumbled into |
| Leaning on `ATTACH` for scale-out | Attached-database count is limited (default ~10) and cross-database transactions weaken atomicity in some journal-mode combinations | Few, short-lived attachments; if you are sharding across ATTACH, you have outgrown SQLite (§9) |
| Relying on `LIKE`/`GLOB` case behaviour | `LIKE` is case-insensitive for ASCII only; `GLOB` is case-sensitive; non-ASCII silently breaks the assumption | State collation intent per column (`COLLATE NOCASE`) or normalize in the application (§7) |
