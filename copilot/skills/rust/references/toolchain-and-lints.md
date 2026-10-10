# Rust — Toolchain, Lints, and Checked-In Policy

> Loaded from the Rust language skill (`../SKILL.md`) — standards three and five route
> here. Everything in this reference is a **file committed to the repository**: the
> point is that toolchain, lint severity, formatting, and coverage policy are diffable
> artifacts, not tribal knowledge.

> **Volatility note.** Versions and lint names below are current as of **Aug 2026 —
> re-verify before pinning**. Rust ships a stable release every six weeks and each one
> adds clippy lints; the release notes and the clippy lint list for the target version
> are the sources of truth.

---

## 1. Toolchain pinning — pin, assert, bump deliberately

Channel names float; pins do not. Two reasons an exact pin beats `channel = "stable"`:

1. **Reproducibility.** The same commit builds with the same compiler on every machine,
   forever. "Works on my rustc" disappears as a failure class, and old releases can be
   rebuilt bit-for-bit against the compiler that shipped them.
2. **Clippy lint drift.** Every stable release adds and promotes lints. Under
   `-D warnings`, an unpinned channel means CI turns red the morning a release ships —
   in whichever PR happens to be open, blaming a diff that did nothing wrong.

```toml
# rust-toolchain.toml — committed at the workspace root
[toolchain]
channel = "1.97.0"
components = ["clippy", "rustfmt"]
```

The pin alone is half a policy. The full pattern has three parts:

**Pin** — the file above. rustup reads it and installs/selects that exact toolchain for
every command run inside the workspace.

**CI assertion** — CI proves it is actually building with the pinned compiler, so a
misconfigured runner image cannot silently substitute another version:

```bash
#!/usr/bin/env bash
# ci/assert-toolchain.sh — fail the build if the running rustc is not the pinned one
set -euo pipefail

expected="$(sed -n 's/^channel *= *"\([^"]*\)".*/\1/p' rust-toolchain.toml)"
actual="$(rustc --version | awk '{print $2}')"

if [ "$actual" != "$expected" ]; then
  echo "toolchain mismatch: rust-toolchain.toml pins ${expected}, CI is running ${actual}" >&2
  exit 1
fi
echo "toolchain OK: ${actual}"
```

**Deliberate bump** — the pin moves within about four weeks of a stable release, as its
own PR, never piggybacked on a feature branch. The bump PR:

- reads the release notes for the target version (releases.rs is the fastest index);
- reads the new and promoted clippy lints and triages each: fix now, or `allow` with a
  reason in `[workspace.lints]`;
- records both in the PR description.

A toolchain bump changes what every line in the repository means to the compiler.
Bundling it with a feature makes the feature diff unreviewable — which of these
warnings came from the code, and which from the compiler?

---

## 2. MSRV policy

MSRV — the minimum supported Rust version — is a different question for applications
and libraries, and the answer goes in the manifest either way.

**Applications:** MSRV *is* the pinned toolchain. Set `package.rust-version` to match
the pin, so a contributor on an older toolchain gets one clear refusal from cargo
instead of a page of confusing compile errors deep inside a dependency:

```toml
[package]
name = "example-service"
rust-version = "1.97.0"
```

**Libraries:** MSRV is a deliberate, older choice — a promise to downstream users on
slower upgrade cadences. Two rules make the promise real:

- The chosen MSRV is **tested in CI on exactly that toolchain** (a dedicated job that
  installs the MSRV toolchain and runs the build + tests). An untested MSRV is a
  claim, not a property; it silently breaks the first time a contributor uses a newer
  language feature.
- Bumping a library's MSRV is a versioning event, called out in the changelog.

```bash
#!/usr/bin/env bash
# ci/msrv.sh — libraries only: prove the declared MSRV actually builds and passes
set -euo pipefail

msrv="$(sed -n 's/^rust-version *= *"\([^"]*\)".*/\1/p' Cargo.toml)"
rustup toolchain install "$msrv" --profile minimal
cargo "+${msrv}" check --workspace --locked
cargo "+${msrv}" test --workspace --locked
```

**Clippy and MSRV:** clippy reads `package.rust-version` and suppresses suggestions
that need a newer compiler. The `msrv` key in clippy.toml exists **only to override**
that — normally it is omitted, so there is one MSRV declaration, not two that can
disagree.

---

## 3. Edition 2024 idioms

Edition 2024 is the baseline (available since Rust 1.85). The changes that carry rules
with them:

| Edition 2024 change | Rule it implies |
|---------------------|-----------------|
| `env::set_var` / `env::remove_var` are `unsafe` | Process-global env mutation is now visibly a soundness question — it races with concurrent reads. Do not wrap calls in `unsafe` to preserve old test habits; design config injection instead — see `testing.md` |
| Attribute form is `#[unsafe(no_mangle)]` | Exported-symbol attributes are themselves unsafe to apply; they belong in FFI island crates with the rest of the audited surface, nowhere else |
| RPIT captures all in-scope lifetimes | `-> impl Trait` now borrows by default; opt out with `+ use<>` when the return type must not capture, and say why at the site |
| `unsafe_op_in_unsafe_fn` warns by default | Raise it to `deny` (§4): an `unsafe fn` body gets no blanket license — every unsafe operation needs its own block and SAFETY comment |
| Let-chains are stable | Prefer `if let Some(cfg) = load() && cfg.enabled { .. }` over nested if-let pyramids; flatter control flow, same semantics |

The RPIT change deserves the one worked example, because it inverts the old default:

```rust
impl Registry {
    // Edition 2024: `-> impl Trait` captures `&self`'s lifetime by default.
    // `+ use<>` opts out — the compiler now proves the returned iterator
    // borrows nothing from the registry, so callers may hold it freely.
    fn snapshot_ids(&self) -> impl Iterator<Item = u64> + use<> {
        self.ids.clone().into_iter()
    }
}
```

Two process rules:

- **Edition migration is its own PR** with no behaviour change: run
  `cargo fix --edition`, bump `edition = "2024"`, review the mechanical diff, done.
  Mixing an edition migration into feature work buries real changes in churn.
- **Sibling repos of one product converge on one edition.** Cross-repo diffs and
  copy-paste between them should compare like with like.

---

## 4. `[workspace.lints]` — the complete template

This is the centerpiece: the mechanism behind "zero unwrap/expect/panic in production
paths" and every other build-enforced rule in the skill. Workspace-level `[lints]` has
been stable since Cargo 1.74. Severity lives **here**, in the manifest, so local
`cargo clippy` sees exactly what CI sees; CI's `-D warnings` is a backstop for stray
warnings, not the policy.

```toml
# Root Cargo.toml
[workspace.lints.rust]
unsafe_code = "deny"                     # per-crate opt-out ONLY in FFI island crates
unsafe_op_in_unsafe_fn = "deny"
unreachable_pub = "warn"
missing_debug_implementations = "warn"

[workspace.lints.clippy]
unwrap_used = "deny"
expect_used = "deny"
panic = "deny"
todo = "deny"
unimplemented = "deny"
dbg_macro = "deny"
print_stdout = "deny"                    # allow in CLI output modules where stdout IS the interface
print_stderr = "deny"
await_holding_lock = "deny"
undocumented_unsafe_blocks = "deny"
wildcard_enum_match_arm = "warn"
pedantic = { level = "warn", priority = -1 }
# Initial pedantic allows — remove as the codebase absorbs pedantic:
module_name_repetitions = "allow"
must_use_candidate = "allow"
missing_errors_doc = "allow"
```

Every member crate opts in — without this, the crate builds under **no policy at all**:

```toml
# Each member crate's Cargo.toml
[lints]
workspace = true
```

Notes on the deliberate choices:

- `unsafe_code = "deny"` at the workspace level with a **per-crate opt-out** — an FFI
  island crate carries `#![allow(unsafe_code)]` (or omits `workspace = true` for the
  rust table with its own override) plus its audit story. See `security.md`.
- `print_stdout` / `print_stderr` at deny: logging goes through the logging facade. A
  CLI whose stdout *is* the interface confines printing to one output module carrying
  `#[allow(clippy::print_stdout)]` with a one-line reason — the allow marks the
  interface, everywhere else the deny holds.
- `pedantic` at `warn` with `priority = -1` lets the named per-lint entries above it
  win, so the group can be adopted incrementally without a hundred-allow prelude.
- Review check that costs one grep: every member crate's manifest contains
  `workspace = true` under `[lints]`.

---

## 5. clippy.toml — the test exemption

This file is what makes the production panic ban practical. In a test, `unwrap()` *is*
the assertion — forcing `Result` plumbing through test bodies waters the ban down until
someone proposes removing it.

```toml
# clippy.toml — committed beside the root Cargo.toml
allow-unwrap-in-tests = true
allow-expect-in-tests = true
allow-panic-in-tests = true
# msrv = "1.85.0"   # only to override package.rust-version — normally omitted
```

- The three allowances scope `unwrap_used`, `expect_used`, and `panic` to production
  code only; `#[cfg(test)]` modules and integration tests are exempt automatically —
  no per-test attributes, no ceremony.
- `msrv` appears only when overriding `package.rust-version` (§2); duplicate
  declarations drift.
- **Rule: clippy.toml exists in every workspace** that denies the panic family.
  Without it, the first test PR either sprouts dozens of `#[allow]` attributes or
  quietly demotes the workspace lints — both are worse than the two-line file.

---

## 6. rustfmt.toml — format policy is a file

Commit a `rustfmt.toml` even if it only records defaults. The absent file does not
mean "we use the defaults" — it means **the policy is whatever each editor does**, and
the first contributor with a customized global rustfmt config reformats every file
they touch.

```toml
# rustfmt.toml
edition = "2024"
max_width = 100
```

- **Stable options only** while the toolchain pin is a stable release. Nightly-only
  options are silently skipped (or error, depending on version) under a stable
  toolchain — the committed style then quietly isn't the applied style.
- **One width across the workspace and across all sibling repos of one product.**
  Divergent widths make every cross-repo diff and code move noisy — hunks that changed
  nothing but wrapping. This is a named defect class, not a taste difference; the fix
  is a one-line PR to the outlier repo.
- Format churn belongs in dedicated PRs, same reasoning as the edition migration (§3).

---

## 7. Coverage — thresholded, ratcheted

Coverage that is measured but not thresholded is a dashboard, not a policy. The
threshold is a CI gate:

```bash
cargo llvm-cov --workspace --locked --fail-under-lines 80
```

- **Core crates hold the threshold.** Shell/bin crates may be exempted **by name, with
  a stated reason** in the CI config — a `main.rs` that only wires clap to the core
  crate does not need line coverage, and saying so explicitly beats a workspace-wide
  lower bar:

```bash
# Core crates hold the bar; the CLI shell is exempt, reason on record:
cargo llvm-cov --workspace --locked --fail-under-lines 80 \
  --ignore-filename-regex '^cli/src/main\.rs$'   # arg wiring only — no branching logic
```
- **Ratchet-only:** the threshold may rise, never fall. Lowering it to get a PR merged
  is the defect the gate exists to catch. When real coverage climbs, raise the floor
  to just under it and bank the progress.
- Runs **after** the plain test step in CI (§8): a red suite should fail fast on the
  cheap run, not inside the slower instrumented one.

---

## 8. CI gate order

Cheap, deterministic checks first; each later step assumes the earlier ones passed.

```bash
#!/usr/bin/env bash
# ci/gates.sh — the whole Rust gate sequence, in order
set -euo pipefail

bash ci/assert-toolchain.sh                                        # §1
cargo fmt --check
cargo clippy --all-targets --all-features --locked -- -D warnings
cargo test --locked
cargo llvm-cov --workspace --locked --fail-under-lines 80          # §7
cargo deny check                                                   # config: dependencies-supply-chain.md
```

- `--locked` on **every** cargo invocation: if `Cargo.lock` disagrees with the
  manifests, the build refuses instead of silently re-resolving. A silent re-resolve
  is an unreviewed dependency change riding along in CI.
- `--all-targets` makes clippy cover tests, benches, and examples — the panic-family
  exemptions from §5 still apply, everything else is held to the same bar.
- `-D warnings` here is the backstop for lints not named in §4; the named severities
  already come from `[workspace.lints]`, identical locally and in CI.
- `cargo deny check` and its `deny.toml` are specified in
  `dependencies-supply-chain.md`.

---

## 9. Checklist and anti-patterns

### Policy-file checklist

- [ ] `rust-toolchain.toml` pins an exact version with clippy + rustfmt components (§1)
- [ ] CI asserts `rustc --version` against the pin (§1)
- [ ] Toolchain bumps: own PR, within ~4 weeks of release, notes + new lints read (§1)
- [ ] `package.rust-version` set — apps match the pin; libraries test their MSRV in CI (§2)
- [ ] Edition 2024 everywhere; migrations as dedicated no-behaviour-change PRs (§3)
- [ ] `[workspace.lints]` matches the template; deviations have reasons (§4)
- [ ] Every member crate: `[lints] workspace = true` — grep it (§4)
- [ ] `clippy.toml` with the three test allowances (§5)
- [ ] `rustfmt.toml` committed; one width across the product's repos (§6)
- [ ] Coverage thresholded via `--fail-under-lines`, ratchet-only (§7)
- [ ] CI order: toolchain → fmt → clippy → test → coverage → deny, all `--locked` (§8)

### Anti-patterns

| Anti-Pattern | Problem | Solution |
|--------------|---------|----------|
| `channel = "stable"` in rust-toolchain.toml | Compiler changes under the repo; new clippy lints break CI in unrelated PRs; old commits stop rebuilding identically | Exact pin (`1.97.0`) + CI assertion + deliberate bump PR (§1) |
| Lint severities only as CI flags | Local clippy is green while CI is red; contributors learn to push-and-pray instead of trusting their editor | Severities in `[workspace.lints]`; CI `-D warnings` as backstop only (§4) |
| Nightly-only rustfmt options on a stable pin | `cargo fmt` skips or rejects them — the committed style silently isn't the applied style | Stable options only while the pin is stable (§6) |
| Coverage measured but not thresholded | The number drifts down with nobody accountable; "we track coverage" becomes decoration | `--fail-under-lines` in CI, ratchet-only (§7) |
| `[workspace.lints]` present, member crate missing `workspace = true` | That crate builds under no lint policy at all — silently, indefinitely | Every member manifest carries `[lints] workspace = true`; one grep in review (§4) |
