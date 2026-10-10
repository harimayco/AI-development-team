# SQL Dialect — Oracle

The **developer view** of Oracle Database: SQL, PL/SQL and migrations as you write
them, and the places where Oracle's semantics diverge sharply from every other
engine in this directory. The shared standards — parameterized-only SQL, versioned
migrations, expand-migrate-contract, EXPLAIN-before-merge — live in `../SKILL.md`
and are not restated here. Engine operation (RAC administration, AWR analysis, Data
Guard, RMAN) belongs to /dba — handoffs are named where a topic crosses that line.
Embedded engine → `sqlite.md`; the review checklist that consumes this file is
`sql-review.md`.

> **Volatility note.** Oracle renamed the current line: **Oracle AI Database 26ai**
> is the 23ai family consolidated and rebranded (on-prem Linux GA January 2026),
> while **19c remains widespread in the field**. The gap between those two is a
> decade of features. Standing rule: **verify the actual server version before
> using a version-gated feature** — the field is full of 19c instances behind
> 26ai-era documentation.

---

## 1. Version status

| Line | Status | Developer-visible highlights |
|---|---|---|
| 19c | Long-term release, still the field majority | Baseline: no `BOOLEAN` in SQL, no DDL `IF NOT EXISTS` |
| 23ai family / **26ai** | Current (26ai on-prem GA Jan 2026) | `BOOLEAN`, DDL `IF [NOT] EXISTS`, JSON-Relational Duality views, SQL domains, `SELECT` without `FROM dual` |

Version check — run it before trusting any 23ai+ feature:

```sql
SELECT banner_full FROM v$version;
-- If v$ views aren't granted to your schema:
SELECT product, version_full FROM product_component_version;
```

Rules:

- The repo declares its minimum-supported Oracle release; committed SQL and PL/SQL
  target it. A 23ai+ feature enters only when the declared minimum moves.
- Treat "we're on 26ai" claims as unverified until the query above says so — the
  rebrand makes version conversations ambiguous.

---

## 2. Bind variables — the hard-parse doctrine

Oracle caches parsed statements in the shared pool keyed by the SQL **text**.
Literals make every statement unique: each execution **hard-parses** — CPU burned,
shared-pool memory fragmented, library-cache latch contention under load. This is
an Oracle-specific *performance* reason on top of the injection rule in `../SKILL.md`:

```sql
-- Floods the shared pool: one cached cursor per customer id, all plans recomputed
SELECT * FROM orders WHERE customer_id = 48211;

-- One cursor, reused by every execution:
SELECT * FROM orders WHERE customer_id = :customer_id;
```

- **`cursor_sharing=FORCE` is a band-aid, not policy** — it rewrites literals into
  binds server-wide, masking the offending code while degrading plan quality for
  legitimately literal predicates. If someone proposes it, the actual fix is the
  code that isn't binding.
- **Bind peeking + adaptive cursor sharing**: the optimizer peeks at the first
  execution's bind values and may keep that plan for very different values.
  Adaptive cursor sharing repairs this over time for skewed predicates, but only
  after bad executions. **The sanctioned exception**: for a heavily skewed,
  low-cardinality predicate (`status = 'ARCHIVED'` at 0.1% of rows vs `'ACTIVE'` at
  90%), a deliberate literal — with a comment saying why — gives each value its own
  plan:

```sql
SELECT o.id, o.total
FROM   orders o
WHERE  o.status = 'ARCHIVED'   /* literal on purpose: heavy skew; a shared
                                  bind plan full-scans for this rare value */
AND    o.region_id = :region_id;
```

---

## 3. PL/SQL discipline

**SQL-first**: one set-based statement beats any loop — the engine crosses the
SQL↔PL/SQL boundary once instead of per row. Reach for PL/SQL when rows genuinely
must be iterated, and then never row-by-row:

```sql
DECLARE
  CURSOR c_src IS
    SELECT id, status FROM staging_orders;
  TYPE t_rows IS TABLE OF c_src%ROWTYPE;
  l_rows       t_rows;
  bulk_errors  EXCEPTION;
  PRAGMA EXCEPTION_INIT(bulk_errors, -24381);
BEGIN
  OPEN c_src;
  LOOP
    FETCH c_src BULK COLLECT INTO l_rows LIMIT 1000;
    EXIT WHEN l_rows.COUNT = 0;
    BEGIN
      FORALL i IN 1 .. l_rows.COUNT SAVE EXCEPTIONS
        UPDATE orders o
        SET    o.status = l_rows(i).status
        WHERE  o.id = l_rows(i).id;
    EXCEPTION
      WHEN bulk_errors THEN
        FOR j IN 1 .. SQL%BULK_EXCEPTIONS.COUNT LOOP
          audit_pkg.log_row_error(
            p_row_id     => l_rows(SQL%BULK_EXCEPTIONS(j).ERROR_INDEX).id,
            p_error_code => SQL%BULK_EXCEPTIONS(j).ERROR_CODE);
        END LOOP;
    END;
  END LOOP;
  CLOSE c_src;
  COMMIT;               -- once, at the end — never inside the loop
END;
/
```

- **`BULK COLLECT ... LIMIT` + `FORALL`**, never a row-by-row cursor loop. The
  `LIMIT` bounds PGA memory; `SAVE EXCEPTIONS` + `SQL%BULK_EXCEPTIONS` keeps one
  bad row from aborting the batch while still *recording* it.
- **`WHEN OTHERS THEN NULL` is BANNED.** It converts every failure — deadlock,
  space exhaustion, logic bug — into silent data corruption. Handle **named**
  exceptions or re-raise (`RAISE;`).
- **`RAISE_APPLICATION_ERROR` with named constants**, collected in one package spec
  so error codes cannot collide or drift:

```sql
CREATE OR REPLACE PACKAGE app_errors AS
  c_order_not_found  CONSTANT PLS_INTEGER := -20001;
  c_stock_exhausted  CONSTANT PLS_INTEGER := -20002;
END app_errors;
/
```

- **Packages as modules**: the spec is the API, the body is the implementation —
  callers recompile only when the spec changes. Loose standalone procedures become
  an unversioned soup.
- **Autonomous transactions** (`PRAGMA AUTONOMOUS_TRANSACTION`) are for **audit
  logging only** — a log row that survives the caller's rollback. Any other use is
  a transaction-integrity hole wearing a convenience costume.
- **Invoker vs definer rights** (`AUTHID CURRENT_USER` vs the `DEFINER` default) is
  a deliberate, per-package choice: definer rights run with the owner's privileges
  (a privilege escalation surface); invoker rights follow the caller. State it,
  don't inherit it by omission.
- **No `COMMIT` inside loops** — it breaks restartability, forces redo per chunk,
  and invites ORA-01555 (snapshot too old) against your own cursor.

---

## 4. Optimizer & diagnosis

The tool of record is `DBMS_XPLAN.DISPLAY_CURSOR` with runtime statistics:

```sql
SELECT /*+ GATHER_PLAN_STATISTICS */
       o.id, o.total
FROM   orders o
WHERE  o.status = :status AND o.region_id = :region_id;

SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST'));
```

**E-Rows vs A-Rows drift is the signal** — the same reading method as every engine:
find the worst estimate-vs-actual gap deepest in the plan, because it poisons every
join decision above it:

```text
| Id | Operation                    | Name          | E-Rows | A-Rows | Buffers |
|  0 | SELECT STATEMENT             |               |        |  48211 |   92084 |
|  1 |  NESTED LOOPS                |               |     12 |  48211 |   92084 |  <- chose NL believing 12 rows
|* 2 |   TABLE ACCESS FULL          | ORDERS        |      4 |   9600 |    3104 |  <- the deepest lie: 4 vs 9600
|* 3 |   INDEX RANGE SCAN           | IDX_ITEMS_ORD |      3 |      5 |   88980 |  <- executed 9,600 times
```

- Fix the deepest drift first — usually stale statistics or correlated predicates.
  **After bulk loads, gather stats before judging any plan**:

```sql
BEGIN
  DBMS_STATS.GATHER_TABLE_STATS(ownname => USER, tabname => 'ORDERS');
END;
/
```

- **SQL Monitor** (`DBMS_SQL_MONITOR` / the report views) shows long-running
  statements live, per plan step — the first stop when a query is *currently* slow.
- **AWR, ASH, SQL Plan Baselines and SQL profiles exist at /dba altitude.** Know the
  handoff: when a plan regresses in production and the SQL didn't change, that is a
  plan-stability conversation for /dba, not a hint-hunt in application code.

---

## 5. The `''` IS NULL flagship trap

In Oracle, the empty string **is** NULL for `VARCHAR2`. There is no such thing as a
stored zero-length string:

```sql
INSERT INTO customers (nickname) VALUES ('');   -- stores NULL

SELECT COUNT(*) FROM customers WHERE nickname = '';   -- always 0:
-- '' is NULL, and equality with NULL is never true

SELECT 'order-' || NULL FROM dual;              -- 'order-'  (!)
-- Concatenation treats NULL as empty — the OPPOSITE of PostgreSQL/standard SQL,
-- where 'order-' || NULL yields NULL. Code ported in either direction breaks here.
```

- **Never encode an empty-vs-null distinction** in an Oracle schema — it cannot be
  stored. If "known to be blank" differs from "unknown" in the domain, model it
  explicitly (a status column), don't lean on `''`.
- **`NVL`/`COALESCE` at the boundaries**: normalize at the application edge so the
  same value means the same thing on every engine the code touches.
- **Ordering**: Oracle sorts NULLs **last** on `ASC`, **first** on `DESC` — the
  reverse of some engines' defaults. Whenever NULL position matters, say it:
  `ORDER BY last_seen DESC NULLS LAST`.

---

## 6. DATE vs TIMESTAMP

**`DATE` contains time-of-day** — down to the second. The classic surprise:
`WHERE order_date = DATE '2026-08-01'` matches only midnight rows.

| Type | Holds | Timezone behaviour |
|---|---|---|
| `DATE` | Date + time to the second | None |
| `TIMESTAMP(n)` | + fractional seconds | None |
| `TIMESTAMP WITH TIME ZONE` | + explicit offset/region | Stored as entered, offset preserved |
| `TIMESTAMP WITH LOCAL TIME ZONE` | Fractional seconds, no stored offset | Normalized to the DB timezone on write, rendered in the **session** timezone on read |

- **`TRUNC(date_col)` in a predicate kills the index** — the function hides the
  column. Use half-open ranges (which also handle fractional seconds correctly):

```sql
-- Index-killing:
WHERE TRUNC(occurred_at) = DATE '2026-08-01'

-- Index-friendly, same meaning:
WHERE occurred_at >= DATE '2026-08-01'
  AND occurred_at <  DATE '2026-08-01' + 1
```

  A function-based index on `TRUNC(occurred_at)` is the fallback when the predicate
  shape can't change — but the range predicate needs no special index.
- **`SYSDATE` is the database server's OS clock; `CURRENT_DATE` is the session
  timezone's now.** They differ whenever client and server timezones do — pick one
  per codebase (servers on UTC: `SYSDATE`/`SYSTIMESTAMP`) and be consistent.

---

## 7. Sequences & identity

```sql
CREATE TABLE orders (
  id NUMBER GENERATED ALWAYS AS IDENTITY (CACHE 1000) PRIMARY KEY,
  total NUMBER(12,2) NOT NULL
);
```

- **`GENERATED AS IDENTITY` (12c+) over trigger+sequence** — the legacy
  before-insert-trigger pattern adds a row-by-row trigger cost and a detached
  sequence to manage; identity is declarative and visible in the dictionary.
- **`CACHE` sized to insert rate.** The default `CACHE 20` forces a data-dictionary
  update every 20 values on a hot table; `CACHE 1000+` for high-insert tables.
- **Gaps are guaranteed and fine** — cache flushes on instance restart, rollbacks,
  RAC distribution. Anything needing gap-free numbers (invoice sequences under some
  regimes) is its own serialized mechanism, never the PK generator.
- **RAC: `CACHE` + `NOORDER`** — each node takes its own cache range and inserts
  don't contend across nodes. `ORDER` (strict cross-node ordering) serializes the
  cluster on the sequence; only accept that cost for an explicit requirement.

---

## 8. Partitioning for developers

- **Range/interval** for time-series (interval partitioning auto-creates partitions
  on insert), **list** for discrete domains (region, tenant class), **hash** to
  spread contention when no natural key exists:

```sql
CREATE TABLE events (
  event_id    NUMBER GENERATED ALWAYS AS IDENTITY,
  occurred_at TIMESTAMP NOT NULL,
  payload     VARCHAR2(4000)
)
PARTITION BY RANGE (occurred_at)
INTERVAL (NUMTOYMINTERVAL(1, 'MONTH'))
(PARTITION p_before_2026 VALUES LESS THAN (TIMESTAMP '2026-01-01 00:00:00'));
```

- **LOCAL indexes by default** — one index segment per partition; partition drops
  and truncates stay cheap. **GLOBAL indexes only for cross-partition uniqueness**
  (a unique index that doesn't include the partition key must be global) — and then
  every partition-maintenance operation must say `UPDATE INDEXES` or the global
  index goes UNUSABLE.
- **Pruning requires the bare partition key in the predicate.**
  `WHERE occurred_at >= :d` prunes; `WHERE TRUNC(occurred_at) = :d` scans every
  partition — the same function-over-column rule as §6, with a fleet-sized blast
  radius.

---

## 9. Materialized views

- **Fast (incremental) refresh has prerequisites**: materialized view logs on every
  base table, and query-shape restrictions (aggregate MVs want `COUNT(*)` included,
  `INCLUDING NEW VALUES` on the log):

```sql
CREATE MATERIALIZED VIEW LOG ON orders
  WITH SEQUENCE, ROWID (customer_id, total)
  INCLUDING NEW VALUES;

CREATE MATERIALIZED VIEW mv_customer_totals
  REFRESH FAST ON COMMIT
  ENABLE QUERY REWRITE
AS
SELECT customer_id,
       COUNT(*)   AS order_count,
       SUM(total) AS lifetime_total
FROM   orders
GROUP BY customer_id;
```

- **`ON COMMIT` refresh taxes every writing transaction** on the base table —
  reserve it for genuinely read-critical aggregates; otherwise schedule refreshes
  (`DBMS_SCHEDULER`) at a tolerated staleness.
- **Query rewrite** lets the optimizer answer base-table queries from the MV
  transparently — powerful, and one more reason a "why is this query fast/slow"
  investigation must look at MVs.
- **Honesty check**: when the refresh rules fight you, a plain summary table
  maintained by a scheduled job is often simpler, more debuggable, and equally
  fast. An MV is a tool, not a status symbol.

---

## 10. RAC-awareness — the SQL-visible slice

Multiple instances share one database; blocks ping between nodes. What you can see
from the SQL seat:

- **Right-growing index hot blocks**: every node inserting sequence-ordered PKs
  fights over the same right-most leaf block. The options and their costs:
  - **Reverse-key index** — spreads inserts, but **kills range scans**
    (`BETWEEN`, `>`) on that column; equality only.
  - **Hash-partitioned global index** — spreads contention across N leaf "edges"
    while keeping range scans within partitions:

```sql
CREATE INDEX idx_orders_id ON orders (id)
  GLOBAL PARTITION BY HASH (id) PARTITIONS 8;
```

- **Sequence caching** (§7) is the other half of the same fix — `CACHE`+`NOORDER`
  keeps nodes out of each other's ranges.
- **Service/connection affinity**: routing one workload (or one tenant) to one node
  keeps its blocks in one buffer cache instead of shipping them between nodes.
  You'll see this as per-service connection strings — respect them.
- Interconnect analysis, GES/GCS waits, service placement → /dba.

---

## 11. Migrations under Oracle

**DDL AUTO-COMMITS — there is no transactional DDL.** A migration script that fails
halfway leaves the schema half-changed and your earlier DML committed. Consequences:

- **Every migration script must be re-runnable** (idempotent) or checkpointed. On
  23ai+, `IF [NOT] EXISTS` handles the common cases; below that, guard blocks query
  the dictionary:

```sql
DECLARE
  l_count PLS_INTEGER;
BEGIN
  SELECT COUNT(*) INTO l_count
  FROM   user_tab_columns
  WHERE  table_name  = 'ORDERS'
  AND    column_name = 'REGION_ID';
  IF l_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE orders ADD (region_id NUMBER(10))';
  END IF;
END;
/
```

- Sequence the script so each statement is safe to repeat: dictionary-guarded DDL
  first, then idempotent DML (`MERGE`, `WHERE NOT EXISTS`) — never "half DDL, some
  DML, more DDL" that a mid-script failure leaves unreconstructable.
- **Online redefinition** (`DBMS_REDEFINITION`) and **edition-based redefinition**
  exist for zero-downtime restructures — both at /dba altitude; your part is the
  expand-migrate-contract sequencing per `../SKILL.md`.

---

## 12. Footguns

| Footgun | Why it bites | Rule |
|---|---|---|
| `ROWNUM` with `ORDER BY` | `ROWNUM` is assigned **before** the sort — `WHERE ROWNUM <= 10 ORDER BY x` returns ten arbitrary rows, sorted | 12c+ `OFFSET :o ROWS FETCH NEXT :n ROWS ONLY`, or `ROW_NUMBER() OVER (ORDER BY ...)` in a subquery |
| `CHAR` comparisons | Blank-padded semantics: `CHAR(10)` `'A'` equals `'A       '`, but not when compared to a `VARCHAR2` bind | `VARCHAR2` for everything variable; `CHAR` only for true fixed-width codes |
| Quoted identifiers | `CREATE TABLE "MyTable"` is case-sensitive forever after — `"MyTable"` ≠ `MYTABLE`; every query must quote | Never quote identifiers; let them fold to upper case |
| `TO_CHAR`/`TO_DATE` without a format mask | Results depend on session `NLS_DATE_FORMAT`/`NLS_TERRITORY` — works locally, breaks per client | Always an explicit mask: `TO_DATE(:s, 'YYYY-MM-DD')`; better, `DATE`/`TIMESTAMP` literals or typed binds |
| `VARCHAR2` size boundary | 4000-byte SQL limit by default; 32k requires `MAX_STRING_SIZE=EXTENDED` (a one-way, per-DB setting) | Verify the target DB's setting before schemas assume 32k; sizes are **bytes** unless declared `CHAR` semantics |
| `''` in any predicate | It's NULL (§5) — `= ''` never matches | `IS NULL`; normalize at the boundary |
| `DATE` equality on a "day" | `DATE` carries time-of-day (§6) — midnight-only matches | Half-open ranges |
| `WHEN OTHERS THEN NULL` | Silent corruption engine (§3) | Named exceptions or re-raise; the review rejects it on sight |
