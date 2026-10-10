# Rust — Review Pass (loaded by /rev)

Extends the reviewer's process for Rust diffs. The developer-side doctrine is this
skill's other references (`errors.md`, `async-tokio.md`, `testing.md`, `security.md`,
`sqlite-rusqlite.md`, `toolchain-and-lints.md`, `dependencies-supply-chain.md`) — this
file is the reviewer's teeth, and the developer's self-review before requesting one.

## 1. Trigger

Use when /rev is reviewing changes to:

- `.rs` source files
- `Cargo.toml` / `Cargo.lock`
- `deny.toml`, `rustfmt.toml`
- CI workflow files that run the Rust gates (fmt/clippy/test steps)

## 2. Pass 0 — commands requiring no project config

Run these before reading a line of the diff. None of them require the repo to have
configured anything; they are the reviewer's own instruments and work on any Rust
workspace as checked out. Two are cargo subcommands installed once on the reviewer's
side:

```bash
cargo install cargo-audit cargo-deny --locked   # reviewer-side tools, one-time
```

### Format

```bash
cargo fmt --check
```

Honours the repo's `rustfmt.toml` if present. The **absence** of `rustfmt.toml` in an
application repo is itself a MINOR finding — format policy should be pinned in the
repo, not inherited from whichever toolchain runs last.

### Reviewer-imposed lints (work even when the repo has no `[lints]` table)

```bash
cargo clippy --all-targets --all-features --locked -- \
  -D warnings \
  -W clippy::unwrap_used \
  -W clippy::expect_used \
  -W clippy::panic \
  -W clippy::dbg_macro \
  -W clippy::todo \
  -W clippy::await_holding_lock \
  -W clippy::undocumented_unsafe_blocks
```

These are imposed from the CLI precisely so a repo cannot dodge them by never opting
in; a repo that already carries the `toolchain-and-lints.md` table will simply come
back clean. Triage note: `unwrap`/`expect` hits inside `#[cfg(test)]` regions are
exempt (`testing.md` §10 makes panics in tests legitimate) — everything else gets a
look. `--locked` keeps the run honest: if it fails, the lockfile does not match the
manifest, which is itself a finding.

### Advisories and dependency policy

```bash
cargo audit          # zero config — needs only Cargo.lock
cargo deny check     # if deny.toml exists
```

`cargo audit` runs against any lockfile with no setup. If `deny.toml` exists, run
`cargo deny check` and treat every `ignore` entry without a written reason as a
finding. If `deny.toml` does **not** exist in a daemon/application repo: **MAJOR
finding** — `dependencies-supply-chain.md` owns the policy it should contain.

### Grep sweeps — every hit needs a check or is a finding

The contract for each pattern: the reviewer looks at **every** hit; a hit is either
verified harmless (and the review says why) or it is a finding. There is no third
state — an untriaged hit is an unfinished review.

**unwrap/expect in production paths** (triage out test modules):

```bash
grep -rn --include='*.rs' -E '\.(unwrap|expect)\(' src/ crates/*/src/
```

Legitimate survivors are rare and always documented: a mutex whose poisoning is
unrecoverable by design, a static regex proven valid by a test. "It can't fail here"
without evidence is a finding (`errors.md`).

**Unsafe sites** (locator only — the deciding check is the
`clippy::undocumented_unsafe_blocks` lint already in the Pass-0 command):

```bash
grep -rn --include='*.rs' -e 'unsafe fn' -e 'unsafe {' src/ crates/*/src/
```

Every `unsafe` block needs a `// SAFETY:` comment stating the invariant upheld
(`security.md` §7). The grep cannot tell a documented block from an undocumented one
(and hits `unsafe` inside strings) — the clippy lint makes that distinction
mechanically. Use the grep's hit list to spot-read each site's `// SAFETY:` argument
for whether the stated invariant actually holds; the lint only checks that a comment
exists.

**Env mutation** (unsafe in edition 2024; `testing.md` §5 has the only sanctioned shape):

```bash
grep -rn --include='*.rs' 'set_var\|remove_var'
```

Hits outside a guarded, current-thread test helper are findings — production code
reads config once at the main boundary and injects it; it never mutates env.

**Secret-shaped values near logging/format calls:**

```bash
grep -rniE '(token|secret|api_key|password)' --include='*.rs' src/ \
  | grep -E 'debug!|info!|warn!|error!|println!|format!'
```

The second stage narrows to same-line flows; a secret bound on one line and logged
three lines later still needs the manual trace. The grep finds the neighborhoods —
the reviewer walks them. Any secret reaching a log or `format!` is BLOCKING
(`security.md` §2).

**Silently dropped spawns:**

```bash
grep -rn --include='*.rs' 'tokio::spawn'
```

Each hit's `JoinHandle` must be awaited, stored for shutdown, or documented as
deliberate fire-and-forget. A dropped handle for must-complete work is a task whose
failure nobody will ever see (`async-tokio.md`).

### Reporting Pass 0

The review report lists each command/pattern with its hit count and the triage
outcome. "Grep ran clean" and "12 hits, all in test modules, exempt" are both valid
outcomes; silence about a sweep is not.

## 3. Reviewer checklist

Each item is a verifiable claim about the diff — check it, or cite the line that
breaks it. Findings name the file, the line, and the reference section they violate.

### Errors

- [ ] Every new fallible public fn has a failure table in the ticket or matching error-variant tests (`errors.md` §4, `testing.md` §1).
- [ ] No new `unwrap`/`expect`/`panic!` on a production path (Pass 0 grep triaged to zero).
- [ ] Library-crate APIs return concrete error enums, not `Box<dyn Error>`/anyhow.
- [ ] Errors gain context where they are handled, not stringly re-wrapped at every level.
- [ ] Wire error codes added in this diff appear in the exhaustive round-trip test.

### Async

- [ ] No lock held across an await point (`await_holding_lock` clean, plus a manual scan of new `.await`s near guards).
- [ ] Every `tokio::spawn` JoinHandle is awaited, stored, or documented fire-and-forget (Pass 0 grep).
- [ ] External awaits (network, subprocess, IPC) carry timeouts (`async-tokio.md`).
- [ ] New channels are bounded, and the chosen capacity has a stated reason.
- [ ] Cancellation-safe: a dropped future leaves no half-applied state behind.

### State & types

- [ ] No new `_ =>` arm on a domain enum — a new variant must break compilation, not vanish into a default.
- [ ] No bool+Option pairs co-varying where an enum names the legal states (`architecture.md` §5).
- [ ] Domain newtypes cross module and DB boundaries, not raw `String`/`i64` (`sqlite-rusqlite.md` §6).
- [ ] Any new `unsafe` sits inside a designated island and its SAFETY comment states the invariant (`security.md` §7).
- [ ] SQL uses bind parameters only, and connections come from the single choke-point constructor (`sqlite-rusqlite.md` §2).

### Tests

- [ ] The dev doc's test list matches the shipped tests; every gap has an explanation (`testing.md` §1).
- [ ] Test names are behaviour sentences; `cargo test -- --list` over the new tests reads as a spec.
- [ ] Timing tests use `start_paused`; no real sleeps in test code.
- [ ] Provider fixtures are scripted and refuse to run with a production credential present (`testing.md` §6).
- [ ] Changed behaviour has a test that fails on the old code — not one written to pass on the new.

### Dependencies

- [ ] New dependencies pass the new-dependency checklist and a `cargo deny check` run (`dependencies-supply-chain.md`).
- [ ] `Cargo.lock` changes match `Cargo.toml` intent — no surprise major bumps riding along.
- [ ] No new git/path dependencies without a stated reason.

### Security

- [ ] No secret-shaped value flows into logs, errors, or responses (Pass 0 grep traced).
- [ ] Secret comparisons are constant-time; no `==` on secret material (`security.md` §3).
- [ ] New listening sockets bind loopback unless the ticket states otherwise (`security.md` §4).
- [ ] Externally influenced paths go through the safe-join guard (`security.md` §5).

### Finding format

State the claim broken, the evidence, and the severity — not an impression:

```text
BAD:  "error handling could be better in store.rs"
GOOD: "BLOCKING — src/store.rs:141 `.unwrap()` on a rusqlite query in a
       production path; map to StoreError instead (errors.md §2)"
```

## 4. Severity mapping (fixed — do not downgrade)

Severity is set by this table, not by negotiation. A finding may be *disputed on the
facts* (the hit is in a test module; the socket is loopback after all) — but a
confirmed finding keeps its listed severity.

### BLOCKING

- `unwrap`/`expect`/`panic!` in a production path
- String-built SQL
- Secrets in logs or argv
- `==` on secret material
- Lock held across an await point
- Undocumented or unsound `unsafe`
- Dropped JoinHandle for must-complete work
- Advisory ignore (`cargo audit`/`cargo deny`) without a written reason
- A new listening socket not loopback-bound without a stated reason

### MAJOR

- Unbounded channel
- External await without a timeout
- Real sleeps in tests
- bool+Option co-varying state where an enum belongs
- Missing `[lints]` table or `deny.toml` in an application repo
- TODO committed
- Bootstrap DDL (`CREATE TABLE IF NOT EXISTS`) standing in for migrations

### MINOR

- Naming that hides role or intent
- Narration comments restating the next line
- Absent `rustfmt.toml` (format policy unpinned)
- Pedantic-lint noise — report once; do not let it bury the real findings

On a follow-up round, re-run Pass 0 and review **the fix and the prior findings** —
not the whole PR again. The severities above still apply to anything the fix itself
introduced.
