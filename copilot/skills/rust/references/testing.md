# Rust — Testing Doctrine: Behaviour Sentences, Deterministic Fixtures, Paused Time

How Rust tests are designed, named, placed, and kept deterministic. The failure modes a
test list must cover come from `errors.md` §4; the reviewer's enforcement of everything
below lives in `rust-review.md`; lint exemptions that make test-only `unwrap` free are
in `toolchain-and-lints.md`.

**Mental model.** A test suite is a spec that happens to execute. If reading the test
names does not tell a stranger what the system promises, the suite is a pile of
regression tripwires, not a spec — and every flaky, time-dependent, or
network-touching test converts CI from a verifier into a rumor.

---

## 1. Test cases are designed before code

From the acceptance criteria **plus the failure tables** (`errors.md` §4), write the
test LIST first — names only, no bodies — into the dev doc. Then TDD: pick a name,
write the failing test, make it pass, refactor. The list is the contract; the reviewer
compares it to the shipped tests and asks about every gap (`rust-review.md` §3).

Why names-first: it forces the failure cases to be enumerated while they are cheap.
Test suites written after the code cover what the code does; test lists written before
it cover what the ticket demands — including the negative criteria that otherwise
never get a test.

---

## 2. Names are behaviour sentences

Sentence-style snake_case, each name a complete claim about observable behaviour:

```text
a_request_cancelled_before_dispatch_is_never_sent
an_invalid_utf8_chunk_degrades_to_replacement_without_stalling_the_stream
a_note_saved_twice_keeps_a_single_row
an_expired_session_token_is_rejected_before_the_handler_runs
```

Rules:

- **No `test_` prefix** — the attribute already says it is a test.
- **No method names in the name** — `save_returns_ok` couples the test to an
  identifier and claims nothing; rename the method and the sentence is a lie.
- **No "works" / "correctly" / "properly"** — a sentence that cannot be false is not
  a claim.
- The subject is a scenario (`a_request_cancelled_before_dispatch`), the predicate an
  observable outcome (`is_never_sent`).

**Measurable acceptance:** `cargo test -- --list` reads as the spec. Run it; if the
output is not a coherent description of the system's promises, the names failed.

---

## 3. Placement

| Tier | Where | What |
|---|---|---|
| Unit | `#[cfg(test)] mod tests` at the bottom of the file | Private functions, invariants, error variants — anything needing module access |
| Integration | `tests/` directory | The crate's **public API only**, as a consumer would call it |
| Doctest | `///` examples on public library APIs | The docs' claim that the example compiles and runs |
| E2E | `tests/` driving the real binary (§7) | The user-visible behaviour of the shipped artifact |

Each file under `tests/` compiles as its own crate — group related integration tests
in one file rather than paying link time per scenario. Doctests are not decoration:
they are the only mechanism that fails CI when the documentation rots.

---

## 4. Async and time — paused clocks, never sleeps

ALL timing logic is tested under `#[tokio::test(start_paused = true)]` with
`tokio::time::advance`. Paused time auto-advances when every task is idle, so a 30 s
timeout test completes in microseconds of wall clock — deterministically:

```rust
use std::time::Duration;

#[tokio::test(start_paused = true)]
async fn a_provider_call_still_pending_at_the_deadline_fails_with_timeout() {
    let pending = std::future::pending::<()>();
    let outcome = tokio::time::timeout(Duration::from_secs(30), pending).await;
    assert!(outcome.is_err());
}
```

Explicit `advance` pins down before/after boundaries — the debounce shape:

```rust
use std::time::Duration;

#[tokio::test(start_paused = true)]
async fn a_burst_of_edits_triggers_exactly_one_save_after_the_quiet_period() {
    // Unit under test: Debouncer::new(quiet) -> (handle, receiver of fired values).
    let (debouncer, mut fired) = Debouncer::new(Duration::from_millis(300));

    debouncer.submit("a").await;
    debouncer.submit("ab").await;

    tokio::time::advance(Duration::from_millis(299)).await;
    assert!(fired.try_recv().is_err(), "fired inside the quiet period");

    tokio::time::advance(Duration::from_millis(2)).await;
    assert_eq!(fired.recv().await.as_deref(), Some("ab"));
}
```

Rules:

- **Any real sleep >100ms in test code is a review finding** (`rust-review.md` §4,
  MAJOR). A sleep-synchronized test is a race with a timer — it passes on your machine
  and flakes on the loaded CI runner.
- `flavor = "current_thread"` (the `#[tokio::test]` default — do not override it to
  `multi_thread`) whenever the test touches globals or the environment (§5).
- `start_paused` only works on the current-thread runtime; that constraint and the
  globals rule point the same direction.

---

## 5. Environment hygiene (edition 2024)

`std::env::set_var` and `remove_var` are **unsafe in edition 2024** — mutating the
environment while another thread reads it is undefined behaviour on POSIX. The design
rule that removes the problem outranks any guard:

> **Configuration is read ONCE at the main boundary into a struct and injected.**
> Code deep in the tree never reads env. Then no test ever needs to mutate env to
> steer behaviour — it constructs the config struct directly.

If a test must touch env anyway (exercising the boundary parser itself): current-thread
flavor, plus a save/restore guard that restores on panic:

```rust
/// Restores an environment variable on drop — including on panic.
struct EnvGuard {
    key: &'static str,
    previous: Option<String>,
}

impl EnvGuard {
    fn set(key: &'static str, value: &str) -> Self {
        let previous = std::env::var(key).ok();
        // SAFETY: used only in single-threaded (current_thread) tests, so no
        // other thread reads the environment concurrently.
        unsafe { std::env::set_var(key, value) };
        Self { key, previous }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        // SAFETY: same single-threaded test context as `set`.
        unsafe {
            match &self.previous {
                Some(value) => std::env::set_var(self.key, value),
                None => std::env::remove_var(self.key),
            }
        }
    }
}
```

The `temp-env` crate is the off-the-shelf version of this guard (with serialization
across tests); prefer it over accumulating hand-rolled copies.

---

## 6. Deterministic fixtures — scripted providers that refuse real credentials

Test doubles for external providers (LLM APIs, payment APIs, any metered third party)
are **scripted fixtures replaying canned exchanges** — recorded or hand-written
request/response pairs, not smart mocks. And the fixture's constructor enforces the
suite's most important structural property:

> **A test suite must be structurally incapable of silently becoming an integration
> run against production.**

The fixture asserts at startup that no production credential is present in the
environment, and refuses to run otherwise:

```rust
/// Well-known env vars that indicate a real provider credential is present.
const PRODUCTION_CREDENTIAL_VARS: &[&str] = &[
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "STRIPE_SECRET_KEY",
    "AWS_SECRET_ACCESS_KEY",
];

/// Set deliberately — and only — for a real integration run.
const INTEGRATION_OVERRIDE: &str = "ALLOW_REAL_PROVIDER_CREDENTIALS";

pub struct ScriptedProvider {
    exchanges: Vec<Exchange>,
}

impl ScriptedProvider {
    /// Refuses to construct while a production credential is visible.
    pub fn from_script(script: &str) -> Self {
        if std::env::var(INTEGRATION_OVERRIDE).is_err() {
            for var in PRODUCTION_CREDENTIAL_VARS {
                if std::env::var(var).is_ok() {
                    panic!(
                        "refusing to run scripted-provider tests: {var} is set in the \
                         environment. Unset it, or set {INTEGRATION_OVERRIDE}=1 for a \
                         deliberate integration run."
                    );
                }
            }
        }
        Self { exchanges: parse_script(script) }
    }
}
```

Without the guard, a developer with a key exported in their shell can run "the unit
suite" and silently exercise — and bill, and mutate — a production account. The
override env var exists so the rare deliberate integration pass is an explicit act,
visible in the command line that launched it.

The rest of determinism follows the same injection rule as §5:

- **Clocks are injected.** Domain code takes a clock (or a timestamp) as input; no
  `SystemTime::now()` / `Instant::now()` below the boundary.
- **RNG is injected and seeded:** `StdRng::seed_from_u64(...)` in tests. A test that
  cannot reproduce its own failure is a rumor.

---

## 7. E2E for terminal and desktop apps — a real pty, the release binary

The E2E tier is **black-box**: it drives the real binary over a real pty
(`portable-pty` / `expectrl`-class tools), with the external provider scripted (§6),
and asserts on emitted frames/output. It is the only tier where the shipped artifact
actually runs — and it is kept small and behaviour-critical, because each test costs
seconds, not microseconds.

```rust
use std::io::Write;
use std::time::Duration;
use portable_pty::{native_pty_system, CommandBuilder, PtySize};

#[test]
fn the_binary_starts_renders_the_prompt_and_exits_cleanly_on_quit() {
    let pty = native_pty_system()
        .openpty(PtySize { rows: 24, cols: 80, pixel_width: 0, pixel_height: 0 })
        .expect("open pty");

    // CARGO_BIN_EXE_<name> points at the binary built for the current profile;
    // run this tier with --release in CI so it exercises the shipped artifact.
    let mut cmd = CommandBuilder::new(env!("CARGO_BIN_EXE_myapp"));
    cmd.env("PROVIDER_BASE_URL", scripted_provider_url());
    let mut child = pty.slave.spawn_command(cmd).expect("spawn app");

    let mut reader = pty.master.try_clone_reader().expect("reader");
    let mut writer = pty.master.take_writer().expect("writer");

    // read_until: small poll-with-deadline helper collecting pty output.
    let frame = read_until(&mut reader, "> ", Duration::from_secs(5));
    assert!(frame.contains("> "), "prompt never rendered; got: {frame}");

    writer.write_all(b"q").expect("send quit key");
    let status = child.wait().expect("wait for exit");
    assert!(status.success(), "app exited non-zero");
}
```

A pty matters because terminal apps behave differently when stdout is a pipe (no
raw mode, no ANSI, different buffering) — piping the binary tests a code path users
never run. Keep assertions on stable output (a prompt, a status line, an exit code),
not on full frame snapshots that break with every cosmetic change.

---

### In-process service tests — the tier below the pty

For HTTP services (axum), there is a middle tier that needs no socket, no port, and
no spawned process: build the real router and drive it in process with tower's
`ServiceExt::oneshot`. The full middleware stack — auth, limits, error mapping — is
exercised; nothing is bound.

```rust
use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::util::ServiceExt;

#[tokio::test]
async fn a_request_without_a_bearer_token_is_refused_with_401() {
    let app = build_router(test_state());

    let response = app
        .oneshot(Request::builder().uri("/api/items").body(Body::empty()).unwrap())
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}
```

Reserve the pty tier for what only the shipped binary can prove; route handler and
middleware behaviour belongs here, at microsecond cost.

## 8. Property-based testing — narrow, four target classes

Adopt `proptest` 1.x **narrowly, in the core crate**, where inputs are structured and
invariants are crisp. Four target classes earn it:

1. **Serde round-trips** — `decode(encode(x)) == x` for every persisted or wire type.
2. **Parsers and frame buffering** — arbitrary chunk splits of a valid stream parse
   identically to the unsplit stream (the class of bug example tests never catch).
3. **State-machine invariants** — arbitrary event sequences never reach an illegal
   state; pairs with the typestate/enum modelling in `architecture.md` §2:

```rust
use proptest::prelude::*;

proptest! {
    #[test]
    fn any_event_sequence_never_reaches_an_illegal_state(
        events in prop::collection::vec(any::<Event>(), 0..64)
    ) {
        let mut machine = Machine::new();
        for event in events {
            // `apply` is total: it returns a state, it never panics.
            machine = machine.apply(event);
            prop_assert!(machine.invariants_hold());
        }
    }
}
```

4. **Numeric and limit logic** — quota arithmetic, backoff caps, pagination clamps.

Not for CRUD glue: a property test over "insert then select returns the row" is an
example test with extra steps. Mechanics: `#[derive(Arbitrary)]` (proptest-derive) or
hand strategies for `Event`; the **`proptest-regressions/` directory is committed** —
it is the permanent record of every falsified claim; default 256 cases per property
is fine, raise it locally when hunting.

---

## 9. Coverage

`cargo-llvm-cov`, with the threshold set in `toolchain-and-lints.md`:

```bash
cargo llvm-cov --workspace --all-features --summary-only   # CI gate
cargo llvm-cov --workspace --all-features --html           # local drill-down
```

Coverage is a **floor, not a target**: it exists to catch untested modules sneaking
in, not to be optimized. Chasing the number produces assertion-free tests that
execute lines and verify nothing. The reviewable rule: **uncovered lines in a diff
need a reason** — "unreachable by construction" or "exercised only by the E2E tier"
are reasons; silence is not.

---

## 10. Test-quality rules the reviewer enforces

- **One behaviour per test.** A test asserting three behaviours reports one failure
  and hides two.
- **No branching or loops in example-test bodies.** An `if` in a test means two tests
  are sharing a name; a loop means the failure message won't say which iteration.
  (Property-test bodies driving a machine through generated events are the deliberate
  exception — the harness owns the iteration.)
- **Assert observable behaviour, not internals.** A test asserting a mock was called
  passes when the outcome is wrong and fails when a refactor changes the path — both
  are the wrong answer. Assert the row exists, the response body, the emitted event.
- **`unwrap`/`expect` are fine in tests.** A panic in a test IS a failure report.
  The clippy exemptions in `toolchain-and-lints.md` make this free — spend the error
  discipline in production code, not in fixtures.

---

## 11. Checklist

- [ ] Test list written from acceptance criteria + failure tables before code; dev doc carries it.
- [ ] `cargo test -- --list` reads as a spec — sentences, no `test_` prefix, no method names.
- [ ] Units co-located in `#[cfg(test)]`; integration in `tests/` against the public API; doctests on public APIs.
- [ ] Every timing test uses `start_paused` + `advance`; no sleep >100ms anywhere in test code.
- [ ] No env mutation outside the guard + current-thread flavor; config injected, not read from env in the tree.
- [ ] Provider fixtures are scripted and refuse to run with a production credential present (§6).
- [ ] Clocks and RNG injected; RNG seeded; no wall-clock reads in domain code.
- [ ] E2E tier drives the release binary over a pty with scripted providers; small and behaviour-critical.
- [ ] Property tests on the four §8 classes; `proptest-regressions/` committed.
- [ ] Coverage at the `toolchain-and-lints.md` floor; uncovered diff lines have a stated reason.

---

## 12. Anti-patterns

| Anti-Pattern | Problem | Solution |
|---|---|---|
| Real network in unit tests | Slow, flaky, and one exported key away from hitting production | Scripted fixtures with the credential refusal guard (§6) |
| Sleep-based synchronization | A race with a timer — passes locally, flakes under CI load | `start_paused` + `advance`; await the actual signal (§4) |
| Asserting a mock was called | Verifies the path, not the outcome; wrong on both failure and refactor | Assert observable state: the row, the response, the event (§10) |
| Shared mutable fixture state | Tests pass alone, fail together (or worse: only pass together) | Fresh fixture per test; temp dirs; no `static mut`/`lazy_static` state |
| Hidden ordering dependencies between tests | `cargo test` runs tests in parallel and in any order — the suite lies nondeterministically | Each test builds its own world; if order matters, it is one test |
| Coverage-chasing assertion-free tests | Lines execute, nothing is verified; the floor becomes a fiction | One behaviour, one assertion set; review the claim, not the percentage (§9) |
