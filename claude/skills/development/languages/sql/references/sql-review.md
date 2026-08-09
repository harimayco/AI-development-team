# SQL — Review Pass (loaded by /rev)

> The reviewer's teeth for SQL and migrations. Developer doctrine — the standards, dialect
> semantics, and templates — is the rest of this skill (`../SKILL.md` and the dialect
> references); this file is what /rev runs and what /rev blocks on.

The pass has a fixed shape: **Pass 0** is mechanical (lint + greps, no project config,
no judgement), **migration review** works the checklist against every migration file in
the diff, **the manual read** covers what no grep can see, and findings land in the fixed
severity mapping at the end. Run Pass 0 first — arguing about style in a PR that still
concatenates user input into a statement is reviewing the wrong thing.

## 1. Trigger

Load this reference when the diff contains:
- `.sql` files, or anything under a migrations directory (`migrations/`, `db/migrate/`,
  Flyway/Liquibase paths)
- Embedded SQL strings in any host language
- ORM annotations carrying raw SQL (`@Query(nativeQuery = true)`, `raw()`, `sql` tagged
  templates, `text()` clauses)

## 2. Pass 0 — no-config commands

The grep contract (same as backend review): **every hit needs a check or is a finding.**
Each hit is individually dispositioned — verified safe (static string, bound parameters,
deliberate full-table write with its comment) or reported. A hit with no disposition is
itself a finding: an unexamined hit and a missed injection look identical in the report.

### Lint

```bash
pipx install sqlfluff      # 4.3+
sqlfluff lint --dialect postgres <paths>     # or: mysql | oracle | sqlite
```

Infer the dialect from the repo's driver or connection config — do not guess. Core rules
need no project `.sqlfluff`; if the repo carries one, it wins. Lint findings are MINOR on
their own; a file that cannot even parse under the declared dialect is not.

sqlfluff lints `.sql` files; SQL embedded in host-language strings is covered by the greps
below — a green lint run says nothing about the embedded statements, so never skip the
greps because the linter passed.

### Postgres migration linter

```bash
# squawk (~0.10) — Postgres migrations only; npm package squawk-cli, or a release binary
npx squawk-cli <migration files>
```

Catches non-`CONCURRENTLY` index creation, `NOT NULL` additions, and blocking constraint
patterns before they reach a live table. Each squawk warning maps onto the lock-analysis
row of the migration checklist below — it is evidence, not a verdict; the author's stated
plan can overrule it, silence cannot.

### String-built SQL, per host language

```bash
# Rust — format!() within two lines of a SQL verb (first grep keeps file:line; the
# filter adds none, so hits stay usable as evidence)
grep -rn --include='*.rs' -A2 'format!' . | grep -iE '\b(SELECT|INSERT|UPDATE|DELETE)\b'

# Python — f-strings, %-formatting, or concatenation near SQL verbs
grep -rniE "f['\"][^'\"]*\b(select|insert|update|delete)\b" --include='*.py' .
grep -rniE "\b(select|insert|update|delete)\b[^'\"]*['\"] *(%|\+|\.format\()" --include='*.py' .

# Java / Kotlin — concatenation onto a SQL string
grep -rniE '"[^"]*\b(select|insert|update|delete)\b[^"]*" *\+' --include='*.java' --include='*.kt' .

# JS / TS — template literals interpolating into SQL
grep -rniE '`[^`]*\b(select|insert|update|delete)\b[^`]*\$\{' --include='*.js' --include='*.ts' .
```

What clears a hit: the assembled parts are all **completely static strings** (a query
built from fixed fragments is ugly but safe), or every runtime value travels as a **bind
parameter**. Anything assembling a *value* into SQL text is **BLOCKING**. Assembled
*identifiers* (a column in `ORDER BY`, a table suffix) are acceptable only from a static
allowlist map visible at the call site — see `../SKILL.md` standard 2.

### Unbounded writes

```bash
grep -rniE 'DELETE FROM|UPDATE [A-Za-z_"]+ SET' --include='*.sql' <paths>
```

Every hit carries a `WHERE`, or an explicit comment on the statement declaring the
full-table write deliberate. No third state — "obviously intended" is not a disposition.

### Smell greps

```bash
grep -rn 'SELECT \*' <production paths>       # missing column contract
grep -rniE 'NOT IN *\( *SELECT' <paths>       # NULL-poisonable — ../SKILL.md standard 8
grep -rniE "LIKE *'%" <paths>                 # leading wildcard: unindexable search plan
```

What clears each: `SELECT *` — only in throwaway tooling or `EXISTS(SELECT * ...)`
subqueries, never in a production result path. `NOT IN (SELECT ...)` — the subquery's
column is provably `NOT NULL` (constraint, not vibes), else demand `NOT EXISTS`. Leading
wildcard — acceptable only when a real search index (FTS/trigram) serves the query and
the `LIKE` is a residual filter, not the plan.

### Migration-history integrity

```bash
git log --oneline --diff-filter=M -- <migrations dir>
```

A migration file modified **after it reached the default branch** is **BLOCKING** — every
database that already applied it has silently diverged from the file; the fix is a new
migration. Fixup commits to a migration introduced by the same unmerged PR are fine —
check where each modifying commit sits relative to the default branch before flagging.

## 3. Migration review checklist

- [ ] Linear numbering — no collisions with concurrently merged branches, no renumbering
- [ ] Expand/contract phase labelled in the file header (`../SKILL.md` has the template)
- [ ] Lock analysis stated — check every DDL statement against the table below
- [ ] Backfills batched, with a stated batch size and a pause/lag bound between batches
- [ ] Index justification comment present — it names the query the index serves
- [ ] `EXPLAIN` evidence attached for new query shapes, at realistic cardinality
- [ ] Rollback stance stated — forward-only by default; a down-migration only if rehearsed

Dangerous DDL, condensed per dialect — a migration containing any of these with no stated
plan is a finding:

| Dialect | Dangerous DDL | Why it blocks |
|---|---|---|
| Postgres | `ALTER TABLE ... TYPE`, inline `SET NOT NULL` on a large table, `CREATE INDEX` without `CONCURRENTLY`, adding a constraint without `NOT VALID` + separate `VALIDATE` | Table rewrite, or an `ACCESS EXCLUSIVE` lock held for the duration |
| MySQL | ALTERs that fall back to `ALGORITHM=COPY` (charset changes, most type changes) — anything not verified `INSTANT`/`INPLACE` | Full table copy; writes blocked or replica lag explodes |
| Oracle | Any DDL inside a multi-step migration | DDL auto-commits the surrounding transaction — there is no atomic multi-step migration; each statement needs its own failure plan |
| SQLite | Anything beyond `ADD COLUMN`/`RENAME` | Requires a create-copy-drop-rename table rebuild — it must be written explicitly, not hidden by tooling |

## 4. EXPLAIN evidence — what counts

"I ran EXPLAIN" is not evidence; the plan is. What the attachment must show:

- **Realistic cardinality.** A hundred-row dev table seq-scans everything and the planner
  is right to — a plan from it proves nothing. Seed a scratch container to a plausible row
  count before asking for the plan; one line does it on Postgres:

```sql
INSERT INTO orders (id, status) SELECT g, 'open' FROM generate_series(1, 500000) g;
ANALYZE orders;
```
- **The three questions, answered by the plan text:**
  1. Is the expected index used for the selective predicate — or is there a seq/full scan
     where the author claimed an index?
  2. Are the row estimates sane against the actual counts (`EXPLAIN ANALYZE` where the
     engine has it)? Estimates off by orders of magnitude mean the plan will flip in
     production.
  3. Where does filtering happen — a large "rows removed by filter" under an index scan
     means the index serves the wrong predicate.
- A reviewer who doubts the attached plan reruns it in the scratch container — the seed
  script travels with the PR, so this is minutes, not archaeology.

## 5. Manual read (Pass 2)

What no grep catches — read for these explicitly:

- [ ] **Transaction scope** — one unit of work per business action; no user/network wait
      inside an open transaction; isolation commented where it matters
      (`../SKILL.md` standard 5)
- [ ] **NULL behaviour** stated for every predicate over a nullable column — aggregates,
      comparisons, unique constraints (standard 8)
- [ ] **Deterministic ordering under `LIMIT`** — every `ORDER BY ... LIMIT` carries a
      unique tiebreaker column, or pagination will skip and repeat rows
- [ ] **Identifier allowlists** — any dynamic `ORDER BY`/table-name choice maps through a
      static allowlist visible at the call site
- [ ] **N+1 shapes** — a loop containing a query; verify against dev query logs, fix with
      joins, batched `IN`, or window functions
- [ ] **Backfill loops bounded** — batch size, pause, and a termination condition that
      cannot spin forever on a row that never updates
- [ ] **`LIKE` inputs escaped** — `%` and `_` escaped before binding when the pattern
      carries user input
- [ ] **`IN` lists bounded** — an unbounded list from user input becomes a parse-cost and
      driver-limit bug; array binds or chunking instead
- [ ] **Timestamp policy consistent** — new columns follow the database's documented
      timezone policy, not the author's local habit
- [ ] **Test-data hygiene** — deterministic fixtures, per-test rollback or throwaway
      schema, no production dumps with PII (standard 9)
- [ ] **Schema-drift surfaces** — if the diff touches ORM models *and* migrations, the
      equivalence test still passes; if it adds a second DDL surface, that is a finding
      (standard 10)

## 6. Empirical verification

The scratch-container one-liners in `../SKILL.md` are the **reviewer's** tool, not only
the developer's. When a review thread starts arguing about NULL behaviour, truncation,
collation, or what a plan will do — stop arguing and run it. The scratch query settles in
ninety seconds what argument gets wrong across several rounds.

A review comment asserting row behaviour that was never executed is a hypothesis and must
say so. Same rule the other way: a developer's "it works" over row behaviour without an
executed check is not evidence — ask for the scratch output, or produce it yourself in
less time than the reply would take. (Oracle is the sanctioned exception: when no instance
is reachable, `../SKILL.md` allows skipping the empirical check *provided the claim is
labelled unverified* — hold the author to the label, not to the container.)

## 7. Reporting

- Every finding carries file:line, the severity from the mapping below, and the evidence
  (grep hit, plan line, scratch output) — not just an opinion.
- The report states the disposition of **every** Pass 0 hit, including the ones cleared —
  "12 hits, 11 verified bound, 1 BLOCKING at `db/queries.rs:88`" — so the next round can
  see what was checked rather than re-deriving it.
- On a follow-up round, review the fix and the prior findings, not the whole PR again —
  the `review-tier` process skill owns that rule.
- Severity comes from the mapping below and only from it — authors negotiate fixes,
  never labels.

## 8. Severity mapping (fixed — do not downgrade)

**BLOCKING**
- String-built SQL / unparameterized input anywhere a value meets a statement — an
  injection surface, whatever the author intended
- `UPDATE`/`DELETE` without `WHERE`, unintended — one statement from an empty table
- An edited applied migration — every applied database has already diverged from the file
- Blocking DDL on a large live table with no stated plan — an outage scheduled by merge
- A new table without a primary key — replication and dedup both break; a stated reason
  in the migration is the only exemption

**MAJOR**
- N+1 evidence — a loop containing a query, or query-log bursts in dev
- A new selective predicate with no index plan
- `NOT IN` over a nullable subquery
- Per-row autocommit loop
- `SELECT *` in production paths
- A relationship with no foreign key and no stated justification for its absence

**MINOR**
- sqlfluff style findings, naming, formatting
