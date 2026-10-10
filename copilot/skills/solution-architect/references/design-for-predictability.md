# Architecture — Design for Predictability (contracts · state tables · FMEA · test-case-first · blast radius · extension points)

This reference exists to kill one specific, expensive failure: a feature is reviewed green while its
negative behaviour was never designed. The happy path ships, the demo works, and then every review
round finds bugs — because the guard, the refusal, the duplicate delivery, and the race were never
anyone's deliverable. The architect's job here is to hand the developer and the testers a design
whose failure modes are enumerated and whose test cases exist BEFORE implementation. When the
negative behaviour is designed up front, review verifies a plan; when it is not, review becomes an
expensive way to discover one.

## Proportionality — what to produce on which track

The change class comes from the workflow engine's classification, never from this file. Match the
deliverables to the track the engine picked:

| Track | Change class | Deliverables from this reference |
|-------|--------------|----------------------------------|
| `full` | significant — new service/dependency/schema/boundary, or security-sensitive | All six: D1–D6 |
| `standard` | a feature/story; new logic spanning files | D1 + D4 only |
| `floor` / `floor_min` | small / trivial | None — skip this reference |

Two clarifications:

- `ARCH_APPROVED` is a hard gate on its own triggers (new service, new dependency, schema change,
  cross-boundary change, public API) regardless of how small the change looks; when a trigger fires,
  produce the deliverables for the matching scope even on an otherwise-light track.
- Right-sized, the full set is a page or two of tables per boundary — not a document phase. If a
  deliverable is growing past that, the boundary is too big; split the boundary, not the document.

---

## D1 — Boundary contract sheet

**What it is.** One sheet per module boundary — a public API, a queue consumer, a storage layer, a
seam between modules. Each row states an invariant, precondition, or postcondition, plus the error
contract, the idempotency expectation, and the concurrency expectation for the boundary. It is
written BEFORE implementation: a contract discovered while coding is a design escape, not a design.

**Rules.**

- One sheet covers one boundary. List the boundaries the change touches first, then write sheets
  for those — not for the whole system.
- Every row names the failure behaviour when the clause is violated — a typed error, a rejection, an
  alert. "It shouldn't happen" is not a behaviour; `REJECT` with a reason is.
- The error contract separates errors that are part of the API (callers must handle them) from
  defects (a violated invariant that pages someone). Both are named here.
- Idempotency and concurrency rows are mandatory for every boundary that can be retried or raced —
  which is nearly all of them. "Called twice" and "called concurrently" are inputs, not accidents.
- The **Enforced by** column is filled at implementation, not at design time. It is deliberately the
  same "named enforcing symbol" the framework's Done rule demands for negative criteria: the
  contract sheet is where those names are born, and the final audit reads them back.

**Template.**

````markdown
| Boundary | Invariant / Pre / Post | Violated ⇒ (named failure behaviour) | Enforced by (symbol — filled at implementation) |
|----------|------------------------|--------------------------------------|-------------------------------------------------|
| `module.operation(args)` | Pre: … | `NamedError`; no state change | `Class.method` |
| `module.operation(args)` | Invariant: … | rejection / alert, named | `Class.method` |
| `module.operation(args)` | Post: … · Idempotency: repeat ⇒ … · Concurrency: race ⇒ … | duplicate effect = defect, alert | `Class.method` |
````

**Worked micro-example — payment capture boundary.**

| Boundary | Invariant / Pre / Post | Violated ⇒ (named failure behaviour) | Enforced by (symbol) |
|----------|------------------------|--------------------------------------|----------------------|
| `payments.capture(orderId, amount)` | Pre: order state is `AUTHORIZED` | `CaptureNotAuthorized` error; no state change | *(filled at implementation)* |
| `payments.capture(orderId, amount)` | Invariant: captured total never exceeds authorized total | `AmountExceedsAuthorization`; capture refused | *(filled at implementation)* |
| `payments.capture(orderId, amount)` | Post: exactly one ledger entry per successful capture; Idempotency: repeat with same key returns the first result | second ledger entry = defect; alert fires | *(filled at implementation)* |
| `payments.capture(orderId, amount)` | Concurrency: two concurrent captures on one order — exactly one wins | loser receives `AlreadyCaptured`, never a double charge | *(filled at implementation)* |

---

## D2 — State table

**What it is.** For every stateful entity or workflow, a matrix of states × events. Each cell gives
the next state, the action taken, and any guard condition. One table per stateful entity — if the
table will not fit on a page, the entity has too many responsibilities, and that is a design finding
in its own right.

**Rules.**

- **NO BLANK CELLS.** Every impossible combination is an explicit `REJECT(reason)`. Blank cells are
  where race bugs live: a blank cell is a decision nobody made, which the implementation will make
  by accident under load.
- Each cell maps 1:1 to a D4 test row — including every `REJECT` cell. A cell without a test is a
  designed behaviour nobody will notice regressing.
- The architect also names the **code representation** of the state set — a sealed interface (JVM),
  a discriminated union (TypeScript), or a Rust enum with data-carrying variants (see
  `../../../development/languages/rust/SKILL.md`) — so that exhaustiveness is compiler-enforced and
  a new state breaks every non-updated match site at compile time. The representation is part of
  the decision, not an implementation detail left to taste.
- Guards go in the cell, in brackets, so the reader sees the condition next to the transition it
  gates.
- Actions in a cell list their side effects explicitly — notifications, writes, emitted events. A
  transition's side effects are contract, and D3's partial-write class interrogates each one: a
  side effect the table does not mention is one the crash-recovery design cannot account for.

**Template.**

````markdown
| State ↓ / Event → | eventA | eventB | eventC |
|--------------------|--------|--------|--------|
| StateOne | NextState — action [guard] | REJECT(reason) | REJECT(reason) |
| StateTwo | REJECT(reason) | NextState — action | NextState — action [guard] |
| TerminalState | REJECT(terminal) | REJECT(terminal) | REJECT(terminal) |
````

**Worked micro-example — submission lifecycle.** Representation: sealed interface
`SubmissionState` with one variant per row, so a new state cannot be added without the compiler
listing every match site to update.

| State ↓ / Event → | submit | approve | reject | cancel |
|--------------------|--------|---------|--------|--------|
| Draft | Submitted — validate, notify reviewers [form complete] | REJECT(not submitted) | REJECT(not submitted) | Cancelled — discard draft |
| Submitted | REJECT(already submitted) | Approved — notify owner | Rejected — notify owner with reason | Cancelled [only by owner] |
| Approved | REJECT(terminal) | REJECT(terminal) | REJECT(terminal) | REJECT(terminal) |
| Rejected | Submitted — resubmit after edits | REJECT(not submitted) | REJECT(already rejected) | Cancelled |
| Cancelled | REJECT(terminal) | REJECT(terminal) | REJECT(terminal) | REJECT(terminal) |

The `Submitted × submit → REJECT(already submitted)` cell is the kind of decision a blank cell
silently delegates to whichever code path gets there first.

---

## D3 — Lightweight FMEA

**What it is.** Per boundary, a fast walk of a FIXED failure-class checklist, recording for each
class how the failure is detected, what the designed response is, how severe it is, and which test
scenario proves the response. The fixed list is the point: value comes from not being allowed to
skip the classes that feel unlikely.

**The fixed failure-class checklist.** Walk all of these, in order, for each boundary:

1. Timeout
2. Partial write / crash between steps
3. Retry & duplicate delivery
4. Concurrent access & interleaving
5. Malformed or hostile input
6. Version skew (old client–new server, and the reverse)
7. Clock skew
8. Resource exhaustion
9. Dependency down
10. Empty and huge input

Each class is shorthand for one question the design must answer:

| Class | The question it forces |
|-------|------------------------|
| Timeout | What does the caller do when no answer comes — and what state is the callee left in? |
| Partial write | If the process dies between step N and step N+1, what does recovery see? |
| Retry & duplicate delivery | What happens when the same request or message arrives twice? |
| Concurrent access | What happens when two of these interleave on the same entity? |
| Malformed / hostile input | What is rejected at the boundary before any effect occurs? |
| Version skew | Can last release's client talk to this release's server — and the reverse? |
| Clock skew | Does anything order, expire, or dedupe by wall-clock across machines? |
| Resource exhaustion | What degrades first under memory/disk/connection pressure, and is it visible when it does? |
| Dependency down | While the dependency is unreachable — refuse, queue, or degrade? Which, by design? |
| Empty and huge input | Do zero items and a million items both terminate with designed behaviour? |

**Rules.**

- "N/A" requires a stated reason. "Can't happen" without a reason is a blank cell wearing a label.
- An **H or M row without a test scenario ID is an incomplete design** — severity without a proof
  obligation is an opinion.
- Timebox roughly 30 minutes per boundary. This is not aerospace FMEA; it is a forcing function to
  make ten unpleasant questions get ten designed answers.
- Where the designed response needs a known pattern (saga, outbox, circuit breaker, idempotent
  consumer), cite the pattern from `architecture-patterns.md`, `event-driven.md`, or
  `security-architecture.md` rather than restating it here.

**Template.**

````markdown
| Failure mode | Detection | Designed response | Severity (H/M/L) | Test scenario ID |
|--------------|-----------|-------------------|------------------|------------------|
| timeout: … | … | … | H | TC-… |
| partial write: … | … | … | M | TC-… |
| hostile input: … | … | … | H | TC-… |
````

**Worked micro-example — payment capture boundary.**

| Failure mode | Detection | Designed response | Severity | Test scenario ID |
|--------------|-----------|-------------------|----------|------------------|
| Timeout on provider capture call | client timeout at 5s | retry with the same idempotency key, max 3; then park order in `CAPTURE_PENDING` for reconciliation | H | TC-12 |
| Crash between provider capture and ledger write | reconciliation job diffs provider records vs ledger | replay from provider record; ledger write is idempotent on capture id | H | TC-13 |
| Duplicate webhook delivery | idempotency key seen in store | drop duplicate, log at debug, return 200 | M | TC-14 |
| Hostile amount input (negative, overflow, wrong currency) | request validation at the boundary | `REJECT(InvalidAmount)`; no provider call is made | H | TC-15 |
| Clock skew between provider and ledger timestamps | N/A — ordering uses provider capture id, never wall-clock | (reason stated; no response needed) | L | — |

---

## D4 — Test-case table

**What it is.** The central artifact of this reference. Produced BEFORE implementation and handed
to the QA role and the developer: the developer uses it as the red phase of TDD; the QA role uses
it as the skeleton of the test plan. It is the point where the design's promises become checkable.

**Rules.**

- Columns are fixed: **ID | Given / When / Then | Kind (unit/integration/e2e) | Negative? |
  Source (AC # / D2 cell / D3 row) | Enforcing symbol (filled by the developer at implementation)**.
- **Traceability:** every acceptance criterion, every D2 cell, and every D3 H/M row appears exactly
  once. A source with no row is an undesigned behaviour; a source with two rows is a duplicated
  intent that will drift.
- **NEGATIVE ROWS ≥ POSITIVE ROWS.** This is the framework's hard-won lesson applied at design
  time: audits repeatedly find the negative criteria unmet — the guard, the refusal, the
  cannot-bypass — while the happy paths ship. If the table has more positive rows than negative
  ones, the design has enumerated fewer refusals than features, and the review rounds will find the
  difference the expensive way.
- **Kind is the cheapest sufficient proof.** A D2 `REJECT` cell is usually a unit test on the state
  machine; a D3 crash-recovery or duplicate-delivery row usually needs an integration test; reserve
  e2e for what only the assembled system can prove. Placing every row at e2e is how test suites
  become slow enough to skip.
- **IDs are stable once handed off.** The QA role's traceability matrix and the E2E automation
  reference them; renumbering after handoff silently breaks every downstream mapping.
- The **Enforcing symbol** column starts empty. The developer fills it during implementation with
  the symbol that makes the row pass; the final verification audit reads this column to check that
  every designed behaviour is enforced by named code, not by hope.
- **Boundary with the QA role:** this table derives from design artifacts (AC, D2, D3). The QA role
  adds the user-behaviour dimension on top — personas, exploratory testing, value-delivery
  judgement — and owns test-design methodology; see
  `../../../quality/testing/tester/SKILL.md` rather than restating it here. This table is the QA
  role's input, not its replacement.

**Template.**

````markdown
| ID | Given / When / Then | Kind | Negative? | Source | Enforcing symbol |
|----|---------------------|------|-----------|--------|------------------|
| TC-01 | Given … When … Then … | unit | no | AC-1 | *(filled at implementation)* |
| TC-02 | Given … When … Then REJECT(…) | unit | yes | D2: State×event | *(filled at implementation)* |
| TC-03 | Given … When … Then … | integration | yes | D3: failure row | *(filled at implementation)* |
````

**Worked micro-example — submission lifecycle (rows drawn from D2/D3 above).**

| ID | Given / When / Then | Kind | Negative? | Source | Enforcing symbol |
|----|---------------------|------|-----------|--------|------------------|
| TC-01 | Given a complete Draft, When submit, Then state is Submitted and reviewers are notified | integration | no | D2: Draft×submit | *(filled at implementation)* |
| TC-02 | Given a Submitted item, When approve, Then state is Approved and the owner is notified | integration | no | D2: Submitted×approve | *(filled at implementation)* |
| TC-03 | Given a Draft, When approve, Then REJECT(not submitted) and state is unchanged | unit | yes | D2: Draft×approve | *(filled at implementation)* |
| TC-04 | Given a Submitted item, When submit again, Then REJECT(already submitted) | unit | yes | D2: Submitted×submit | *(filled at implementation)* |
| TC-05 | Given an Approved item, When cancel, Then REJECT(terminal) | unit | yes | D2: Approved×cancel | *(filled at implementation)* |
| TC-06 | Given a non-owner, When cancel a Submitted item, Then the guard refuses | integration | yes | D2: Submitted×cancel guard | *(filled at implementation)* |

Four negative rows to two positive: the ratio is the design telling the truth about where the risk
is.

---

## D5 — Blast-radius note

**What it is.** Required before touching any shared contract — a wire format, a schema, a published
event, a shared library's API. It answers, in writing: who consumes this, how do we know, what
class of change is this, in what order do the parts move, and how do we get back.

**Rules.**

- **Consumer list with the method attached.** State HOW the list was found — a grep across the
  consuming repositories, a generated-client registry, a topic-subscription listing. "We believe
  only X uses it" is a guess, not a method, and the unlisted consumer is the one that breaks.
- **Compatibility class**, one of three: *additive* (new optional field, new endpoint), *behavioural*
  (same shape, different meaning — the most dangerous class, because every schema check passes),
  *breaking* (removed/renamed/retyped). Behavioural changes get the same rigor as breaking ones.
- **Migration order** written as steps with the compatibility window stated — which versions must
  coexist, and for how long.
- **Rollback plan** that works from every intermediate step, not only from the end state.
- The note is written before the first commit that touches the contract, and attached to the
  ticket. A blast-radius note reconstructed after the fact is a post-mortem.

**Standards to hold the design to.**

- A single schema source of truth, with generated types on both producer and consumer sides and a
  CI drift check — hand-maintained mirror types are where silent skew starts.
- Versioned wire contracts: additive-only within a version; breaking changes require an explicit
  version bump, never a quiet mutation.
- Database changes follow expand–migrate–contract. The mechanics of writing those migrations —
  locks, batching, backfill, constraint timing — belong to the SQL language skill:
  `../../../development/languages/sql/SKILL.md`.

**Template.**

````markdown
| Field | Entry |
|-------|-------|
| Contract touched | … |
| Compatibility class | additive / behavioural / breaking |
| Consumers (and how the list was found) | … (found via: grep / client registry / subscription list) |
| Migration order | 1. expand … 2. migrate consumers … 3. contract … |
| Rollback plan | from step 1: … · from step 2: … |
````

**Worked micro-example — renaming `amount` to `amount_minor` in a published payment event.**

| Field | Entry |
|-------|-------|
| Contract touched | `payment.captured` event, schema v3 |
| Compatibility class | breaking (rename + unit change: major units → minor units) |
| Consumers (how found) | ledger service, notifications, analytics export — via the schema registry's subscription list, confirmed by grep across the consumer repositories |
| Migration order | 1. expand: publish both fields in v3 (additive) · 2. migrate all three consumers to `amount_minor` · 3. contract: bump to v4, drop `amount` |
| Rollback plan | steps 1–2: revert consumer commits, both fields still published · step 3: re-publish v3 alongside v4 until consumers are reverted |

---

## D6 — Extension-point declaration

**What it is.** "Easier to extend with predictable consequences" means variation points are
DECLARED, not discovered. An undeclared extension point is discovered by the next developer as a
grep through call sites, and each addition is a small archaeology project with an unknown blast
radius. This deliverable names each point where the design expects variation, and makes the cost of
adding a variant predictable.

**Rules.** For each extension point, declare:

- **What varies** — the axis of variation (payment method, export format, notification channel).
- **What is fixed** — the invariants an extension may not break, by reference to the D1 sheet.
  These are the guarantees that make extensions safe to add without re-reviewing the world.
- **The recipe** — exactly which files a new variant touches and which tests must be added. Two
  mechanisms make the recipe self-enforcing: D2's sealed representation makes a forgotten consumer
  a compile error, and an architecture-rule test makes a forgotten registration a test failure. A
  recipe that relies on the author remembering a step is a recipe for a partial variant.
- **The mechanism**, chosen by who extends: a closed set the team controls → sealed variants; a
  point third parties extend → a registry or adapter seam with the contract at the seam.
- **Feature flags** carry an owner and an expiry date at declaration time. An expired flag is a
  board-integrity warning — the Scrum-Master role's territory
  (`../../../management/scrum-master/SKILL.md`).
- **Deprecation** happens only with a named replacement, a removal date, and a rule that freezes
  new usages (a lint or architecture test) — otherwise the deprecated path grows while it waits to
  die.
- Declaring **non-extension** is equally valid: "this set is closed; extend via design review"
  stops speculative seams, which cost maintenance while serving nobody.

**Template.**

````markdown
| Extension point | What varies | Fixed invariants (from D1) | Recipe (files + required tests) | Mechanism |
|-----------------|-------------|----------------------------|----------------------------------|-----------|
| … | … | … | … | sealed variants / registry / adapter seam |
````

**Worked micro-example — payment method as an extension point.**

| Extension point | What varies | Fixed invariants (from D1) | Recipe (files + required tests) | Mechanism |
|-----------------|-------------|----------------------------|----------------------------------|-----------|
| Payment method | authorization + capture flow per provider | captured ≤ authorized; exactly one ledger entry per capture; idempotent on retry | add one variant to the sealed `PaymentMethod` set; implement the two-method provider contract; add its D4 rows (one positive, the refusal, the duplicate-delivery case); register in the method registry — a missed registration fails the architecture-rule test, a missed match site fails compilation | sealed variants + registry |

---

## Anti-patterns

| Anti-pattern | Why it fails |
|--------------|--------------|
| Contract sheet written during implementation | It documents what the code happens to do, which is precisely the thing it was meant to constrain |
| Blank state-table cell, "we'll handle it later" | "Later" is a production race; the cell gets decided by whichever code path reaches it first |
| "N/A" in the FMEA without a reason | A blank cell wearing a label — the class was skipped, not answered |
| Test-case table produced after the code | It inherits the code's blind spots; the table must derive from AC, D2, and D3, not from the diff |
| Negative rows in the minority | The design enumerated more features than refusals — the review rounds will find the difference |
| H/M FMEA rows with no test scenario ID | Severity became an opinion instead of a proof obligation |
| All six deliverables on a one-file change | Over-processing here erodes the will to do this on the change that actually needs it |
| Extension by grep | The variation point was discovered, not declared — every addition is archaeology with unknown blast radius |
| Enforcing-symbol column still empty at Done | The designed behaviours were promised, not enforced; the final audit must bounce it |

---

## Gate integration — where each deliverable lands

| Deliverable | Gate / consumer |
|-------------|-----------------|
| D1–D6 | Attached to the `ARCH_APPROVED` ticket note — they *are* the "boundaries, constraints, recommended pattern" content of that note, at design-for-predictability depth |
| D4 | Feeds the QA role's specs (`../../../quality/testing/tester/SKILL.md`), the developer's TDD red phase, and the final verification audit of the Enforcing-symbol column |
| D3 (H-severity rows in security failure classes) | Route to `/secops` — hostile input, authz races, resource exhaustion overlap the `SECOPS_APPROVED` triggers, and that gate is a safety override: hard even when the change is otherwise small |
| D5 | Fires whenever the `schema_change` / `cross_boundary` / `public_api` triggers defined in the workflow definition fire — the same triggers that make `ARCH_APPROVED` hard |
| D3 mitigations needing a known pattern | Cite `architecture-patterns.md`, `event-driven.md`, or `security-architecture.md` — do not restate patterns in the FMEA |

The through-line: D1 names the promises, D2 closes the state space, D3 interrogates it, D4 turns
all three into proof obligations, D5 bounds the damage of changing a promise, and D6 prices the
cost of the next variant. A design with these six on the table before implementation is one where
the review confirms, rather than discovers, the negative behaviour.
