# Rust — Ownership-Driven Architecture: Types, State Machines, and the Thin-Shell Pattern

How to structure a Rust application so the compiler carries the review: invariants
live in types, state transitions are exhaustive matches, and every user-facing
surface is a thin adapter over one framework-agnostic core. The goal is boring
reviews — the compiler prints what must change; the reviewer confirms, not hunts.

**Mental model.** Ownership is an architecture tool, not only a memory-safety tool.
Every decision below reduces to two questions: *who owns this state*, and *what is
the total set of shapes it can take?* Answer both in the type system and review
becomes reading. Assumes Rust 2024 edition (stable since 1.85). Lint backing:
`toolchain-and-lints.md`; errors: `errors.md`; tasks/channels: `async-tokio.md`;
review order: `rust-review.md`.

---

## 1. The core-crate pattern

One framework-agnostic **core crate** owns all domain logic and state; every shell —
CLI, TUI, GUI, daemon, test harness — is an adapter translating its framework's input
into core events and rendering core state back out.

**Standards (each one checkable):**

1. The core crate has **zero UI-framework dependencies** — `cargo tree -p <core> -e no-dev` lists no UI/TUI/GUI crate and no HTTP framework.
2. Shells define **no domain types** and contain **no business branching** — a shell `match` dispatches on *which* event or effect arrived, never on what it means.
3. A **UI-framework major bump touches only the shell crate** — if it forces an edit inside the core, a framework type leaked across the seam: a review finding.
4. Core state changes only through the event seam below; no shell reaches into fields.

### Template: the event/effect seam

Events in, transitions inside, side effects out **as data** — executed by the shell:

```rust
// core/src/app.rs — this crate imports no UI framework and no async runtime.

/// Everything that can happen TO the application.
pub enum Event {
    QuerySubmitted { text: String },
    ResultsArrived { hits: Vec<String> },
    CancelRequested,
}

/// Side effects the core *requests*; the shell/runtime layer executes them.
pub enum Effect {
    StartSearch { query: String },
    AbortSearch,
    Render,
}

pub enum Phase { Idle, Searching { query: String } }

pub struct App {
    pub phase: Phase,
    pub last_results: Vec<String>,
}

impl App {
    pub fn handle(&mut self, event: Event) -> Vec<Effect> {
        match event {
            Event::QuerySubmitted { text } => {
                self.phase = Phase::Searching { query: text.clone() };
                vec![Effect::StartSearch { query: text }]
            }
            Event::ResultsArrived { hits } => {
                self.last_results = hits;
                self.phase = Phase::Idle;
                vec![Effect::Render]
            }
            Event::CancelRequested => match self.phase {
                Phase::Searching { .. } => {
                    self.phase = Phase::Idle;
                    vec![Effect::AbortSearch, Effect::Render]
                }
                Phase::Idle => vec![],
            },
        }
    }
}
```

**Why this shape:** `handle` is a pure transition over `&mut self` — no I/O, no
`await`, no framework types — so every transition is testable with plain `#[test]`
functions: construct a state, feed an event, assert on state and effects. No mocks,
no runtime. Effects-as-data keeps the runtime, HTTP client, and disk behind one
executor in the shell layer (`async-tokio.md`); a new UI replaces the adapter only.

---

## 2. State machines as pure enums

Model any lifecycle with more than two states as an enum whose variants carry exactly
the data valid in that state. Transitions are methods returning
`Result<NextState, TransitionError>`. Matches on domain enums are **exhaustive with
no `_ =>` arm** — adding a state must break compilation at every consumer.

### Worked template: a request/turn lifecycle

`Idle → Held → Sending → Done | Cancelled`, with *cancelled-before-send* modeled as
a transition to a data-carrying state, not a boolean flag on some other state:

```rust
#[derive(Debug)]
pub enum Turn {
    Idle,
    Held { draft: String },
    Sending { request_id: u64, draft: String },
    Done { response: String },
    Cancelled { at: CancelPoint },
}

#[derive(Debug, PartialEq, Eq)]
pub enum CancelPoint { BeforeSend, InFlight }

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum TransitionError {
    #[error("send requires a held draft")] NotHeld,
    #[error("cancel is not valid from this state")] CannotCancel,
}

impl Turn {
    /// Held -> Sending. Consumes the value, so the pre-send shape cannot be reused.
    pub fn send(self, request_id: u64) -> Result<Turn, TransitionError> {
        match self {
            Turn::Held { draft } => Ok(Turn::Sending { request_id, draft }),
            Turn::Idle | Turn::Sending { .. } | Turn::Done { .. } | Turn::Cancelled { .. } => {
                Err(TransitionError::NotHeld)
            }
        }
    }

    /// Cancelled-before-send is a *state*, not a `was_cancelled` boolean.
    pub fn cancel(self) -> Result<Turn, TransitionError> {
        match self {
            Turn::Held { .. } => Ok(Turn::Cancelled { at: CancelPoint::BeforeSend }),
            Turn::Sending { .. } => Ok(Turn::Cancelled { at: CancelPoint::InFlight }),
            Turn::Idle | Turn::Done { .. } | Turn::Cancelled { .. } => {
                Err(TransitionError::CannotCancel)
            }
        }
    }
}

// In the module's #[cfg(test)] mod tests (structure: `testing.md`):
#[test]
fn cancel_before_send_is_recorded_as_a_state() {
    let turn = Turn::Held { draft: "hello".into() };
    let cancelled = turn.cancel().expect("Held -> Cancelled is a legal transition");
    assert!(matches!(cancelled, Turn::Cancelled { at: CancelPoint::BeforeSend }));
    // A finished turn refuses to send — the failure is a value, not a panic.
    assert!(cancelled.send(1).is_err());
}
```

Add a `Turn::Paused` variant and `send`, `cancel`, and every rendering `match` stop
compiling until someone decides what `Paused` means there — that compile-error list
*is* the change checklist. A wildcard arm would have shipped the new state silently
half-handled: that is why `_ =>` on a domain enum is a review finding.

---

## 3. Newtype discipline — parse, don't validate

IDs, tokens, paths, counts, and budgets cross module boundaries as **newtypes** with
a private inner field and a checked constructor. **Rule:** a raw `String` or `u64`
with domain meaning crossing a module boundary is a review finding. The constructor
is the *only* way in — including through serde, so a deserialized value is already valid:

```rust
use serde::{Deserialize, Serialize};

/// A validated session identifier. The inner field is private: every value in the
/// program — including deserialized ones — passed the same checks exactly once.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(try_from = "String")] // derived Serialize already writes the inner string
pub struct SessionId(String);

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum SessionIdError {
    #[error("session id is empty")] Empty,
    #[error("session id contains a character outside [a-z0-9-]")] BadCharacter,
}

impl SessionId {
    pub fn as_str(&self) -> &str { &self.0 }
}

impl TryFrom<String> for SessionId {
    type Error = SessionIdError;
    fn try_from(raw: String) -> Result<Self, Self::Error> {
        if raw.is_empty() {
            return Err(SessionIdError::Empty);
        }
        if !raw.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-') {
            return Err(SessionIdError::BadCharacter);
        }
        Ok(SessionId(raw))
    }
}

// In the module's #[cfg(test)] mod tests:
#[test]
fn serde_round_trip_enforces_the_invariant() {
    let id: SessionId = serde_json::from_str("\"sess-01hq3k\"").expect("valid id parses");
    assert_eq!(serde_json::to_string(&id).expect("serializes"), "\"sess-01hq3k\"");
    // The same constructor runs inside serde: bad input fails to DESERIALIZE.
    assert!(serde_json::from_str::<SessionId>("\"NOT VALID\"").is_err());
}
```

Add a delegating `TryFrom<&str>` for borrowed input; add length/charset limits as the
domain requires (`#[serde(try_from)]` needs `Display` on the error — `thiserror`
provides it). Downstream code takes `SessionId`, never `&str`: nobody validates twice.

---

## 4. Typestate — order-dependent protocols at compile time

When a protocol has **compile-time-known, order-dependent steps** — a builder that
must not build incomplete, a connection that must not query before connect — encode
the step as a type parameter with zero-sized state types and `PhantomData`:

```rust
use std::marker::PhantomData;

pub struct NeedsUrl;
pub struct Ready;

pub struct RequestBuilder<Stage> {
    url: String, // placeholder until `Stage = Ready`; private, so never observable
    _stage: PhantomData<Stage>,
}

pub struct Request { pub url: String }

impl RequestBuilder<NeedsUrl> {
    pub fn new() -> Self {
        RequestBuilder { url: String::new(), _stage: PhantomData }
    }

    /// The only way to reach `Ready` — and `build` exists only on `Ready`.
    pub fn url(self, url: impl Into<String>) -> RequestBuilder<Ready> {
        RequestBuilder { url: url.into(), _stage: PhantomData }
    }
}

impl RequestBuilder<Ready> {
    pub fn build(self) -> Request {
        Request { url: self.url }
    }
}
```

Misuse is not a runtime error — it does not compile:

```text
error[E0599]: no method named `build` found for struct `RequestBuilder<NeedsUrl>`
```

If a placeholder field feels wrong, move it *into* the state type (`Ready { url: String }`)
so the not-yet-set shape has nowhere to live. Stage-independent setters (timeouts,
headers) live on `impl<Stage> RequestBuilder<Stage>`.

**Honest counter-rule:** typestate is for sequences known at compile time. For
**runtime-dynamic** states — the current value depends on input, time, or the peer —
an enum state machine (§2) beats typestate: you cannot store a `Client<whatever state
it is in>` without erasing the parameter that did the work. An enum *of* typestates
means you wanted §2 all along.

---

## 5. Making illegal states unrepresentable — a worked micro-refactor

The before-shape, seen in almost every codebase:

```rust
pub struct Conn {
    connected: bool,
    addr: Option<Addr>,
    retries: u8,
}
```

Two concrete bugs this shape *permits* (and therefore, eventually, contains):

1. **`addr` read while `None`.** Nothing ties `connected == true` to `addr.is_some()`;
   every read of `addr` needs its own guard, and one will be wrong after a refactor.
2. **`retries` meaningful in only one phase.** It only means something while
   reconnecting, but it is always present and mutable — bumped or trusted in the
   wrong phase, no diagnostic.

The refactor — one variant per phase, each carrying exactly the data valid in it:

```rust
pub enum Conn {
    Disconnected,
    Reconnecting { addr: Addr, attempts: u8 },
    Connected { addr: Addr },
}
```

Both old bugs are now compile errors, produced at every offending site:

```text
error[E0609]: no field `addr` on type `Conn`          // every read must match a phase
error[E0026]: variant `Conn::Connected` does not have a field named `attempts`
```

The cost is a handful of `match` arms; the return is the invariant enforced at every
future call site. Spend it whenever a `bool` and an `Option` travel together.

---

## 6. Shared-state discipline

**`Arc<Mutex<_>>` minimal and short-lived.** The pattern is always: lock → extract or
clone → drop the guard → *then* `await` (`clippy::await_holding_lock` denies it —
`toolchain-and-lints.md`).

```rust
use std::sync::{Arc, Mutex};

pub struct AppState { pub endpoint: String, pub cached: Option<String> }

pub async fn refresh(state: Arc<Mutex<AppState>>, client: reqwest::Client)
    -> Result<(), reqwest::Error>
{
    // 1. Lock, extract, DROP — never across an await. Cache state: poison = recover.
    let endpoint = {
        let guard = state.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        guard.endpoint.clone()
    };
    // 2. Await with no lock held.
    let body = client.get(&endpoint).send().await?.text().await?;
    // 3. Re-lock briefly to publish the result.
    state.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).cached = Some(body);
    Ok(())
}
```

- **Prefer message passing for cross-task ownership.** When several tasks contend on
  one `AppState`, give each subsystem an actor that *owns* its state and takes
  commands over a channel (`async-tokio.md`). Ownership scales; sharing contends.
- **`RwLock` only with measured read contention** — it buys read parallelism at the
  price of writer-starvation risk. Adopt it on a profile, not vibes.
- **Poisoned-lock policy is a per-state decision**, not a reflex `.unwrap()`: recover
  a cache; propagate a named error where a half-updated value could be acted on.

---

## 7. UI shells — what is deliberately NOT here

This reference encodes **no UI-framework API content**: current Rust GUI frameworks
are pre-1.0 with breaking minor releases, and any API detail written here would rot in
months. APIs are looked up against the framework's own repository at task time
(Documentation Lookup rule in `../SKILL.md`).

What **is** stable, and is the contract every shell follows:

```text
fn draw(frame, &AppState)                        // render: pure function of core state
fn map_input(framework_input) -> Option<Event>   // input: mapped to core events
```

- **Immediate-mode render as a pure function of core state** — `draw` reads
  `&AppState` and paints; it never mutates, performs I/O, or decides. If a redraw
  needs data the state lacks, that is a core change first.
- **Input mapped to core events** — a key press becomes an `Event` (§1) or nothing;
  the shell translates, it never "handles".
- **No domain logic in view code** — a conditional in `draw` may choose *how to
  show* a state, never *what the state means*.
- **Framework types never appear in core signatures** (§1) — which is what makes
  pre-1.0 churn survivable: the blast radius of a breaking release is one adapter crate.

---

## 8. Modules and visibility

- **`pub(crate)` by default**; plain `pub` is reserved for the intentional surface.
- **`unreachable_pub` at `warn`** (pinned in `toolchain-and-lints.md`) catches `pub`
  items no external caller can reach — accidental API surface.
- **One directory per domain module**; helpers stay private to it. **Re-export the
  intentional surface from `lib.rs`** so the API is one screen:

```rust
// lib.rs — the deliberate public surface. Everything under `mod` is internal.
mod http;
mod ingest;
mod store;

pub mod config;

pub use config::Config;
pub use ingest::{IngestError, IngestReport};
```

A reviewer should be able to read `lib.rs` and know the whole contract of the crate.

---

## 9. Checklist

- [ ] `cargo tree -p <core> -e no-dev` shows no UI/HTTP-framework crates in the core (§1).
- [ ] Shells contain no domain types and no business branching — dispatch only (§1).
- [ ] Every domain enum is matched exhaustively; no `_ =>` arm anywhere (§2).
- [ ] Lifecycles with more than two states are enums, not flag fields (§2, §5).
- [ ] Boundary-crossing primitives are newtypes; serde goes through `try_from` (§3).
- [ ] Typestate used only for compile-time-known sequences (§4).
- [ ] No lock guard held across an `await`; poisoning policy decided per state (§6).
- [ ] `lib.rs` re-exports the whole public surface; `unreachable_pub` is clean (§8).

## Anti-patterns

| Anti-Pattern | Problem | Solution |
|---|---|---|
| Shell contains a `match` on domain rules | Business logic forks per shell; the second shell ships a subtly different product | Move the branch into the core's `handle`; shells dispatch events and execute effects, nothing else (§1) |
| Core crate imports a UI crate | Framework churn forces core changes; domain tests now need a framework | Cut the dependency; pass data out as effects; verify with `cargo tree -e no-dev` (§1) |
| State flags (`bool` + `Option` pairs) instead of enums | Illegal combinations are constructible, so they eventually get constructed | One enum variant per phase, carrying exactly that phase's data (§5) |
| God-`AppState` behind one mutex where subsystems fit actors | Every task contends on one lock; one slow path stalls unrelated features | Per-subsystem actor tasks owning their slice of state (§6, `async-tokio.md`) |
| Typestate for runtime-dynamic states | The state parameter cannot be stored or chosen at runtime without erasing it | Enum state machine with transition methods (§2); typestate only for compile-time sequences (§4) |
| `_ =>` arm on a domain enum | A new variant compiles everywhere and is handled nowhere | Exhaustive matches; let the compiler enumerate every consumer that must decide (§2) |
