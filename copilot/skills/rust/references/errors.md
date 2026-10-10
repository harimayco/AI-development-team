# Rust — Error Doctrine and the Failure-Table Method

Failure paths are enumerated **before** code is written, encoded as types the compiler
tracks, and mapped to the wire exactly once at the boundary. The centerpiece is the
failure table (§4): a small artifact that turns "did we handle the sad paths?" from a
review-time archaeology exercise into a diff between a table and a test list.

**Mental model.** An error type is an API contract. Its variants are the failure modes
a caller can *act on differently* — no more (a variant nobody matches is noise), no
less (a merged variant forces string-sniffing). Everything else here follows from
taking that contract as seriously as the function signature.

Lint enforcement for the rules below lives in `toolchain-and-lints.md`; how a reviewer
checks them is in `rust-review.md`.

---

## 1. The doctrine table

| Crate kind | Error type | Rules |
|---|---|---|
| Library / core crate | `thiserror` (v2) enum **per module** | Variants = failure modes callers can act on. Carry the data the caller needs (`path`, `id`, `limit`) — not prose. |
| Binary / shell crate | `anyhow::Result` + `.context()` | Context added at **every** fallible boundary, so the chain reads as a story: what was being attempted, with which inputs. |
| Library **public API** | Never `anyhow` | `anyhow::Error` is opaque — callers cannot match on it, so they parse `Display` strings. Publishing it as your API outsources your contract to your prose. |
| Anywhere | Never a `Msg(String)` enum | A one-variant enum wrapping a string is `anyhow` without the ergonomics: unmatched *and* unstructured. If you cannot name the failure modes yet, you are not ready to implement (§4). |

The boundary between the two worlds is mechanical: domain errors flow **up** through
library code as enums; the binary converts to `anyhow` (or to a wire error, §3) at the
outermost layer and never before.

---

## 2. Error enums as API contracts — exhaustive on purpose

The usual advice says: put `#[non_exhaustive]` on error enums so adding a variant is
not a breaking change. Apply that **only to published libraries** — crates with
external consumers you cannot recompile.

**Workspace-internal crates stay exhaustive, deliberately.** This inverts the usual
advice, so the reasoning belongs in writing: inside a workspace, "adding a failure
variant breaks every caller's `match`" is not a cost — it is the entire mechanism.
A new failure mode *should* stop the build until every call site decides what it does
about it. With `#[non_exhaustive]`, every internal caller is forced to write a
wildcard arm, and the new failure silently falls into whatever that arm does — usually
a generic 500 and a log line nobody reads. You have paid the enum tax and bought
`anyhow` behavior.

Rule of thumb: `#[non_exhaustive]` marks the boundary where you stop trusting the
compiler to reach your callers. Do not draw that boundary inside your own workspace.

---

## 3. When hand-rolled beats thiserror — stable wire codes

Errors that serialize to a wire protocol with **stable numeric codes** (JSON-RPC and
kin) need guarantees `thiserror` does not give: codes never renumber, every code is
registered in one place, and serialization is proven for all of them. Hand-roll this
layer.

The pattern: **all wire codes as `pub const` in one module**, application-specific
JSON-RPC codes confined to the reserved `-32000..=-32099` implementation-defined
range, every domain error mapped to a named code **at the handler boundary**, and a
round-trip test that enumerates **all** constants. Declaring the constants through a
registry macro means a new code cannot exist outside the registry — so the test covers
it the moment it is declared, and forgetting is not possible, only refusing.

```rust
// wire/codes.rs — every wire error code lives here and only here.
macro_rules! wire_codes {
    ( $( $name:ident = $value:literal ; )+ ) => {
        $( pub const $name: i32 = $value; )+

        /// Every wire code paired with its name. Tests iterate this registry,
        /// so a new code is covered the moment it is declared.
        pub const ALL_CODES: &[(i32, &str)] = &[
            $( ($name, stringify!($name)), )+
        ];
    };
}

wire_codes! {
    // JSON-RPC 2.0 standard codes — fixed by the spec, never renumber.
    PARSE_ERROR = -32700;
    INVALID_REQUEST = -32600;
    METHOD_NOT_FOUND = -32601;
    INVALID_PARAMS = -32602;
    INTERNAL_ERROR = -32603;

    // Application codes — confined to the reserved -32000..=-32099 range.
    NOT_READY = -32000;
    RESOURCE_EXHAUSTED = -32001;
    CANCELLED = -32002;
    NOT_FOUND = -32003;
    AUTH_REQUIRED = -32004;
    FORBIDDEN = -32005;
}
```

```rust
// wire/error.rs — the one wire error shape, plus the one mapping point.
use serde::Serialize;

use crate::wire::codes;

#[derive(Debug, Serialize)]
pub struct ApiError {
    pub code: i32,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<serde_json::Value>,
}

impl ApiError {
    pub fn new(code: i32, message: impl Into<String>) -> Self {
        ApiError { code, message: message.into(), data: None }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("no record with id {id}")]
    NotFound { id: String },
    #[error("store is at capacity")]
    Busy,
    #[error("store is closed")]
    Closed,
    #[error("database failure: {0}")]
    Database(String),
}

impl From<StoreError> for ApiError {
    fn from(err: StoreError) -> Self {
        // Exhaustive on purpose (§2): a new StoreError variant fails to compile
        // until someone chooses its wire code. That is the point.
        match err {
            StoreError::NotFound { id } => {
                ApiError::new(codes::NOT_FOUND, format!("no record with id {id}"))
            }
            StoreError::Busy => {
                ApiError::new(codes::RESOURCE_EXHAUSTED, "store is at capacity; retry later")
            }
            StoreError::Closed => ApiError::new(codes::NOT_READY, "store is not initialized"),
            StoreError::Database(detail) => {
                ApiError::new(codes::INTERNAL_ERROR, format!("storage failure: {detail}"))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_wire_code_serializes_and_stays_in_range() {
        for &(code, name) in codes::ALL_CODES {
            let json = serde_json::to_value(ApiError::new(code, name)).expect("serializes");
            assert_eq!(json["code"], code, "{name} must round-trip its code");

            let standard = (-32700..=-32600).contains(&code);
            let application = (-32099..=-32000).contains(&code);
            assert!(standard || application, "{name} = {code} is outside both ranges");
        }

        let mut seen: Vec<i32> = codes::ALL_CODES.iter().map(|&(c, _)| c).collect();
        seen.sort_unstable();
        seen.dedup();
        assert_eq!(seen.len(), codes::ALL_CODES.len(), "duplicate wire code assigned");
    }
}
```

The handler boundary is the **only** place a domain error becomes a wire error. Deeper
layers return their enums and know nothing about codes; clients match numerically and
never parse messages. Cancellation gets one named code used everywhere — the async
side of that contract is in `async-tokio.md`.

---

## 4. The failure-table method

This is the anti-"bugs found at review" mechanism. **Before implementing any fallible
function**, fill in a failure table:

| Column | Meaning |
|---|---|
| failure | What can go wrong, in domain terms |
| detected by | The concrete check or call that observes it |
| mapped to | The error variant (and wire code, if it crosses the boundary) |
| caller action | What the caller does about it — retry, report, skip, halt |
| test name | The test that proves the row, named after it |

The table lives in the dev doc or ticket. Test names are taken from its rows. The
reviewer's job (per `rust-review.md`) is then a *diff*: every row has a shipped test
with that name, every variant appears in some row. No archaeology.

### Worked example: `ingest_file(path) -> Result<IngestReport, IngestError>`

| # | failure | detected by | mapped to | caller action | test name |
|---|---|---|---|---|---|
| 1 | source file missing | `File::open` → `ErrorKind::NotFound` | `IngestError::SourceMissing { path }` | report; no retry | `ingest_missing_file_maps_to_source_missing` |
| 2 | permission denied | `File::open` → `ErrorKind::PermissionDenied` | `IngestError::AccessDenied { path }` | report; suggest permissions fix; no retry | `ingest_permission_denied_maps_to_access_denied` |
| 3 | malformed content | header/record parse failure | `IngestError::Malformed { line, reason }` | skip file, continue the batch | `ingest_malformed_header_reports_line` |
| 4 | oversize input | size check **before** reading | `IngestError::TooLarge { size, limit }` | reject; operator raises the limit deliberately | `ingest_oversize_rejected_before_read` |
| 5 | partial write (crash mid-write) | temp-file + rename protocol; orphan `.tmp` found on startup | `IngestError::IncompleteWrite { tmp_path }` | delete orphan, re-ingest | `ingest_orphan_tmp_detected_and_reingested` |
| 6 | concurrent modification | mtime/length re-check after read | `IngestError::SourceChanged { path }` | retry once, then surface | `ingest_source_changed_retries_once` |

The enum falls straight out of the table:

```rust
use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum IngestError {
    #[error("source file not found: {path}")]
    SourceMissing { path: PathBuf },
    #[error("permission denied reading {path}")]
    AccessDenied { path: PathBuf },
    #[error("malformed content at line {line}: {reason}")]
    Malformed { line: usize, reason: String },
    #[error("input is {size} bytes; limit is {limit}")]
    TooLarge { size: u64, limit: u64 },
    #[error("incomplete previous write found: {tmp_path}")]
    IncompleteWrite { tmp_path: PathBuf },
    #[error("source changed while reading: {path}")]
    SourceChanged { path: PathBuf },
    #[error("io failure on {path}: {source}")]
    Io { path: PathBuf, #[source] source: std::io::Error },
}
```

Rules that make the table earn its keep:

- **"Detected by" must name a concrete check.** If you cannot say what observes the
  failure, the row is a wish, not a plan — rows 4–6 exist precisely because someone
  had to invent the size pre-check, the rename protocol, and the mtime re-check.
- **Variants merge only when caller actions match.** Rows 1 and 2 stay separate
  because "fix permissions" and "wrong path" are different user actions. A row whose
  action equals another's *may* share its variant.
- **Foreign non-exhaustive enums get a wildcard; yours do not.** Mapping
  `io::ErrorKind` requires `_ =>` (it is non-exhaustive upstream) — route the
  remainder into the data-carrying `Io` variant, never into a bare catch-all.

---

## 5. Panic policy

- **A panic is a bug, never control flow.** No `panic!` for input validation, no
  catch-unwind as a handler strategy.
- **`unreachable!("why this cannot happen")` must carry the argument.** A bare
  `unreachable!()` asserts nothing; the message is the proof the reviewer checks —
  "the loop returns on the final attempt", not "shouldn't get here".
- **`unwrap`/`expect` are banned in production code** by the workspace lints in
  `toolchain-and-lints.md`; tests are exempt (an `expect` in a test is an assertion
  with a message, which is exactly what tests are for).
- **Startup fallibility flows through `anyhow` context (§7), not `.expect()`.**
  "called `Result::unwrap()` on an `Err` value: Os { code: 2 }" at boot is a support
  ticket; "reading config file /etc/app/config.toml: No such file or directory" is a
  fix.
- **`let _ = fallible()` on a `Result` is a finding.** Mark important result types
  `#[must_use]` so dropping them warns. The one sanctioned exception — an actor's
  reply send, where the only failure is "caller stopped listening" — carries a comment
  saying so (`async-tokio.md`).

---

## 6. Logging errors

- **Log once, at the handling site.** Propagation hops add *context* to the error;
  they do not add log lines. If a failure produces five stack-hopping log entries,
  dashboards over-count it five-fold and the real frequency of everything is hidden.
- **Structured fields over string interpolation:**

```rust
tracing::warn!(error = %err, path = %path.display(), "ingest failed");
```

  Fields survive as queryable data (`error`, `path`); an interpolated sentence
  survives as grep fodder. The event message stays constant so occurrences aggregate.
- **`Display` of secrets is forbidden** — tokens, keys, and credentials never reach a
  log line, an error message, or an `ApiError.data` payload. Wrap them in newtypes
  whose `Debug`/`Display` redact; the full rules are in `security.md`.

---

## 7. Templates

**Module error enum** (library side — one per module, data-carrying):

```rust
use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum SnapshotError {
    #[error("snapshot directory does not exist: {dir}")]
    DirMissing { dir: PathBuf },
    #[error("snapshot {name} is corrupt: {reason}")]
    Corrupt { name: String, reason: String },
    #[error("io failure on {path}: {source}")]
    Io { path: PathBuf, #[source] source: std::io::Error },
}
```

**Binary main** (shell side — `anyhow` with context at every fallible boundary):

```rust
use anyhow::Context;

fn main() -> anyhow::Result<()> {
    let config_path = std::env::var("APP_CONFIG").context("APP_CONFIG is not set")?;
    let raw = std::fs::read_to_string(&config_path)
        .with_context(|| format!("reading config file {config_path}"))?;
    let config: Config = toml::from_str(&raw)
        .with_context(|| format!("parsing {config_path} as TOML"))?;
    run(config).context("service exited with an error")
}
```

**Boundary mapping function** (the only place domain becomes wire — §3):

```rust
pub async fn get_snapshot(store: &SnapshotStore, name: &str) -> Result<Snapshot, ApiError> {
    store.load(name).await.map_err(ApiError::from)
}
```

---

## 8. Checklist

- [ ] Failure table written and in the dev doc/ticket **before** implementation (§4).
- [ ] Every table row has a shipped test with the row's name; reviewer diffed them (§4).
- [ ] Library crates expose `thiserror` enums; no `anyhow` in any public signature (§1).
- [ ] Workspace-internal enums exhaustive; `#[non_exhaustive]` only on published crates (§2).
- [ ] All wire codes in the single registry; registry test green; app codes within `-32000..=-32099` (§3).
- [ ] Domain → wire mapping happens at the handler boundary only (§3, §7).
- [ ] No `unwrap`/`expect` outside tests; every `unreachable!` argues its case (§5).
- [ ] Each failure logged exactly once, with structured fields, secrets redacted (§6).

## Anti-patterns

| Anti-Pattern | Problem | Solution |
|---|---|---|
| Swallowing an error into `Option` (`.ok()`) | The cause is destroyed at the scene; "why is this None?" becomes unanswerable in production | Keep the `Result` and map the error; convert to `Option` only when absence is genuinely not a failure |
| `Box<dyn Error>` in a core crate | Callers cannot match; type erasure at the layer whose whole job is to distinguish failures | `thiserror` enum per module (§1) |
| `.to_string()` on an error, then wrapping the string | Source chain, backtrace, and matchability all lost at the first hop | Preserve the source: `#[source]`/`#[from]` in enums, `.context()` in binaries (§7) |
| Catch-all `Internal` variant absorbing new failures | New failure modes route silently to the generic path; the enum lies about coverage | Exhaustive mapping (§2); the compiler must force a decision per new variant (§3) |
| Logging at every propagation hop | One failure counts as five; alert thresholds and dashboards are fiction | Log once at the handling site; hops add context only (§6) |
| Matching on error strings (`e.to_string().contains(...)`) | Breaks on any message edit; messages are for humans, not dispatch | Match on variants or wire codes — that is what §3 exists for |
