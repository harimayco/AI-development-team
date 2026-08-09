# Rust — Embedding SQLite with rusqlite

**Scope:** the Rust side of embedding SQLite — connection policy, migrations,
statement discipline, async access, type mapping, backup. SQL semantics, FTS5 schema
and triggers, and dialect footguns live in the SQL language skill:
`../../sql/references/sqlite.md` — cross-linked both ways; neither restates the other.

**Mental model.** SQLite turns your process into the database server: every guarantee
a server provides — enforced FKs, a migration history, one writer — is now *your*
code's job, per connection, from day one.

---

## 1. Versions

| Crate | Version | Notes |
|---|---|---|
| rusqlite | 0.40 | Features: `bundled`, `modern_sqlite` |
| libsqlite3-sys | transitive | With `bundled`, this crate pins the embedded SQLite version |
| rusqlite_migration | 2.5 | Off-the-shelf migration runner — the §3 alternative |

- **`bundled` compiles SQLite from source** and statically links one known version
  everywhere. The cost: **you own the upgrade cadence** — know which SQLite your
  locked `libsqlite3-sys` embeds and bump for upstream integrity fixes; SQLite
  **3.51.3** fixed a WAL-reset corruption bug, and a copy pinned before it ships
  that bug to every install:

```rust
#[test]
fn the_bundled_sqlite_is_at_least_the_wal_reset_fix_release() {
    // SQLITE_VERSION_NUMBER: major*1_000_000 + minor*1_000 + patch.
    let version = rusqlite::version_number();
    assert!(version >= 3_051_003, "bundled SQLite {version} predates the 3.51.3 WAL fix");
}
```

- **`modern_sqlite` asserts** the runtime SQLite is at least the bindings' target
  version, unlocking newer APIs. Trivially true with `bundled`; a *system* SQLite
  older than that fails the check at startup rather than misbehaving silently.

---

## 2. The connection choke point

Exactly **one** function in the codebase opens connections — every store, read pool,
and diagnostic tool goes through it; `Connection::open` appears nowhere else. A
greppable standard:

```bash
grep -rn 'Connection::open' src/   # every hit outside db/mod.rs is a finding
```

It exists because SQLite's most important settings are **per-connection** and default
wrong — one forgotten pragma on one path is an integrity bug no other path's test catches.

```rust
use std::path::Path;
use rusqlite::Connection;

/// The ONLY constructor for database connections in this crate.
pub fn open_connection(path: &Path) -> rusqlite::Result<Connection> {
    let conn = Connection::open(path)?;
    conn.execute_batch(
        "PRAGMA journal_mode = WAL;
         PRAGMA synchronous = NORMAL;
         PRAGMA foreign_keys = ON;
         PRAGMA busy_timeout = 5000;",
    )?;
    Ok(conn)
}
```

- **`journal_mode = WAL`** — readers stop blocking the writer and vice versa. WAL
  persists in the file; re-setting it on open is harmless.
- **`synchronous = NORMAL`** — the WAL sweet spot. Tradeoff, stated plainly: a power
  cut may lose the last commits, but **never integrity**. `FULL` buys last-commit
  durability at a heavy fsync cost; most embedded workloads take `NORMAL` and say so.
- **`foreign_keys = ON`** — **per-connection and OFF by default.** Every connection,
  no exceptions: one bare open path silently ignores every FK constraint for life.
- **`busy_timeout = 5000`** — wait up to 5 s for a competing lock instead of failing
  instantly with `SQLITE_BUSY` under mundane contention.

Enforced by a test every store's constructor must pass — FKs are what rots silently:

```rust
#[test]
fn every_connection_from_the_constructor_enforces_foreign_keys() {
    let dir = tempfile::tempdir().expect("create tempdir");
    let conn = open_connection(&dir.path().join("app.db")).expect("open");
    let fk: i64 = conn.query_row("PRAGMA foreign_keys", [], |r| r.get(0)).expect("pragma");
    assert_eq!(fk, 1, "constructor must enable foreign_keys on every connection");
}
```

Repeat per store constructor, so none grows a private open path. (Tests may use
`Connection::open_in_memory`; the grep standard targets `src/`.)

---

## 3. Schema migrations via `user_version`

SQLite ships no migration table, but the file header has a 32-bit slot: `PRAGMA
user_version`. That is the whole mechanism — a **forward-only** migration list plus
the version cursor. Never edit a shipped migration; append a new one.

```rust
const MIGRATIONS: &[&str] = &[
    // 1: initial schema
    "CREATE TABLE note (
         id INTEGER PRIMARY KEY, body TEXT NOT NULL, created_at INTEGER NOT NULL
     ) STRICT;",
    // 2: archiving
    "ALTER TABLE note ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;",
];

pub fn migrate(conn: &mut Connection) -> rusqlite::Result<()> {
    migrate_to(conn, MIGRATIONS.len())
}

fn migrate_to(conn: &mut Connection, target: usize) -> rusqlite::Result<()> {
    let current: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    for (index, step) in MIGRATIONS.iter().enumerate().take(target).skip(current as usize) {
        // SQLite DDL is transactional: the step and its version bump land atomically.
        let tx = conn.transaction()?;
        tx.execute_batch(step)?;
        tx.pragma_update(None, "user_version", (index + 1) as i64)?;
        tx.commit()?;
    }
    Ok(())
}
```

Two tests are required, not optional:

```rust
#[test]
fn a_fresh_database_migrates_to_the_latest_version() {
    let mut conn = Connection::open_in_memory().expect("open");
    migrate(&mut conn).expect("migrate");
    let v: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0)).expect("version");
    assert_eq!(v as usize, MIGRATIONS.len());
}

#[test]
fn a_database_upgraded_from_any_intermediate_version_matches_a_fresh_install() {
    let mut fresh = Connection::open_in_memory().expect("open");
    migrate(&mut fresh).expect("migrate fresh");
    let expected = normalized_schema(&fresh);
    for stop in 1..MIGRATIONS.len() {
        let mut conn = Connection::open_in_memory().expect("open");
        migrate_to(&mut conn, stop).expect("migrate to intermediate");
        migrate(&mut conn).expect("finish migration");
        assert_eq!(normalized_schema(&conn), expected, "diverged upgrading from v{stop}");
    }
}

/// sqlite_master, whitespace-normalized, ordered — the comparable schema fingerprint.
fn normalized_schema(conn: &Connection) -> Vec<String> {
    let mut stmt = conn
        .prepare(
            "SELECT type || ' ' || name || ' ' || sql FROM sqlite_master
             WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type, name",
        )
        .expect("prepare");
    stmt.query_map([], |row| row.get::<_, String>(0))
        .expect("query")
        .map(|row| row.expect("row").split_whitespace().collect::<Vec<_>>().join(" "))
        .collect()
}
```

The second test catches a shipped migration edited after the fact, and upgrade paths
that only work in one shot: stop at any version, upgrade later, land on the
fresh-install schema.

- **Off-the-shelf alternative:** `rusqlite_migration` 2.5 implements exactly this
  (`user_version` cursor, transactional steps); adopt it unless the project must own
  the runner.
- **`CREATE TABLE IF NOT EXISTS` is bootstrap, not versioning.** It only creates
  what is missing; the first `ALTER TABLE` proves it — old installs have the old
  shape, new installs the new, and the bootstrap cannot tell which it is looking at.
  Adopt `user_version` from day one, while the list has one entry.

---

## 4. Statement discipline

- **Bind parameters only** — `?1` positional or `:name` named. `format!`-built SQL
  is an injection surface and defeats the statement cache; blocking in review
  (`rust-review.md`). An unavoidable dynamic *identifier* comes from a hardcoded
  allowlist — never from input.
- **`prepare_cached` on hot paths** — compiles once per connection, reuses by SQL text.
- **Multi-row writes go inside an explicit transaction.** Autocommit is one implicit
  transaction — and one fsync — per statement: the classic 100x slowdown.

```rust
// BEFORE — autocommit: one fsync per row.
for event in &events {
    conn.execute(
        "INSERT INTO event (kind, payload, at) VALUES (?1, ?2, ?3)",
        (&event.kind, &event.payload, event.at),
    )?;
}

// AFTER — one transaction, one cached statement: two orders of magnitude faster.
let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
{
    let mut insert = tx.prepare_cached(
        "INSERT INTO event (kind, payload, at) VALUES (?1, ?2, ?3)",
    )?;
    for event in &events {
        insert.execute((&event.kind, &event.payload, event.at))?;
    }
}
tx.commit()?;
```

- **`BEGIN IMMEDIATE` for write transactions.** A deferred transaction starts as a
  reader and upgrades at its first write; if another writer got there first, the
  upgrade fails with a `SQLITE_BUSY` the busy handler cannot safely wait out (the read
  snapshot is stale). IMMEDIATE takes the write lock up front, honours `busy_timeout`,
  and fails fast on busy instead of deadlocking on a lock upgrade.

---

## 5. Access architecture — apply the dedicated-thread actor

`rusqlite::Connection` is `Send` but **`!Sync`**: it may move to a thread, never be
shared between tasks. The access pattern is not SQLite-specific — it is the
dedicated-thread actor in `async-tokio.md` §5 (one OS thread owns the resource;
callers send commands over a channel, get replies over oneshot, with a ready
handshake at startup). Apply that template verbatim; the SQLite decisions on top:

- **One writer connection, owned by one actor.** SQLite allows a single writer at a
  time anyway; funnelling writes through one connection turns lock contention into
  orderly queueing, and `BEGIN IMMEDIATE` conflicts disappear by construction.
- **An optional read pool** for heavy read concurrency: a few extra read-only
  connections (from the §2 choke point, via `OpenFlags`), each on its own actor
  thread — WAL reads run concurrently with the writer. Add only when profiling
  shows reads queueing behind writes.
- **Never share one `Connection` across tasks.** A tokio `Mutex` around it
  type-checks, then serializes every query through an async lock while blocking a
  runtime thread for each one — the worst of both worlds.

---

## 6. Type mapping

- **STRICT tables for new schemas.** Without STRICT, type affinity stores `'banana'`
  in an INTEGER column; STRICT makes a misbound parameter an immediate error.
  (Needs SQLite 3.37+ — always true with `bundled`.)
- **Choose ONE datetime encoding and write it down.** Default: `INTEGER` unix seconds
  (cheap, indexable). `TEXT` RFC 3339 when human-readable dumps matter more. Never
  both in one schema; never a third.
- **bool is `INTEGER` 0/1** — rusqlite maps `bool` natively. **Blobs are `Vec<u8>`**
  — no base64-in-TEXT detours.
- **Domain newtypes cross the boundary, not raw strings.** Implement
  `FromSql`/`ToSql` so rows produce domain types directly:

```rust
use rusqlite::types::{FromSql, FromSqlError, FromSqlResult, ToSql, ToSqlOutput, ValueRef};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct NoteId(pub i64);

impl ToSql for NoteId {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> { Ok(ToSqlOutput::from(self.0)) }
}

impl FromSql for NoteId {
    fn column_result(value: ValueRef<'_>) -> FromSqlResult<Self> { value.as_i64().map(NoteId) }
}
```

For validated TEXT newtypes, run the domain parser inside `column_result`, mapping
failure to `FromSqlError::Other` — a corrupt row fails the query that touched it
instead of leaking an invalid value into domain code.

---

## 7. FTS5 from Rust — sanitize MATCH input

Schema, triggers, and tokenizers live in `../../sql/references/sqlite.md`. The Rust
rule: **never pass user input to `MATCH` raw.** FTS5 parses it as an expression
language — `"` opens phrases, `-` excludes, `NEAR(` is a function, `column:` filters —
so raw input is query injection, usually surfacing as `fts5: syntax error` on
innocent punctuation. Allowlist term characters, quote each term, join explicitly:

```rust
/// Reduce arbitrary user input to a safe FTS5 MATCH expression. Splitting on
/// non-alphanumerics guarantees no metacharacter survives, so quoting is sound.
pub fn fts5_query(input: &str) -> Option<String> {
    let terms: Vec<String> = input
        .split(|c: char| !c.is_alphanumeric())
        .filter(|term| !term.is_empty())
        .map(|term| format!("\"{term}\""))
        .collect();
    if terms.is_empty() { None } else { Some(terms.join(" AND ")) }
}
```

For search-as-you-type, *your* code may append `*` after the final quote (`"term"*`
is FTS5 prefix syntax) — never accepted from input. Unit tests use hostile inputs:

```rust
#[test]
fn hostile_match_metacharacters_are_reduced_to_quoted_terms() {
    assert_eq!(
        fts5_query(r#"" OR body:secret NEAR(admin"#).as_deref(),
        Some(r#""OR" AND "body" AND "secret" AND "NEAR" AND "admin""#),
    );
    assert_eq!(fts5_query("-launch").as_deref(), Some("\"launch\""));
}

#[test]
fn punctuation_only_input_yields_no_query_instead_of_a_syntax_error() {
    assert_eq!(fts5_query("\"\"()-:*"), None);
}
```

---

## 8. Backup & health

- **Never file-copy a live WAL database** — committed data sits in the `-wal` sidecar
  until checkpointed; the copy is torn. Consistent copies come from the database:

```rust
// Consistent online snapshot; also compacts. For incremental copies with
// progress, use the online backup API (rusqlite::backup::Backup, feature "backup").
conn.execute("VACUUM INTO ?1", [dest.to_string_lossy()])?;
```

- **`PRAGMA wal_checkpoint(TRUNCATE)` after large churn** — bulk imports and mass
  deletes grow the `-wal` file; a truncating checkpoint at a quiet moment reclaims it.
- **`PRAGMA integrity_check` belongs in diagnostics** (health endpoint, `doctor`
  subcommand): `query_row` its first row; anything but `"ok"` is surfaced, never swallowed.
- **`PRAGMA optimize` on close** — in the actor's shutdown path before the
  connection drops. Cheap; keeps planner statistics current.

---

## 9. Checklist

- [ ] `Connection::open` appears only in the choke-point constructor (§2 grep clean).
- [ ] Pragma block on every connection; the `foreign_keys = 1` test covers each store.
- [ ] `user_version` migrations from day one; both §3 tests present and green.
- [ ] No `format!`-built SQL; bind parameters everywhere; `prepare_cached` on hot paths.
- [ ] Multi-row writes inside `BEGIN IMMEDIATE` transactions.
- [ ] Access via the dedicated-thread actor (`async-tokio.md` §5); one writer.
- [ ] New tables STRICT; one documented datetime encoding; newtypes implement `FromSql`/`ToSql`.
- [ ] User search input passes the FTS5 sanitiser, with hostile-input tests.
- [ ] Backups via `VACUUM INTO` / backup API; `integrity_check` wired into diagnostics.
- [ ] Bundled SQLite version known and asserted ≥ the latest upstream integrity fix (§1).

---

## 10. Anti-patterns

| Anti-Pattern | Problem | Solution |
|---|---|---|
| `Connection` inside a tokio `Mutex` | Blocks a runtime thread per query; serializes all access through an async lock | Dedicated-thread actor from `async-tokio.md` §5 |
| Assuming `foreign_keys` is global or persistent | Per-connection, OFF by default — one bare open path ignores every FK silently | Choke-point constructor + the §2 pragma test |
| `CREATE TABLE IF NOT EXISTS` as versioning | Installs drift apart at the first `ALTER`; no way to tell which shape a DB has | `user_version` migrations from the first table (§3) |
| `format!`-built SQL | Injection surface; defeats the statement cache; blocking in review | Bind parameters (`?1` / `:name`), allowlisted identifiers |
| Copying the `.db` file for backup | `-wal`/`-shm` sidecars hold committed data; the copy is torn | `VACUUM INTO` or the online backup API (§8) |
| One connection shared across tasks | `!Sync` — and even wrapped, every caller queues behind every other | One writer actor; optional read-only pool (§5) |
