# Rust — Security at Process Boundaries

Memory safety is the part Rust gives you; everything in this file is the part it does
not. Domain modelling that makes invalid states unrepresentable is `architecture.md`;
timeouts and backpressure mechanics are `async-tokio.md`; SQL injection discipline is
`sqlite-rusqlite.md` §4; the reviewer's enforcement greps are `rust-review.md`.

**Mental model.** Every byte that crosses into the process chose to be there; you did
not choose it. Security work in Rust is deciding, at each boundary, what shape a byte
sequence must prove before it becomes a domain value — and making sure secrets, once
inside, cannot leak back out through the boundaries you forgot were outputs (logs,
errors, `Debug` impls, `ps`).

---

## 1. Trust boundaries — everything entering the process is input

Enumerate them per program, explicitly, in the design doc: **argv, env, files,
sockets, IPC, and database contents**. All of them are inputs — including the
database: a row written by an older version, another tool, or an attacker with file
access is exactly as untrusted as a network packet.

- **Validation happens at the boundary, into domain newtypes** — parse, don't
  validate (`architecture.md` §3). Inside the tree, a `UserName` *is* valid; the
  proof happened once, at the edge, and the type carries it.
- **Every deserialization is capped.** serde will happily build whatever the bytes
  describe: request bodies get limits (axum: `DefaultBodyLimit::max(n)` per route or
  layer), strings get length caps at the newtype parser, recursive structures get
  explicit depth limits. An uncapped decode is an allocation bomb with a schema.
- Database reads go through the same newtype parsers (`sqlite-rusqlite.md` §6) — a
  corrupt or hostile row fails the query that touched it, it does not wander inward.

---

## 2. Secrets — location, type, logging

- **Never in argv.** Command-line arguments are visible to every user on the machine
  via `ps`/`/proc`. Secrets arrive through env, or through a file with `0600`
  permissions whose *path* is the argument.
- **Wrap them in a type that cannot leak.** The `Secret<T>` newtype makes accidental
  logging print `[REDACTED]` and makes deliberate access greppable:

```rust
/// Sensitive material that must not appear in logs, errors, or dumps.
pub struct Secret<T>(T);

impl<T> Secret<T> {
    pub fn new(inner: T) -> Self {
        Self(inner)
    }

    /// The only way to reach the inner value — call sites are greppable.
    pub fn expose(&self) -> &T {
        &self.0
    }
}

impl<T> std::fmt::Debug for Secret<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[REDACTED]")
    }
}

impl<T> std::fmt::Display for Secret<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[REDACTED]")
    }
}
```

  The `secrecy` crate is the off-the-shelf version (adds `Serialize` refusal and
  zeroization hooks); prefer it over a third hand-rolled copy.
- **tracing field policy: tokens and keys are never logged** — not at `debug!`, not
  "temporarily". The review grep that enforces this lives in `rust-review.md` §2;
  every hit where a secret-shaped value flows into a logging or `format!` call is a
  blocking finding.
- **Higher tier, when warranted:** `zeroize` for long-lived key material, so keys do
  not linger in freed memory or core dumps. Optional for short-lived bearer tokens;
  worth it for signing keys.

---

## 3. Constant-time comparison

`==` on secret bytes leaks the length of the matching prefix through timing — an
attacker who can measure response times can recover a token byte by byte. **`==` on
secret material is BLOCKING in review** (`rust-review.md` §4). Compare bearer tokens
and MACs with `subtle`:

```rust
use subtle::ConstantTimeEq;

/// Constant-time verification of a presented token against the expected one.
///
/// Length is not treated as secret (subtle returns false early on mismatched
/// lengths); the content comparison examines every byte regardless of where
/// the first difference is.
pub fn verify_token(presented: &[u8], expected: &[u8]) -> bool {
    presented.ct_eq(expected).into()
}
```

For password *hashes*, the KDF's own verifier (§6) already compares safely — do not
re-implement it.

---

## 4. The local-daemon pattern

Any locally listening service — a dev tool's backend, an editor helper, a local API —
follows this checklist. "It's only on localhost" has preceded a long list of CVEs:
every browser tab on the machine can reach localhost.

- **Bind `127.0.0.1` explicitly, never `0.0.0.0` by default.** Exposing on all
  interfaces is a configuration a user writes down deliberately, with a warning in the
  docs — not a default and not a fallback.
- **Bearer token generated on first run**, stored in a `0600` file under the app's
  data directory:

```rust
use std::io::Write;
use std::path::Path;
use rand::RngCore;

/// First-run token: 256 bits from the OS CSPRNG, hex-encoded, mode 0600.
fn generate_token_file(path: &Path) -> std::io::Result<String> {
    let mut bytes = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    let token = hex::encode(bytes);

    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)?.write_all(token.as_bytes())?;
    Ok(token)
}
```

- **Constant-time compare** (§3) on every authenticated request.
- **Auth on by default** — an explicit opt-out for development, never an opt-in for
  production. A flag that *enables* auth ships with auth off.
- **Secrets never appear in responses, logs, or error messages.** The token
  round-trips through the `Authorization` header and nowhere else.
- **Wire error codes do not leak internals** — the client gets a stable code and a
  human message; stack traces, paths, and query text stay in the server-side log.
- **The health endpoint is the only unauthenticated route.** It returns liveness and
  nothing else — no version strings, no config echo, no directory paths.

---

## 5. Path traversal — safe_join

Any externally influenced path — an RPC parameter, a config value, an archive entry
name — must be confined to an allowed root. Reject `..` first, then canonicalize and
prefix-check:

```rust
use std::path::{Component, Path, PathBuf};

#[derive(Debug, thiserror::Error)]
pub enum PathError {
    #[error("absolute paths are not accepted")]
    AbsoluteRejected,
    #[error("path traversal ('..') is not accepted")]
    TraversalRejected,
    #[error("path escapes the permitted root")]
    EscapedRoot,
    #[error("path could not be resolved")]
    Io(#[source] std::io::Error),
}

/// Confine an externally influenced relative path to `root`.
pub fn safe_join(root: &Path, untrusted: &str) -> Result<PathBuf, PathError> {
    let relative = Path::new(untrusted);
    if relative.is_absolute() {
        return Err(PathError::AbsoluteRejected);
    }
    if relative.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err(PathError::TraversalRejected);
    }
    let canonical = root.join(relative).canonicalize().map_err(PathError::Io)?;
    let canonical_root = root.canonicalize().map_err(PathError::Io)?;
    if !canonical.starts_with(&canonical_root) {
        return Err(PathError::EscapedRoot);
    }
    Ok(canonical)
}
```

Caveats, spelled out because each one has been a CVE somewhere:

- **`canonicalize` requires the path to exist.** For create-new flows, canonicalize
  the *parent* directory, prefix-check that, then join the (already `..`-free) final
  component.
- **Symlinks resolve before the prefix check** — a link under the root pointing
  outside it is correctly rejected. That is the point of canonicalizing *after* the
  component check, not instead of it.
- **TOCTOU:** a symlink created between the check and the open defeats a path check.
  Open the checked path immediately; against a hostile *local* user, path checks are
  insufficient — use capability-style APIs (the `cap-std` crate) that resolve inside
  a directory handle.

---

## 6. Passwords and KDFs — argon2id

Passwords are never hashed with a general-purpose hash. Use argon2id via the
`argon2` crate (0.5), OWASP-current parameters, `OsRng` salts, PHC-string storage:

```rust
use argon2::{
    password_hash::{rand_core::OsRng, PasswordHash, PasswordHasher, PasswordVerifier, SaltString},
    Algorithm, Argon2, Params, Version,
};

/// argon2id, m=19 MiB, t=2, p=1 — the OWASP minimum recommendation.
/// Re-verify against the current OWASP Password Storage Cheat Sheet at adoption time.
fn kdf() -> Argon2<'static> {
    let params = Params::new(19 * 1024, 2, 1, None).expect("static params are valid");
    Argon2::new(Algorithm::Argon2id, Version::V0x13, params)
}

pub fn hash_password(password: &str) -> Result<String, argon2::password_hash::Error> {
    let salt = SaltString::generate(&mut OsRng);
    Ok(kdf().hash_password(password.as_bytes(), &salt)?.to_string())
}

pub fn verify_password(password: &str, stored_phc: &str) -> bool {
    PasswordHash::new(stored_phc)
        .and_then(|parsed| kdf().verify_password(password.as_bytes(), &parsed))
        .is_ok()
}
```

The PHC string (`$argon2id$v=19$m=19456,t=2,p=1$...`) embeds algorithm, version,
parameters, and salt — parameters can be raised later and verified against old
hashes transparently. Store it as an opaque TEXT column; nothing else.

---

## 7. `unsafe` and FFI islands

- **`unsafe_code = "deny"` at workspace level.** Opting out is a per-crate (or
  per-module) decision made only for FFI islands, visible in the diff:

```toml
# workspace Cargo.toml
[workspace.lints.rust]
unsafe_code = "deny"
unsafe_op_in_unsafe_fn = "deny"

[workspace.lints.clippy]
undocumented_unsafe_blocks = "deny"
```

```toml
# crates/native-bridge/Cargo.toml — the ONE crate allowed to opt out
[lints.rust]
unsafe_code = "allow"
unsafe_op_in_unsafe_fn = "deny"

[lints.clippy]
undocumented_unsafe_blocks = "deny"
```

- **Every `unsafe` block carries a `// SAFETY:` comment stating the invariant
  upheld** — backed mechanically by `undocumented_unsafe_blocks = deny`:

```rust
// SAFETY: `buf` is non-null and points to `len` initialized bytes for the
// duration of this call — guaranteed by the C API contract of `lib_read`.
let bytes = unsafe { std::slice::from_raw_parts(buf, len) };
```

- **`unsafe_op_in_unsafe_fn = deny`** — an `unsafe fn` body no longer gets a blanket
  license; each unsafe operation inside it needs its own block and its own SAFETY
  justification.
- **The island exposes a safe wrapper API.** The unsafe never leaks types that let
  callers violate invariants: no raw pointers, no lifetimes the caller must guess, no
  "call `init` first or UB" contracts. If the safe wrapper cannot enforce the
  precondition with types, the wrapper is not done.
- **Optional tier:** run Miri in CI for crates containing `unsafe` — it catches UB
  the type system cannot see (aliasing violations, use-after-free across the FFI
  edge). Expensive; scope it to the island crates.

---

## 8. DoS posture

Rust removes the memory-corruption path to a crash; it does nothing about resource
exhaustion. Defaults for anything that serves requests:

- **Bounded channels everywhere** — an unbounded channel is an unbounded queue with
  a memory-shaped failure mode (`async-tokio.md` owns the mechanics).
- **Request body limits** on every route (§1), and length caps inside the parsers.
- **Timeouts on all external awaits** — network, subprocess, IPC; the pattern and
  the review rule live in `async-tokio.md`. An await without a deadline is a leak of
  one connection per slow client.
- **Connection limits and per-client rate limiting for daemons** — tower's
  concurrency-limit layer and a governor-style rate limiter; the loopback daemon
  (§4) still gets them, because every local process and browser tab is a client.
- **Reject early, before allocation, where possible** — check the auth header before
  reading the body; check the length prefix before buffering the frame.

---

## 9. Checklist

- [ ] Trust boundaries enumerated in the design doc: argv, env, files, sockets, IPC, database.
- [ ] Boundary input parsed into domain newtypes; every deserialization capped (size, length, depth).
- [ ] No secret in argv; env or 0600 file only; `Secret<T>`/`secrecy` wrapping all key material.
- [ ] No token/key ever logged — grep-clean under `rust-review.md` §2.
- [ ] All secret comparisons constant-time (`subtle`); zero `==` on secret bytes.
- [ ] Daemons: loopback bind, first-run 0600 token, auth on by default, unauthenticated health only.
- [ ] Externally influenced paths through `safe_join` (with the create-new parent variant).
- [ ] Passwords: argon2id with OWASP-current params, OsRng salt, PHC storage.
- [ ] `unsafe_code = deny` at workspace; islands opt out per crate; every block has SAFETY; wrappers safe.
- [ ] Bounded channels, body limits, timeouts, connection/rate limits — rejection before allocation.

---

## 10. Anti-patterns

| Anti-Pattern | Problem | Solution |
|---|---|---|
| Secret in a `format!`/log call | The log pipeline (files, shippers, third-party sinks) now stores the credential | `Secret<T>` with `[REDACTED]` impls; the `rust-review.md` grep is blocking (§2) |
| Token in a URL | URLs land in access logs, browser history, and `Referer` headers | `Authorization` header only; never query parameters |
| `0.0.0.0` default bind | The "local" daemon is now a LAN service with local-grade auth | Explicit `127.0.0.1`; all-interfaces is documented opt-in (§4) |
| `unwrap` on attacker-influenced parse | Remote input becomes a remote panic — a free DoS primitive | Parse into `Result`, map to a boundary error (`errors.md`) |
| Unlimited request body | One request allocates until the OOM killer arbitrates | `DefaultBodyLimit` + parser-level caps (§1, §8) |
| Comparing secrets with `==` | Timing side channel; token recoverable byte by byte | `subtle::ConstantTimeEq` (§3) — blocking in review |
| Auth as opt-in | Every install that never read the docs runs open | Auth on by default; opt-out is explicit and warned (§4) |
