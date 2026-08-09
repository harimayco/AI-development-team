# SQL Dialect — MySQL

The **developer view** of MySQL (InnoDB assumed throughout): the queries, DDL and
migrations you write, and where this engine's behaviour diverges from what the SQL
standard — or PostgreSQL habit — leads you to expect. The shared standards —
parameterized-only SQL, versioned migrations, expand-migrate-contract,
EXPLAIN-before-merge — live in `../SKILL.md` and are not restated here. Engine
operation (replication topology, backup infrastructure, buffer-pool sizing, online
schema-change execution at scale) belongs to /dba — handoffs are named where a topic
crosses that line. Embedded engine → `sqlite.md`; the review checklist that consumes
this file is `sql-review.md`.

> **Volatility note.** MySQL now ships two lines: LTS (long support, no behaviour
> churn) and calendar-versioned Innovation releases. Feature availability differs
> per line and per point release. Standing rule: **verify the actual server version
> before using a version-gated feature** — `SELECT VERSION();` — on the server the
> SQL will actually run.

---

## 1. Version status

| Line | Status | Notes |
|---|---|---|
| 8.0 | **EOL April 2026** | Migration off it is overdue, not optional |
| 8.4 LTS | The baseline | What "MySQL" should mean in a production repo today |
| 9.7 LTS | Current LTS | Newer features; verify managed-platform availability |
| 26.x Innovation | Calendar-versioned (July 2026 onward) | Fast-moving; not a production default |

Rules:

- **Production targets an LTS line.** Innovation releases are for evaluating what's
  coming, not for running the business.
- Check every feature you rely on against the line **actually deployed** — point
  releases gate features aggressively (e.g., `ALGORITHM=INSTANT` grew abilities
  across 8.0.x point releases, §4).
- The repo declares its minimum-supported line; committed SQL targets it.

---

## 2. InnoDB clustered-PK key design

InnoDB has no heap: **the table IS the primary-key B-tree**. Rows live in PK order
inside the PK's leaf pages. Choosing a PK is choosing the table's physical layout.

```sql
CREATE TABLE orders (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  public_id  BINARY(16)      NOT NULL,
  customer_id BIGINT UNSIGNED NOT NULL,
  total      DECIMAL(12,2)   NOT NULL,
  created_at DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY uk_orders_public (public_id),
  KEY idx_orders_customer (customer_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

- **Short, monotonic PK by default** (`BIGINT UNSIGNED AUTO_INCREMENT`). Inserts
  append to the right-most leaf; the buffer pool stays warm on one edge.
- **Random UUIDv4 PKs are a layout attack on yourself**: every insert lands on a
  random leaf → page splits, half-empty pages, buffer-pool churn on the whole tree.
- **If UUIDs are required**, use time-ordered **UUIDv7**, generated in the
  application, stored as `BINARY(16)` via `UUID_TO_BIN(?)`. Caveat: the swap flag
  in `UUID_TO_BIN(uuid, 1)` reorders the *v1* time fields to make v1 sortable —
  **v7 is already time-ordered; store it without the swap flag** (swapping would
  scramble it).
- **Every secondary index carries the PK as its row pointer.** A fat PK
  (`VARCHAR(64)`, composite three-column) is silently appended to *every* secondary
  index — the whole index set inflates.
- **Covering-index math includes the hidden PK columns**: `KEY (customer_id, created_at)`
  on the table above actually contains `(customer_id, created_at, id)` — a query
  selecting only those three is index-covered without an `INCLUDE`-style clause.
- **PK values are effectively immutable**: updating a PK physically moves the row and
  rewrites every secondary-index entry. A "natural key" that can change is not a PK.
- The AUTO_INCREMENT counter persists across restarts since 8.0 (5.7 recalculated it
  from `MAX(id)`, occasionally reusing ids of deleted rows — that hazard is gone).

Covering in practice:

```sql
-- Served entirely from idx_orders_customer (customer_id, created_at) + hidden PK.
-- EXPLAIN Extra says "Using index": no clustered-index visits at all.
SELECT id, created_at
FROM   orders
WHERE  customer_id = ?
ORDER  BY created_at DESC
LIMIT  20;
```

---

## 3. EXPLAIN & the optimizer

`EXPLAIN ANALYZE` (8.0.18+) runs the query and prints a tree with **actual** times
and row counts — the default diagnostic. `EXPLAIN FORMAT=JSON` adds cost detail and
attached conditions without executing.

**Access-type ranking to memorize** (per joined table, best → worst):

| Type | Meaning |
|---|---|
| `const` | At most one row, resolved at plan time (PK/unique = constant) |
| `eq_ref` | One row per outer row via PK/unique — the good join |
| `ref` | Index prefix match, several rows per outer row |
| `range` | Index range scan (`BETWEEN`, `>`, `IN`) |
| `index` | **Full index scan** — the whole index, not a seek |
| `ALL` | Full table scan — on a large table, the finding |

The reading method, in order:

1. **Find the worst estimate-vs-actual drift, deepest in the tree** — below, the
   estimate of 4 rows vs 9,600 actual poisons every join choice above it. Fix the
   statistics first (`ANALYZE TABLE`, histograms), then re-read.
2. **Hunt `ALL` and `index` access on large tables** — full scans where the
   predicate deserved a seek; check the predicate is index-usable at all (§9).
3. **`loops=N` in the thousands on an inner lookup** — an N+1 inside one plan,
   usually a wrong join order caused by the drift in step 1.
4. **Judge "Using filesort" / "Using temporary" by the rows flowing in** — an
   explicit sort pass or intermediate table; 50 rows is noise, 5M is the finding.
5. **Check `key_len`** against the index definition — shorter than expected means
   only a prefix of the composite index is usable (a type/collation mismatch, or a
   range predicate cutting off the columns after it).

```text
-> Nested loop inner join  (cost=2.15 rows=4)
   (actual time=0.12..3201.4 rows=48211 loops=1)        -- estimate 4 vs actual 48k
    -> Table scan on o  (cost=1.02 rows=4)
       (actual time=0.05..91.8 rows=9600 loops=1)       -- type ALL where a range was expected
    -> Index lookup on i using idx_items_order (order_id = o.id)
       (actual time=0.29..0.31 rows=5 loops=9600)       -- inner side executed 9,600 times
```

- `Extra` fine print: **"Using index"** = covering, index-only; **"Using index
  condition"** = index condition pushdown — the predicate ran inside the storage
  engine, good but *not* covering. Don't confuse the two.
- **Optimizer hints are a last resort** and always carry the reason:

```sql
SELECT /*+ INDEX(o idx_orders_customer) */   /* optimizer picks ALL after the
  o.id, o.total                                 stats reset; remove when fixed —
  FROM orders o                                 see the incident ticket */
WHERE o.customer_id = ? AND o.created_at >= ?;
```

- **Histograms** fix skewed estimates on *non-indexed* predicate columns (an index
  already provides statistics):

```sql
ANALYZE TABLE orders UPDATE HISTOGRAM ON status, country WITH 64 BUCKETS;
```

---

## 4. Online DDL — the decision ladder

Three algorithms, in order of preference: **INSTANT** (metadata-only) → **INPLACE**
(no full copy, table stays writable) → **COPY** (full rewrite, writes blocked).

| Change | Best algorithm | Since |
|---|---|---|
| Add column (last position) | INSTANT | 8.0.12 |
| Add column (any position), drop column | INSTANT | 8.0.29 |
| Set/drop column default | INSTANT | 8.0.12 |
| Rename column | INSTANT | 8.0.28 |
| Add/drop index | INPLACE | — |
| Change column type, charset, PK | COPY | — |

**THE RULE: every ALTER states its expected algorithm explicitly**, so a wrong
assumption fails loudly instead of silently copying a 400M-row table:

```sql
ALTER TABLE orders
  ADD COLUMN region_id BIGINT UNSIGNED NULL,
  ALGORITHM=INSTANT, LOCK=NONE;
-- If INSTANT is impossible, this errors immediately — it does NOT fall back.
```

- **Even INSTANT needs a brief exclusive metadata lock.** One long-running query or
  open transaction on the table blocks the ALTER — and the waiting ALTER then
  blocks *everything else* on that table.
- INSTANT changes are budgeted: after **64** instant column changes a table needs a
  rebuild before the next one — plan consolidating rebuilds, don't discover this in
  a deploy window.
- **Table-rewrite changes on big tables** (type changes, charset migrations, PK
  changes) go through `gh-ost` or `pt-online-schema-change`; running those safely is
  /dba's — your part is the expand-migrate-contract sequencing per `../SKILL.md`.

Pre-flight before altering a hot table:

```sql
SET SESSION lock_wait_timeout = 10;   -- fail the ALTER, not the workload
-- Anything long-running holding the table? Kill it or wait it out first:
SELECT id, time, state, LEFT(info, 80) AS query
FROM   information_schema.processlist
WHERE  time > 60 AND info IS NOT NULL;
```

---

## 5. Replication-safe writes

Row-based binlog (`binlog_format=ROW`, the default) stays on — it is what makes
replicas apply exactly what happened. Your writes still decide whether replicas keep
up:

- **One giant transaction stalls replica apply.** A 10M-row `UPDATE` is one binlog
  event group; replicas apply it single-file while lag climbs. **Backfills are
  batched**, 1k–10k rows per transaction, checking lag between chunks:

```sql
-- One chunk; the loop lives in the migration runner or a script.
-- Repeat until ROW_COUNT() = 0, committing between chunks and checking
-- replica lag (Seconds_Behind_Source) before each next chunk:
UPDATE orders
SET    normalized_status = UPPER(status)
WHERE  normalized_status IS NULL
LIMIT  5000;
```

- **Purges are chunked `DELETE`s** for the same reason — and under RR a `DELETE`
  scanning an unindexed predicate locks every scanned row, not just the deleted
  ones (§7):

```sql
DELETE FROM audit_log
WHERE  created_at < NOW() - INTERVAL 90 DAY
LIMIT  5000;   -- repeat until ROW_COUNT() = 0, committing between chunks
```

- **DDL auto-commits your open transaction.** Atomic DDL (8.0+) makes the DDL
  statement itself crash-safe, but it still commits any pending DML on the
  connection first — **never mix DML and DDL inside one intended transaction**; the
  "rollback" you're counting on will quietly not include the DML.
- **AUTO_INCREMENT gaps are normal** — rollbacks, `INSERT ... ON DUPLICATE KEY`,
  batch allocation, replication all create them. Any logic that assumes gap-free or
  contiguous ids is broken by design; never build invoice numbering on it.

---

## 6. The utf8mb4 law

- **Always `utf8mb4`.** MySQL's `utf8` is an alias for the legacy 3-byte `utf8mb3` —
  it rejects emoji and any supplementary-plane character. Treat `utf8` in a schema
  as a bug, full stop.
- **One collation per schema** — default `utf8mb4_0900_ai_ci` (8.0+). Two columns
  with different collations compared in a join predicate force an implicit
  conversion and **the index on the converted side is dead**. Mixed collations
  arrive silently via copied DDL and old dumps; the review catches them.

```sql
-- "Illegal mix of collations" — or a silent conversion that kills one side's index.
-- orders.customer_code is utf8mb4_0900_ai_ci; the legacy table came from an old dump:
SELECT o.id
FROM   orders o
JOIN   legacy_customers c ON c.code = o.customer_code;

-- Fix the column, not the query:
ALTER TABLE legacy_customers
  MODIFY code VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci,
  ALGORITHM=COPY, LOCK=SHARED;   -- charset/collation changes rewrite — plan per §4
```
- **Index-key-length math**: InnoDB's key limit is 3072 bytes (DYNAMIC row format);
  utf8mb4 is 4 bytes/char worst-case → **`VARCHAR(768)` is the widest fully
  indexable column**. Beyond that, a prefix index — and a prefix index cannot serve
  `ORDER BY` or be a covering index:

```sql
ALTER TABLE pages ADD KEY idx_pages_url (url(255)),
  ALGORITHM=INPLACE, LOCK=NONE;
```

---

## 7. REPEATABLE READ gotchas

InnoDB's default isolation is **snapshot-based REPEATABLE READ**, and it is not what
the name suggests:

- **Plain `SELECT`s read your snapshot. Locking reads (`FOR UPDATE`, `FOR SHARE`)
  and DML read the LATEST committed rows.** The classic check-then-update anomaly:

```sql
-- Session A                                 -- Session B
START TRANSACTION;
SELECT qty FROM stock WHERE sku = 'A1';      -- snapshot says 10
                                             -- B: UPDATE stock SET qty = 0
                                             --    WHERE sku = 'A1'; COMMIT;
UPDATE stock SET qty = qty - 1
WHERE sku = 'A1';                            -- reads the LATEST row: 0 - 1 = -1
COMMIT;                                      -- oversold, no error raised
```

  The snapshot justified a write that executed against different data. Fix: make
  the read a lock (`SELECT ... FOR UPDATE`), or make the write self-guarding
  (`AND qty >= 1`) and check the affected-row count.
- **Gap and next-key locks**: a locking read or DML on a scanned *range* locks the
  gaps between index records too — inserts into those gaps block. On insert-heavy
  workloads this produces deadlocks that look spurious; retry deadlock errors
  (1213) by re-running the whole transaction.
- **RR is not serializable — write skew exists.** Two transactions each read a
  constraint's inputs, each write a different row, both commit, the invariant is
  gone. Guard invariants with locks or unique keys, not with reads.
- **The snapshot starts at the first read, not at `START TRANSACTION`** — unless you
  say `START TRANSACTION WITH CONSISTENT SNAPSHOT`. Don't reason about "transaction
  start time" from where the `BEGIN` sits in the code.
- **Job-queue claims use `FOR UPDATE SKIP LOCKED`** (8.0+): competing workers skip
  each other's locked rows instead of convoying. `NOWAIT` is the fail-fast variant:

```sql
START TRANSACTION;
SELECT id, payload
FROM   jobs
WHERE  status = 'queued'
ORDER  BY created_at
LIMIT  10
FOR UPDATE SKIP LOCKED;
-- process, then per claimed id:
UPDATE jobs SET status = 'done', finished_at = NOW(6) WHERE id = ?;
COMMIT;
```

- Many production shops run **`READ COMMITTED` + row-based binlog** instead: fewer
  gap locks, fewer deadlocks, PostgreSQL-like read behaviour. Either choice is
  defensible — **decide per system and record the decision in the repo**, because
  query correctness assumptions differ between the two.

---

## 8. JSON columns

- **`->` returns JSON; `->>` unquotes to text.** `data->'$.id'` compared to a number
  compares JSON values; `data->>'$.id'` compares strings — pick deliberately and
  cast explicitly.
- **Multi-valued indexes** (8.0.17+) index JSON *arrays* — served by `MEMBER OF`,
  `JSON_CONTAINS`, `JSON_OVERLAPS`:

```sql
CREATE INDEX idx_orders_tags
  ON orders ((CAST(data->'$.tags' AS CHAR(64) ARRAY)));

SELECT id FROM orders WHERE 'urgent' MEMBER OF (data->'$.tags');
```

- **The portable scalar pattern**: a generated `STORED` column + a normal index.
  Functional indexes over JSON paths work but have collation/type subtleties; the
  generated column is explicit, inspectable, and survives ORM introspection:

```sql
ALTER TABLE orders
  ADD COLUMN customer_ref BIGINT UNSIGNED
    GENERATED ALWAYS AS (CAST(data->>'$.customer.id' AS UNSIGNED)) STORED,
  ADD KEY idx_orders_customer_ref (customer_ref),
  ALGORITHM=INPLACE, LOCK=NONE;
```

- **`JSON_TABLE` shreds arrays into rows** for joins and aggregation — the readable
  alternative to nested JSON function calls:

```sql
SELECT o.id, jt.sku, jt.qty
FROM   orders o,
       JSON_TABLE(o.data, '$.lines[*]'
         COLUMNS (sku VARCHAR(32) PATH '$.sku',
                  qty INT         PATH '$.qty')) AS jt
WHERE  o.created_at >= ?;
```

- **Rule: no JSON path appears in a `WHERE` without an index plan** — a multi-valued
  index, a generated column, or a written justification of why the scan is fine.

---

## 9. Footguns

| Footgun | Why it bites | Rule |
|---|---|---|
| Implicit numeric conversion | `WHERE varchar_col = 123` casts the **column**: `'123abc' = 123` is TRUE, and the index is dead | Compare strings to strings; quote the literal, fix the type |
| Loose `sql_mode` | Without strict mode: silent truncation, zero-dates, out-of-range values clamped | Strict mode stays on (`STRICT_TRANS_TABLES`, the 8.0+ default); never relax it to "make an insert work" |
| `TIMESTAMP` | Range ends **2038-01-19**; also does implicit timezone conversion | `DATETIME(6)` + application-level UTC policy. (Verify whether the 9.x/26.x line you run extended the `TIMESTAMP` range before relying on it) |
| `ORDER BY` + `LIMIT` without a unique tiebreaker | Ties are returned in nondeterministic order — pagination shows duplicates/skips across pages | Append the PK: `ORDER BY created_at DESC, id DESC` |
| `ENUM` | Changing the value list is an ALTER; anything but appending a value rebuilds the table | A lookup table (or a `CHECK`ed VARCHAR); append-only if you must keep ENUM |
| `VARCHAR(255)` cargo cult | 255 is a fossil of the 767-byte prefix era, not a design decision; oversized rows blunt covering-index math | Size to the domain: `VARCHAR(320)` for email, `VARCHAR(2)` for country code |
| `REPLACE INTO` | It is DELETE + INSERT: fires `ON DELETE` cascades, burns AUTO_INCREMENT, rewrites the clustered row | `INSERT ... ON DUPLICATE KEY UPDATE` |
| Upsert id burn | `INSERT ... ON DUPLICATE KEY UPDATE` consumes an AUTO_INCREMENT value on every conflicting attempt | Expected; size ids `BIGINT UNSIGNED` and read no meaning into gaps (§5) |
| Disabling `ONLY_FULL_GROUP_BY` | Non-aggregated selected columns return an arbitrary row per group | Keep it on (the 8.0+ default); `ANY_VALUE()` when arbitrariness is genuinely intended |
| `utf8` charset | It's `utf8mb3` — 3-byte, rejects emoji (§6) | `utf8mb4` only |
| Mixing DML and DDL in one "transaction" | DDL auto-commits the pending DML first (§5) | Separate migrations from data changes |
