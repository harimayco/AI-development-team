# SQL Dialect — PostgreSQL

The **developer view** of PostgreSQL: the queries, DDL and migrations you write, and
the dialect-specific ways they go wrong. Shared standards — parameterized-only SQL,
versioned migrations, expand-migrate-contract, EXPLAIN-before-merge — live in
`../SKILL.md`; this file is where PostgreSQL *differs*. Engine operation belongs to
/dba — handoffs are named where a topic crosses that line. Embedded engine →
`sqlite.md`; the review checklist that consumes this file is `sql-review.md`.

> **Volatility note.** Current major is **PostgreSQL 18** (released 2025-09-25);
> managed platforms routinely run one or two majors behind. Standing rule: **verify
> the actual server version before using a version-gated feature** —
> `SHOW server_version;` — in every environment the SQL will run, not just a laptop.

---

## 1. Version status & feature gates

PostgreSQL 18 headlines for query/DDL authors: `uuidv7()`, B-tree **skip scan**,
**virtual generated columns**, asynchronous I/O (configuring `io_method` is /dba
territory), and OAuth authentication (visible only through the driver's options).

| Feature | Minimum PG | Notes |
|---|---|---|
| `ADD COLUMN ... DEFAULT` without table rewrite | 11 | Non-volatile defaults only (§8) |
| CTE inlining (`MATERIALIZED` keyword) | 12 | §6 |
| `SET NOT NULL` proven by a validated `CHECK` | 12 | Skips the full-table scan (§8) |
| `CYCLE` clause on recursive CTEs | 14 | §6 |
| `MERGE` | 15 | §2 |
| `MERGE ... RETURNING`, `WHEN NOT MATCHED BY SOURCE` | 17 | §2 |
| `uuidv7()` in core | 18 | Below 18: generate v7 in the application |
| B-tree skip scan | 18 | §4 — don't design for it |
| Virtual generated columns (`VIRTUAL`) | 18 | Computed on read; not indexable — use `STORED` for that |
| Asynchronous I/O | 18 | Server-side win; nothing to change in your SQL |

Rules:

- **The repo declares its minimum-supported PostgreSQL major** (README or migration
  tool config); all committed SQL targets it, and newer features enter only when it moves.
- The declared minimum is a floor, not a fact about production — confirm the running
  version before relying on anything version-gated.

---

## 2. Modern feature rules

### Identity, never serial

```sql
CREATE TABLE orders (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers (id),
  total       numeric(12,2) NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
```

- `serial` is a legacy macro: an `int` column + a *separate* sequence + a default.
  The sequence has its own ownership and grants — `INSERT` on the table still fails
  without `USAGE` on the sequence. Identity is SQL-standard and managed *with* the
  column: permissions, drops and renames follow the table.
- `GENERATED ALWAYS` also rejects accidental explicit-id inserts; bulk loads that
  must preserve ids say `OVERRIDING SYSTEM VALUE` explicitly.

### Primary keys

- Default: **`bigint` identity**. Never `int` for a PK — the 2.1-billion ceiling
  arrives mid-flight and the fix is a rewrite.
- When UUIDs are required: **`uuidv7()`** (18+; app-generated v7 below that). v7 is
  time-ordered — inserts land on the right-most B-tree leaf like a sequence. Random
  **v4 PKs churn the whole index**: page splits, cold buffers, WAL inflation.

### Upserts: `INSERT ... ON CONFLICT`

```sql
INSERT INTO inventory (sku, qty)
VALUES ($1, $2)
ON CONFLICT (sku) DO UPDATE
  SET qty        = inventory.qty + EXCLUDED.qty,
      updated_at = now()
RETURNING id, qty;
```

- The arbiter (`(sku)`) must match a unique index or constraint exactly.
- `EXCLUDED` is the row you *proposed*; the table alias is the row that won.
- `DO NOTHING` swallows silently — acceptable only when losing the row is the intent.

### RETURNING

`INSERT`/`UPDATE`/`DELETE` (and `MERGE` on 17+) return the affected rows in the same
round-trip — no follow-up `SELECT`, no read-your-write race. Use it whenever the
application needs generated ids, defaulted timestamps, or the post-update state.

### LATERAL for top-n-per-group joins

```sql
SELECT c.id, c.name, recent.*
FROM customers c
CROSS JOIN LATERAL (
  SELECT o.id, o.total, o.created_at
  FROM orders o
  WHERE o.customer_id = c.id
  ORDER BY o.created_at DESC
  LIMIT 3
) AS recent;
```

The inner query runs per outer row against the `(customer_id, created_at)` index;
with a small or filtered outer side this beats a window-function scan of everything.

### MERGE — and when ON CONFLICT still wins

```sql
MERGE INTO inventory i
USING staged_counts s ON i.sku = s.sku
WHEN MATCHED AND s.qty = 0 THEN DELETE
WHEN MATCHED THEN UPDATE SET qty = s.qty
WHEN NOT MATCHED THEN INSERT (sku, qty) VALUES (s.sku, s.qty);
```

- `MERGE` (15+) wins for **batch reconciliation**: update/delete/conditional insert
  in one statement, any query as source, and on 17+ `WHEN NOT MATCHED BY SOURCE`
  for rows that vanished upstream.
- `ON CONFLICT` remains the **concurrency-safe** single-row upsert (speculative
  insertion — no duplicate-key error). Concurrent `MERGE` *can* raise unique
  violations: retry them, or keep the hot path on `ON CONFLICT`.

---

## 3. Reading EXPLAIN (ANALYZE, BUFFERS)

Always `EXPLAIN (ANALYZE, BUFFERS)` — a plain `EXPLAIN` shows the plan the planner
liked, not what actually happened. The reading method, in order:

1. **Find the worst estimated-vs-actual row drift, deepest in the tree.** A node
   estimating 4 rows but producing 9,600 poisons every join choice above it — fix
   the deepest lie first (`ANALYZE`, extended statistics), then re-read the plan.
2. **Seq Scan on a large table with a selective predicate** — the index is missing,
   or the predicate's shape defeats it (§4, §12).
3. **Large "Rows Removed by Filter"** — the node read far more than it kept; the
   filter column wants into the index (or a partial index).
4. **BUFFERS: `read` vs `hit`.** High `read` counts mean cold data from disk;
   consistently cold working sets on a hot path are a design smell, not a tuning knob.
5. **External-merge sorts** (`Sort Method: external merge  Disk: ...`) — raise
   `work_mem` per statement via `SET LOCAL work_mem = '64MB'`; server default → /dba.
6. **Nested Loop with `loops=N` in the thousands** — an N+1 query hiding inside one
   plan. Usually the drift from step 1 talked the planner out of a hash join.

```text
Nested Loop  (cost=0.42..845.10 rows=12 width=64)
             (actual time=0.41..3201.9 rows=48211 loops=1)      -- ① est 12 vs actual 48k
  ->  Seq Scan on orders o                                       -- ② seq scan, selective filter
        (cost=0.00..312.00 rows=4 width=32)
        (actual time=0.09..92.3 rows=9600 loops=1)               -- ① the deepest drift: 4 vs 9600
        Filter: ((status = 'pending') AND (region_id = 7))
        Rows Removed by Filter: 190400                           -- ③ read 200k, kept 9.6k
  ->  Index Scan using idx_items_order_id on order_items i
        (cost=0.42..1.31 rows=3 width=32)
        (actual time=0.31..0.32 rows=5 loops=9600)               -- ⑥ inner side ran 9,600 times
Buffers: shared hit=1204 read=48122                              -- ④ mostly cold reads
```

Rules:

- **Analyze at realistic cardinality.** A 100-row dev table produces a different — and
  worthless — plan; plans flip at scale. Use a data-shaped fixture or masked snapshot.
- In production, `auto_explain` logs the real plans of slow statements; enabling and
  budgeting it is /dba's.

---

## 4. Index taxonomy

| Type | Serves | Example |
|---|---|---|
| **B-tree** (default) | Equality, ranges, `ORDER BY`, uniqueness | `CREATE INDEX ON orders (customer_id, created_at);` |
| **GIN** | `jsonb` containment, arrays, full-text | `CREATE INDEX ON events USING gin (payload jsonb_path_ops);` |
| **GiST** | Ranges, geometries, exclusion constraints, nearest-neighbour | `EXCLUDE USING gist (room WITH =, during WITH &&)` |
| **BRIN** | Huge, naturally ordered tables (append-only time series) | `CREATE INDEX ON metrics USING brin (recorded_at);` |
| **hash** | Equality only | Rarely worth it over B-tree; niche |
| **Partial** | A hot subset | `CREATE INDEX ON orders (created_at) WHERE deleted_at IS NULL;` |
| **Covering** | Index-only scans | `CREATE INDEX ON orders (customer_id) INCLUDE (total, status);` |
| **Expression** | Computed predicates | `CREATE INDEX ON users (lower(email));` |

- **Composite ordering rule:** equality columns first, then the *one* range column.
  `WHERE customer_id = $1 AND created_at > $2` wants `(customer_id, created_at)`.
  PG 18's skip scan can rescue a missing leading-column predicate when that column
  is low-cardinality — a safety net, **don't design for it**.
- **Expression indexes must match the query text**, casts included:
  `lower(email)` in the index serves `WHERE lower(email) = $1` and nothing else.
- **`CREATE INDEX CONCURRENTLY` — always, on a live table.** It cannot run inside a
  transaction (the migration tool must support non-transactional steps), and a
  failed run leaves an **INVALID** index: find it (`pg_index` where `NOT indisvalid`),
  `DROP INDEX CONCURRENTLY`, fix the cause, retry.

---

## 5. JSONB

**When jsonb:** genuinely dynamic attributes — per-integration payloads, user-defined
fields, schemaless ingestion you reshape later. **When columns:** anything you filter,
join, or aggregate on routinely. A field that appears in `WHERE` across the codebase
is a column wearing a costume.

```sql
-- Containment (the GIN-friendly shape):
SELECT id FROM events WHERE payload @> '{"type": "refund"}';

-- Extraction: -> keeps jsonb, ->> yields text (mind the cast in comparisons):
SELECT payload->'amount', payload->>'currency' FROM events;

-- jsonpath (12+) for structural queries:
SELECT id FROM events WHERE payload @@ '$.items[*].qty > 100';

-- Expression index on one extracted field you filter on:
CREATE INDEX idx_events_user ON events ((payload->>'user_id'));
```

- **GIN opclass choice:** default `jsonb_ops` supports existence (`?`, `?|`, `?&`)
  *and* containment; **`jsonb_path_ops`** is markedly smaller and faster but serves
  containment/`jsonpath` only. If your access pattern is `@>`, take `jsonb_path_ops`.
- **Anti-pattern: the whole row as one jsonb column.** No types, no constraints, no
  FK integrity, every index an afterthought — a document store minus its advantages.

---

## 6. CTE materialization semantics

Since PG 12, CTEs **inline** into the outer query like subqueries — predicates push
down, indexes apply — *unless* the CTE is recursive, has side effects
(`INSERT`/`UPDATE`/`DELETE ... RETURNING`), is referenced more than once, or you say
`MATERIALIZED`.

```sql
WITH hot_orders AS MATERIALIZED (
  -- Deliberate fence (measured): the planner pushed the outer predicate in and chose badly.
  SELECT id, customer_id, total
  FROM orders
  WHERE created_at > now() - interval '1 day'
)
SELECT c.name, h.total
FROM hot_orders h
JOIN customers c ON c.id = h.customer_id;
```

- `AS MATERIALIZED` is a **deliberate, commented** act — it blocks predicate pushdown,
  which is usually the opposite of what you want. Naked CTEs are for readability;
  let the planner inline them.
- Recursive template with a cycle guard:

```sql
WITH RECURSIVE org AS (
  SELECT id, parent_id, name, 1 AS depth
  FROM departments
  WHERE parent_id IS NULL
  UNION ALL
  SELECT d.id, d.parent_id, d.name, org.depth + 1
  FROM departments d
  JOIN org ON d.parent_id = org.id
  WHERE org.depth < 50              -- cycle guard: bad data must not hang the query
)
SELECT * FROM org;
```

On 14+, `CYCLE id SET is_cycle USING path` detects true cycles precisely; keep the
depth guard anyway as a belt-and-braces bound.

---

## 7. Window functions

### The default-frame gotcha

With an `ORDER BY` and no explicit frame, the frame is
`RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` — and **RANGE includes all peer
rows** of the current `ORDER BY` value:

```sql
SELECT day, amount,
       sum(amount) OVER (ORDER BY day) AS running
FROM payments;
-- day         amount  running
-- 2026-01-01      10       10
-- 2026-01-02      20       50   <- surprise: both Jan-02 rows already show 50,
-- 2026-01-02      20       50      because RANGE pulls in ORDER BY peers
-- 2026-01-03       5       55
```

For a true row-by-row running total, state the frame —
`ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` — or make the `ORDER BY` unique
(`ORDER BY day, id`): same effect, and deterministic output.

### Named windows

```sql
SELECT customer_id, created_at, total,
       row_number() OVER w              AS seq,
       lag(total)   OVER w              AS prev_total
FROM orders
WINDOW w AS (PARTITION BY customer_id ORDER BY created_at);
```

One definition, no copy-paste drift between the functions.

### Top-n-per-group

- **n = 1:** `DISTINCT ON` is the idiomatic PostgreSQL answer —

```sql
SELECT DISTINCT ON (customer_id) customer_id, id, total
FROM orders
ORDER BY customer_id, created_at DESC;
```

- **n > 1:** `row_number()` in a subquery filtered to `rn <= n`, or the LATERAL
  join from §2 when the group list is itself filtered.

---

## 8. Locking & migration-safe DDL

| ALTER | Lock / cost | Safe pattern |
|---|---|---|
| `ADD COLUMN` (no default, or non-volatile default) | Brief `ACCESS EXCLUSIVE`, **no rewrite** (11+) | Fine as-is — still under a `lock_timeout` |
| `ADD COLUMN ... DEFAULT <volatile>` (`now()`, `random()`, `uuidv7()`) | **Full table rewrite** | Add nullable → backfill in batches → set the default for new rows |
| `ADD FOREIGN KEY` / `ADD CHECK` | `ACCESS EXCLUSIVE` + full validating scan | `ADD CONSTRAINT ... NOT VALID` now; `VALIDATE CONSTRAINT` later (takes only `SHARE UPDATE EXCLUSIVE`) |
| `SET NOT NULL` | Full scan under `ACCESS EXCLUSIVE` | Add `CHECK (col IS NOT NULL) NOT VALID` → `VALIDATE` → `SET NOT NULL` (12+ sees the proof, skips the scan) → drop the CHECK |
| `ALTER COLUMN TYPE` (storage-changing) | **Rewrite** + `ACCESS EXCLUSIVE` | Don't. Expand-migrate-contract: new column, dual-write, backfill, swap |
| `CREATE INDEX` | Blocks writes for the build | `CREATE INDEX CONCURRENTLY` (§4) |
| `DROP COLUMN` | Brief; no rewrite (leaves a dead attribute) | Contract phase only — after no reader references it |

Every migration file opens with timeouts, so a blocked DDL fails fast instead of
queueing behind — and then blocking — the whole workload:

```sql
SET lock_timeout = '5s';
SET statement_timeout = '15min';
-- DDL follows. If 5s of waiting isn't enough, the migration retries off-peak;
-- it must never sit in the lock queue while traffic piles up behind it.
```

### Job queue: `FOR UPDATE SKIP LOCKED`

```sql
BEGIN;
SELECT id, payload
FROM jobs
WHERE status = 'queued'
ORDER BY created_at
LIMIT 10
FOR UPDATE SKIP LOCKED;      -- competing workers skip locked rows: no convoy

-- ...process...
UPDATE jobs SET status = 'done', finished_at = now()
WHERE id = ANY ($1);
COMMIT;
```

### Deadlocks

Lock in a **consistent order** everywhere (e.g., always ascending id; always parent
before child). When two code paths lock the same pair of rows in opposite orders,
the database picks a victim — treat error `40P01` as retryable and re-run the whole
transaction.

---

## 9. Vacuum, bloat, wraparound — the developer slice

- **Your long transactions block vacuum for everyone.** Any open transaction — busy
  or `idle in transaction` — pins the oldest visible snapshot; vacuum can't reclaim
  anything newer. Find yours:

```sql
SELECT pid, state, xact_start, left(query, 60) AS query
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND xact_start < now() - interval '10 minutes';
```

  Application rule: transactions are short; nothing holds one open across a network
  call, a queue wait, or user think-time.
- **Don't index hot-churn columns.** An UPDATE touching only non-indexed columns can
  be a HOT update — same page, no index maintenance. Index a constantly-updated
  column (`updated_at`, counters) and every update pays every index.
- **Wraparound is why autovacuum is never disabled.** Transaction ids are 32-bit;
  vacuum recycles them. "Turn autovacuum off, it slows us down" trades a slowdown
  for an eventual forced shutdown.
- Autovacuum tuning, fillfactor, bloat repair (`pg_repack`) → /dba.

---

## 10. Row-Level Security

```sql
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;   -- the owner obeys policies too

CREATE POLICY tenant_isolation ON documents
  USING      (tenant_id = (SELECT current_setting('app.tenant_id')::bigint))
  WITH CHECK (tenant_id = (SELECT current_setting('app.tenant_id')::bigint));
```

- **`USING` filters what's visible** (SELECT, and which rows UPDATE/DELETE can touch);
  **`WITH CHECK` validates what's written** (INSERT, post-UPDATE state). Omit
  `WITH CHECK` and it silently defaults to `USING` — state both.
- **Wrap `current_setting()` in a scalar subselect** (as above): it becomes an
  InitPlan, evaluated once per query instead of once per row. Helper functions used
  in policies are declared `STABLE` for the same reason:

```sql
CREATE FUNCTION app_tenant_id() RETURNS bigint
  LANGUAGE sql STABLE
  AS $$ SELECT current_setting('app.tenant_id')::bigint $$;
```

- **Owners and `BYPASSRLS` roles skip policies** unless `FORCE ROW LEVEL SECURITY`
  is set; superusers always skip. The app's connection role must be none of these.
- Rule: **RLS is defense-in-depth.** The application still filters by tenant; RLS is
  the net under it, not the primary mechanism.

---

## 11. Connection pooling from the application's seat

Under PgBouncer in **transaction mode**, your connection is a different backend every
transaction. Consequences in the SQL you write:

- **Session state doesn't survive.** `SET work_mem = ...` leaks to a stranger or
  vanishes; use `SET LOCAL` inside the transaction, every time.
- **`LISTEN`/`NOTIFY` doesn't work** through transaction pooling — a listener needs a
  session. Route it over a dedicated direct connection if you need it.
- **Session-level advisory locks** (`pg_advisory_lock`) are unusable — the unlock may
  run on a different backend. Use `pg_advisory_xact_lock`, which releases at commit.
- **Prepared statements** need protocol-level support in PgBouncer 1.21+
  (`max_prepared_statements > 0`) — verify the deployed version and setting before
  enabling driver-side preparing, or expect "prepared statement does not exist".
- Temp tables are session-scoped too — same trap.
- Pool sizing, `default_pool_size`, server-side limits → /dba.

---

## 12. Footguns

| Footgun | Why it bites | Rule |
|---|---|---|
| Cross-type comparisons | A `timestamp` column compared to a `timestamptz` value converts the **column** per-row (session-timezone dependent); int↔text joins cast one side | Match types exactly; fix the schema, not the query |
| `NOT IN` + NULL | One NULL in the subquery and `NOT IN` matches **nothing** — silently | `NOT EXISTS`, always |
| `count(col)` vs `count(*)` | `count(col)` skips NULLs — a different question | `count(*)` for row counts; `count(col)` only deliberately |
| `now()` | Frozen at transaction start — every insert in a long transaction shares one timestamp | Fine for stamps; `clock_timestamp()` when elapsed time matters |
| `timestamp` (no tz) | Meaningless without an implicit timezone convention someone will break | `timestamptz` everywhere; convert at the edges |
| `money` / `float` for currency | Locale-dependent formatting; binary rounding errors | `numeric(p,s)` — no exceptions |
| `DISTINCT` papering over join fanout | The join multiplies rows; DISTINCT hides it and the aggregates are still wrong | Fix the join (or aggregate before joining) |
| `OFFSET` pagination | `OFFSET 100000` reads and discards 100k rows, every page | Keyset: `WHERE (created_at, id) < ($1, $2) ORDER BY created_at DESC, id DESC LIMIT 25` |

---

## 13. pgvector

Index selection (HNSW vs IVFFlat), recall/latency tuning, filtered-search starvation
and reading vector-query plans are covered end-to-end by the DBA reference
`../../../data/dba/dba/references/vector-db-tuning.md` — link there, don't duplicate.
The one rule at this altitude: the distance operator in the query must match the
index's operator class, or the index is silently unused.
