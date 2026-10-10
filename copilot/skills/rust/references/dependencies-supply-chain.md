# Rust — Dependencies and Supply Chain

> Loaded from the Rust language skill (`../SKILL.md`) — standard five routes here for
> dependency policy, and every "add a dependency" task loads this before touching
> `Cargo.toml`. The theme: a dependency is code you ship; policy about it lives in two
> checked-in files — `Cargo.lock` and `deny.toml` — plus a reasoning pattern for every
> exception.

> **Volatility note.** Tool behaviour and config schema below (cargo-deny in
> particular) are current as of **Aug 2026 — re-verify before pinning**. The advisory
> examples are real classes, not a current advisory feed; check RUSTSEC at task time.

---

## 1. Lockfile law

Two rules, no exceptions:

- **`Cargo.lock` is committed — in applications AND libraries.** The old advice to
  gitignore it in libraries is obsolete; the lockfile is what makes CI reproducible,
  makes advisory scans mean something (they scan the versions actually resolved, not a
  hypothetical resolution), and makes `git bisect` land on commits that still build
  with the dependencies they were tested with.
- **Every CI cargo command runs `--locked`.** If the lockfile disagrees with the
  manifests, the build refuses instead of silently re-resolving. A silent re-resolve
  in CI is an unreviewed dependency change riding along in someone else's PR — the
  exact thing review exists to prevent.

```bash
# Any CI cargo step — drift refuses the build instead of resolving through it:
cargo build --locked
cargo test --locked
```

The corollary for review: **the `Cargo.lock` diff is the real dependency diff.** A
one-line `Cargo.toml` change that brings forty transitive crates shows its true size
only in the lockfile. Review it like code, because it is code you will ship.

---

## 2. Exact-pin vs caret — decide once, write it down

There are two coherent version-requirement policies. A workspace picks **one**, and
records the choice where the next contributor will trip over it — a header comment in
the root manifest:

```toml
# Dependency policy: caret requirements; Cargo.lock is the real pin.
# Version bumps arrive as their own PRs via `cargo update`, reviewed like code.
[workspace.dependencies]
tokio = { version = "1", features = ["rt-multi-thread", "macros", "signal"] }
thiserror = "2"
anyhow = "1"
```

**Default: caret + committed lockfile.** The lockfile does the actual pinning; caret
requirements keep semver-compatible security fixes one `cargo update -p <crate>` away.
This is the policy to use unless there is a stated reason not to.

```bash
# The bump under the caret policy — deliberate, reviewed, its own PR:
cargo update -p tokio
cargo test --locked
```

**Exact-pin (`=x.y.z`) — a frozen snapshot** — is legitimate for release-critical
repos, but the written policy must state its price: **no semver-compatible security
fixes arrive without a manual bump.** That converts advisory scanning from a nicety
into a scheduled obligation. An exact-pin policy is only complete with all three of:

1. cargo-deny advisories on **every PR** (§3), and
2. a **weekly cron** run of the same check — because with frozen versions, no PR
   activity means no scanning, and advisories do not wait for your next PR, and
3. a **declared thaw cadence** — e.g. a monthly bump PR that walks pins forward —
   so the snapshot ages by decision, not by neglect.

**Mixing the two policies in one workspace is the worst of both:** some crates float,
some freeze, and nobody can state which property — freshness or reproducibility — the
workspace actually has. The manifest header comment is what prevents the drift.

---

## 3. deny.toml — the complete template

cargo-deny runs all four checks — advisories, licenses, bans, sources — on every PR.
The complete working template:

```toml
# deny.toml — cargo-deny policy; all four checks run in CI (`cargo deny check`)
[graph]
all-features = true

[advisories]
version = 2
yanked = "deny"
# Every ignore is a three-part claim — see the reasoning pattern in this file.
ignore = [
    { id = "RUSTSEC-2026-0000", reason = "reachable only via the crate's `net` feature, which we do not enable (default-features = false); remove when upstream ships the fixed 0.9 line or when the crate is dropped" },
]

[licenses]
version = 2
allow = [
    "MIT",
    "Apache-2.0",
    "Apache-2.0 WITH LLVM-exception",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "Zlib",
    "Unicode-3.0",
]
# Copyleft never enters via the allowlist — only as a deliberate, per-crate exception
# whose reason lives in the PR that adds it:
# exceptions = [{ name = "some-gpl-tool", allow = ["GPL-3.0"] }]

[bans]
multiple-versions = "warn"
wildcards = "deny"
deny = [
    # Worked example: the TLS decision is rustls. A second TLS stack — and a C
    # toolchain dependency — must not re-enter the graph through someone's
    # transitive default feature.
    { crate = "openssl-sys", reason = "TLS is rustls by decision; openssl-sys reintroduces a C build dependency and a second certificate-verification story" },
]

[sources]
unknown-registry = "deny"
unknown-git = "deny"
allow-git = []
```

**The wrapper-exception mechanism.** When a ban genuinely needs an exception, do not
delete the ban — scope it. cargo-deny's `wrappers` field permits the banned crate
*only* when it enters through a named parent:

```toml
[bans]
deny = [
    { crate = "openssl-sys", wrappers = ["legacy-payment-client"] },
]
```

This re-asserts the condition the exception depends on. It reads: banned everywhere,
except beneath `legacy-payment-client`. The day that client is removed — or the day a
*second* dependency starts pulling `openssl-sys` — the recorded excuse stops being
true and the build fails loudly, instead of the ban silently eroding into decoration.

**Sources stay closed.** `unknown-registry` and `unknown-git` at deny with
`allow-git = []` means every crate comes from crates.io. A git dependency is an
unreviewed moving target (and a force-push can rewrite your build); when one is
genuinely needed, adding it to `allow-git` is a review event with a reason, and the
exit plan is to upstream the patch or vendor the code deliberately.

---

## 4. The advisory-ignore reasoning pattern

An ignore entry is not a suppression — it is a **three-part claim**, and all three
parts go into the `reason` string where the next reader (and the next audit) finds
them:

1. **Path** — which code path in the dependency the advisory affects.
2. **Why unreachable** — why that path is unreachable or unexploitable *here*: the
   feature is not enabled, the input is never attacker-controlled, the API is never
   called.
3. **Exit trigger** — the event that removes the ignore: the fixed version to bump
   to, or the removal of the dependency.

Model entry:

```toml
[advisories]
ignore = [
    { id = "RUSTSEC-2026-0123", reason = "segfault in the zlib-ng backend's streaming decompressor; we compile flate2 with the rust-backend feature only, so the affected C path is not built; remove when flate2 0.5 lands or when archive import is dropped" },
]
```

**Naked ignores are review-blocking** — an entry with no reason, or a reason that
names no path and no exit trigger ("false positive", "not relevant to us"). An ignore
without an exit trigger is permanent by default, and permanent ignores are how a
scanner goes quiet one entry at a time while still showing green.

The claim is also checkable: part two usually reduces to something mechanical —
`cargo tree -e features` showing the feature off, or a grep proving the API is never
called. If it does not reduce to something checkable, it is a guess, not a claim.

---

## 5. Unmaintained dependencies

A RUSTSEC "unmaintained" notice is **a finding with a security clock, not noise**. The
crate works today exactly as well as yesterday — but the next real advisory against it
will have no fix coming, and the migration you deferred becomes an emergency with a
CVE number attached.

Policy:

- An unmaintained notice on anything in the tree gets a **replacement ticket within
  one release cycle**, naming the successor (maintained fork, alternative crate, or
  removal of the need).
- The ticket is allowed to conclude "accept and vendor" — but that is a decision with
  an owner, not a default reached by silence.

Worked example class: an **archived YAML crate** still in the tree. The tactical fix
is migrating to a maintained fork; the real question the ticket must ask is whether
the format is still earning its place — a config surface small enough may be better
served by TOML or JSON, which removes the dependency class instead of rotating it.

---

## 6. Bundled C dependencies

A `bundled` feature (libsqlite3-sys and friends) compiles a **vendored copy of the C
library into your binary**. That is often the right call — reproducible builds, no
system-library skew — but it moves patch responsibility to you: you ship the C
library's CVEs and its data-integrity bugs on *your* rebuild schedule, not your
distro's.

- Track the **C project's** advisories and release notes, not only RUSTSEC — the Rust
  wrapper's advisory feed does not mirror every upstream fix.
- Bump on **data-integrity fixes even without a CVE number**. The defining example:
  SQLite 3.51.3 (2026-03) fixed a WAL-reset database-corruption bug. A `bundled`
  libsqlite3-sys pinned to an earlier SQLite ships that corruption bug until the pin
  moves and the binary is rebuilt — no server-side patching can help, because the
  library is inside your artifact.
- Record **which C version the current pin bundles** (the libsqlite3-sys release
  notes state it; rusqlite 0.40's pin is checked the same way) so "are we affected?"
  is answerable from the repo in one minute, not by archaeology.

---

## 7. Tooling matrix

| Tool | Runs | Covers | Notes |
|------|------|--------|-------|
| cargo-deny | CI, every PR | advisories, licenses, bans, sources | The gate. Config is `deny.toml` (§3); wired into the CI order in `toolchain-and-lints.md` |
| cargo-audit | Ad hoc, local | advisories only | Needs only `Cargo.lock` — fast triage on any checkout, no build |
| cargo-machete | CI or pre-review | unused dependencies | Fast and syntactic; occasional false positives are silenced per-crate with `[package.metadata.cargo-machete] ignored`, with a reason |
| cargo-udeps | Ad hoc (nightly toolchain) | unused deps via the build graph | Slower, more precise; the second opinion when machete's answer is disputed |
| cargo-vet | Optional, higher-assurance tier | human audits of dependency source | For threat models that include malicious upstream; heavyweight — adopt deliberately, not by default |

An unused dependency is not clutter, it is **attack surface with no benefit**: it
still resolves, still downloads, still carries advisories, and still builds.

---

## 8. New-dependency checklist

A new dependency is a code review of someone else's repository, compressed into a
checklist. Before the `Cargo.toml` line lands:

- [ ] **Maintenance signal** — recent releases, issues being triaged, not archived;
      a crate that last released years ago is a §5 case on day one
- [ ] **Transitive weight** — `cargo tree -e normal` after adding: is the number of
      arriving crates proportionate to the problem being solved?
- [ ] **Duplicate versions** — `cargo tree -d`: no new duplicate major versions, or
      the duplication is understood and temporary
- [ ] **License** — in the `deny.toml` allowlist (§3); cargo-deny will enforce it,
      checking first avoids a dead PR
- [ ] **MSRV** — compatible with the workspace's pinned toolchain and
      `rust-version` (see `toolchain-and-lints.md`)
- [ ] **Features trimmed** — `default-features = false` where meaningful, then
      enable only what is used; default features are how second TLS stacks and
      bundled C libraries arrive uninvited
- [ ] **Lockfile diff reviewed** — the `Cargo.lock` diff is the real dependency
      diff (§1)
- [ ] **Workflow** — a new dependency is an ARCH/SECOPS trigger; the
      workflow-engine decides whether those gates fire. Adding a dependency is not
      a unilateral call.

---

## 9. Anti-patterns

| Anti-Pattern | Problem | Solution |
|--------------|---------|----------|
| `Cargo.lock` gitignored "because it's a library" | CI tests a resolution nobody ships; advisory scans check hypothetical versions; bisect lands on unbuildable commits | Commit the lockfile everywhere; `--locked` in CI (§1) |
| CI cargo commands without `--locked` | Lockfile drift silently re-resolves — an unreviewed dependency change rides along in an unrelated PR | `--locked` on every cargo invocation (§1) |
| Exact-pin without scheduled scanning | The frozen snapshot stops receiving semver-compatible security fixes, and no PR activity means no scans | Caret + lockfile by default; exact-pin only with per-PR + weekly cron scanning and a declared thaw cadence (§2) |
| Mixed pin policies in one workspace | Neither freshness nor reproducibility holds; the policy cannot even be stated | One policy, recorded in a manifest header comment (§2) |
| Naked advisory ignore | Permanent by default; the scanner goes quiet one entry at a time while showing green | Three-part reason — path, why unreachable, exit trigger (§4) |
| "Unmaintained" advisories triaged as noise | The next real advisory against that crate has no fix coming; deferred migration becomes an emergency | Replacement ticket within one release cycle (§5) |
| Bundled C library never re-bumped | You ship the C project's known bugs — including data-corruption fixes that carry no CVE | Track upstream advisories; bump on data-integrity fixes; record the bundled version (§6) |
| Git dependencies on personal forks | Unreviewed moving target; a force-push rewrites your build | `unknown-git = "deny"`, `allow-git = []`; upstream the patch or vendor deliberately (§3) |
| Wildcard version requirements (`*`) | The manifest states no intent; the resolver accepts anything it finds | `wildcards = "deny"` in `[bans]`; real requirements with the policy from §2 |
