# Rust — Async with Tokio: Task Ownership, Cancellation, Actors, and Streams

The async rules of the house: every spawned task has an owner, cancellation is a
first-class code path tested like the happy path, and state that several tasks want
is *owned by one task* rather than shared under a lock. Version pins for
tokio/tokio-util: `dependencies-supply-chain.md`; lint backing: `toolchain-and-lints.md`.

**Mental model.** A future does nothing until polled and *ceases to exist* when
dropped — and `select!` drops the losers. Every pattern below is a consequence:
ownership of tasks (they finish or are told to stop), of buffers (a dropped poll
loses nothing), of state (no lock crosses an `await`).

---

## 1. Task lifecycle ownership

**The rule:** every `tokio::spawn`'s `JoinHandle` is (a) awaited, (b) held in a
tracker that is aborted or awaited at shutdown, or (c) documented as deliberately
detached *with the reason*. A dropped handle is a task whose panic nobody observes.
The graceful-shutdown skeleton — `TaskTracker` owns the tasks, one
`CancellationToken` stops them, and the ordering matters:

```rust
use tokio_util::{sync::CancellationToken, task::TaskTracker};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let token = CancellationToken::new();
    let tracker = TaskTracker::new();

    for id in 0..4 {
        let child = token.child_token();
        tracker.spawn(async move {
            // real workers run a select! loop racing `child.cancelled()` (§3)
            child.cancelled().await;
            tracing::info!(worker = id, "cancelled; draining");
        });
    }

    tokio::signal::ctrl_c().await?;

    tracker.close();      // 1. no new tasks may join the tracker
    token.cancel();       // 2. every task is told to wind down
    tracker.wait().await; // 3. and we wait until every one of them HAS
    Ok(())
}
```

`close()` must precede `wait()` (it never resolves on an open tracker); cancel
*between* them so every task spawned up to the close is signalled first.

---

## 2. Cancellation

One primitive per application: `CancellationToken`; each subsystem gets a
`child_token()` (parent cancel reaches everything; child cancel stops one subsystem).
**State-map hygiene:** a token registered per request id must be removed on
completion *and* on panic — a `Drop` guard covers every exit path at once:

```rust
use std::{collections::HashMap, future::Future, sync::{Arc, Mutex}};

use tokio_util::sync::CancellationToken;

pub type CancelRegistry = Arc<Mutex<HashMap<u64, CancellationToken>>>;

#[derive(Debug, thiserror::Error)] #[error("request was cancelled")] pub struct Cancelled;

/// Removes the entry when dropped — on success, error, AND panic. The registry
/// is recoverable state, so the poison policy is `into_inner()`.
struct RegistryGuard { id: u64, registry: CancelRegistry }

impl Drop for RegistryGuard {
    fn drop(&mut self) {
        self.registry.lock().unwrap_or_else(|p| p.into_inner()).remove(&self.id);
    }
}

pub async fn run_guarded<T>(
    id: u64, registry: CancelRegistry, parent: &CancellationToken,
    work: impl Future<Output = Result<T, Cancelled>>,
) -> Result<T, Cancelled> {
    let token = parent.child_token();
    registry.lock().unwrap_or_else(|p| p.into_inner()).insert(id, token.clone());
    let _guard = RegistryGuard { id, registry: registry.clone() };

    tokio::select! {
        _ = token.cancelled() => Err(Cancelled),
        result = work => result,
    }
    // _guard drops here on every path — the registry cannot leak entries.
}
```

A cancel endpoint looks up the id, calls `.cancel()`, and reports whether the id was
known. **Cancellation maps to one named wire code everywhere** (`errors.md` §3) — a
client must never learn "cancelled" from message prose.

---

## 3. `select!` and cancellation safety

In a `select!`, every branch that did not complete is **dropped** — including any
bytes a half-finished future had already read into its own buffer. Only
**cancellation-safe** operations may sit in a looped branch: `mpsc::Receiver::recv`
yes (a message is taken or still queued), `read_exact` into a local buffer no.

```rust
use std::time::Duration;

use futures_util::StreamExt;
use tokio::{io::AsyncReadExt, net::TcpStream};
use tokio_util::{codec::{FramedRead, LengthDelimitedCodec}, sync::CancellationToken};

// WRONG — read_exact is not cancellation-safe. When the tick wins mid-read, the read
// future is dropped, its consumed header bytes vanish, and the next iteration
// restarts mid-stream. Silent desynchronization.
async fn recv_loop_torn(mut socket: TcpStream) -> std::io::Result<()> {
    let mut stats = tokio::time::interval(Duration::from_secs(5));
    loop {
        let mut len_buf = [0u8; 4];
        tokio::select! {
            _ = stats.tick() => {} // any winning branch drops the in-flight read
            res = socket.read_exact(&mut len_buf) => {
                res?;
                let len = u32::from_be_bytes(len_buf) as usize;
                // read `len` body bytes and handle the frame ...
            }
        }
    }
}

// RIGHT — the read buffer lives in FramedRead, OUTSIDE the select!. Dropping
// the poll loses nothing: partial bytes wait in the codec for the next poll.
async fn recv_loop(socket: TcpStream, token: CancellationToken) -> std::io::Result<()> {
    let mut frames = FramedRead::new(socket, LengthDelimitedCodec::new());
    loop {
        tokio::select! {
            biased; // poll in written order: a hot stream must not starve shutdown
            _ = token.cancelled() => return Ok(()),
            frame = frames.next() => match frame {
                Some(Ok(bytes)) => tracing::debug!(bytes = bytes.len(), "frame received"),
                Some(Err(e)) => return Err(e),
                None => return Ok(()), // peer closed
            },
        }
    }
}
```

**Pin long-lived futures outside the loop** (the framed stream above; likewise a boxed
upstream response) — state that must survive a lost race cannot live inside a branch.
**`biased;` when starvation matters:** polling order is otherwise randomized; with it,
branches poll in written order — shutdown goes first.

---

## 4. The actor pattern (mpsc + oneshot)

When to use: serializing access to state one task owns; replacing a shared-mutex design
that grew contended or deadlock-prone (`architecture.md` §6). Each command variant
carries its own reply channel — the handle reads like an async method call:

```rust
use std::collections::HashMap;

use tokio::sync::{mpsc, oneshot};

#[derive(Debug, thiserror::Error)]
pub enum RegistryError {
    #[error("registry task has shut down")] Closed,
    #[error("no entry for key {0}")] NotFound(String),
}

enum Command {
    Get { key: String, reply: oneshot::Sender<Result<String, RegistryError>> },
    Put { key: String, value: String, reply: oneshot::Sender<Result<(), RegistryError>> },
}

#[derive(Clone)]
pub struct RegistryHandle { tx: mpsc::Sender<Command> }

impl RegistryHandle {
    pub fn spawn() -> (RegistryHandle, tokio::task::JoinHandle<()>) {
        let (tx, rx) = mpsc::channel(64); // bounded: senders feel backpressure (§7)
        (RegistryHandle { tx }, tokio::spawn(run(rx)))
    }

    pub async fn get(&self, key: String) -> Result<String, RegistryError> {
        let (reply, rx) = oneshot::channel();
        self.tx.send(Command::Get { key, reply }).await.map_err(|_| RegistryError::Closed)?;
        rx.await.map_err(|_| RegistryError::Closed)?
    }

    // put() has the identical shape: build a oneshot, send Command::Put, await the reply.
}

async fn run(mut rx: mpsc::Receiver<Command>) {
    let mut entries: HashMap<String, String> = HashMap::new(); // OWNED: no mutex, no poisoning
    while let Some(cmd) = rx.recv().await {
        match cmd {
            Command::Get { key, reply } => {
                let result = entries.get(&key).cloned().ok_or(RegistryError::NotFound(key));
                // A failed reply send means the caller went away — nothing to act on.
                let _ = reply.send(result);
            }
            Command::Put { key, value, reply } => {
                entries.insert(key, value);
                let _ = reply.send(Ok(()));
            }
        }
    }
    // recv() returned None: every handle dropped — natural shutdown.
}
```

Shutdown is structural: drop all handles and the loop ends. Send and reply-recv
failures both map to one `Closed` error — "actor gone" is ordinary and matchable.

---

## 5. The `!Send` resource on a dedicated OS thread

Some resources are thread-affine — embedded-database connections that are `!Send` or
`!Sync` in their driver, FFI/COM handles, C library contexts — and cannot live in a
tokio task that migrates between workers. The pattern: **one `std::thread` owns the
resource forever**; `std::sync::mpsc` in, `tokio::sync::oneshot` out, and a **ready
handshake** at init so construction errors surface to the caller:

```rust
use std::{path::PathBuf, sync::mpsc, thread::JoinHandle};

use tokio::sync::oneshot;

// `NativeHandle` stands in for any thread-affine resource. The applied SQLite version
// is `sqlite-rusqlite.md`; schema/pragma guidance: `../../sql/references/sqlite.md`.

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("store thread is closed")] Closed,
    #[error("store failed to open: {0}")] Open(String),
    #[error("query failed: {0}")] Query(String),
}

enum StoreCommand {
    // Put, Delete, ... follow the same reply-channel shape.
    Get { key: String, reply: oneshot::Sender<Result<Option<String>, StoreError>> },
}

pub struct Store {
    tx: Option<mpsc::Sender<StoreCommand>>, // Option so Drop can take it
    thread: Option<JoinHandle<()>>,
}

impl Store {
    /// Blocks on the handshake — call during startup, or via `spawn_blocking`.
    pub fn open(path: PathBuf) -> Result<Self, StoreError> {
        let (tx, rx) = mpsc::channel::<StoreCommand>();
        let (ready_tx, ready_rx) = mpsc::channel::<Result<(), StoreError>>();

        let thread = std::thread::spawn(move || {
            // The !Send resource is created ON this thread and never leaves it.
            let resource = match NativeHandle::open(&path) {
                Ok(r) => r,
                // On failure the thread exits; the caller gets an error, not a hang:
                Err(e) => return drop(ready_tx.send(Err(StoreError::Open(e.to_string())))),
            };
            let _ = ready_tx.send(Ok(())); // READY: construction succeeded

            while let Ok(cmd) = rx.recv() {
                match cmd {
                    StoreCommand::Get { key, reply } => {
                        let _ = reply.send(resource.get(&key).map_err(|e| StoreError::Query(e.to_string())));
                    }
                }
            }
            // recv() failed: the sender was dropped — clean shutdown.
        });

        match ready_rx.recv() {
            Ok(Ok(())) => Ok(Store { tx: Some(tx), thread: Some(thread) }),
            Ok(Err(e)) => { let _ = thread.join(); Err(e) }
            Err(_) => Err(StoreError::Open("store thread died during init".into())),
        }
    }

    pub async fn get(&self, key: String) -> Result<Option<String>, StoreError> {
        let (reply, rx) = oneshot::channel();
        let tx = self.tx.as_ref().ok_or(StoreError::Closed)?;
        tx.send(StoreCommand::Get { key, reply }).map_err(|_| StoreError::Closed)?;
        rx.await.map_err(|_| StoreError::Closed)?
    }
}

impl Drop for Store {
    fn drop(&mut self) {
        drop(self.tx.take()); // closes the channel → recv() errors → loop exits
        if let Some(handle) = self.thread.take() {
            let _ = handle.join(); // deterministic: resource released before drop returns
        }
    }
}
```

---

## 6. Blocking work

- **`spawn_blocking` for blocking syscalls and CPU-heavy work.** Measurable rule: no
  `std::fs`/`std::net` calls and no unbounded compute inside an `async fn` body — a
  file read, a password hash, a big serde parse all block a worker thread if written
  inline. Blocking goes in a *sync* function handed to `spawn_blocking`; its
  `JoinError` maps to a named "worker panicked" variant (`errors.md`).
- **`block_in_place` is the narrow exception**: multi-thread runtime only (panics on
  `current_thread`), blocks its worker — only when moving the data is prohibitive.

---

## 7. Timeouts, retries, backpressure

**Every external `await` is wrapped in `tokio::time::timeout` at the boundary** — the
network does not owe you a response. The shape is `tokio::time::timeout(deadline,
fut).await`, its `Elapsed` mapped to a named `DeadlineExceeded { after }` variant
(`errors.md`); wrap each external call, not some outer aggregate (§10).

**Retries carry backoff, jitter, and a budget** — an infinite retry loop is an outage
generator wearing a resilience costume:

```rust
use std::{future::Future, num::NonZeroU32, time::Duration};

pub async fn retry_with_budget<T, E, Fut>(
    mut op: impl FnMut() -> Fut,
    budget: NonZeroU32,
) -> Result<T, E>
where Fut: Future<Output = Result<T, E>> {
    let mut delay = Duration::from_millis(100);
    for attempt in 1..=budget.get() {
        match op().await {
            Ok(value) => return Ok(value),
            Err(err) if attempt == budget.get() => return Err(err),
            Err(_) => {
                // exponential backoff + jitter (any cheap RNG; fastrand shown)
                let jitter = Duration::from_millis(fastrand::u64(0..=delay.as_millis() as u64 / 2));
                tokio::time::sleep(delay + jitter).await;
                delay = (delay * 2).min(Duration::from_secs(10));
            }
        }
    }
    unreachable!("the final attempt either returned Ok or returned its Err above")
}
```

**Channels are bounded by default.** An unbounded channel requires a written argument
for why memory growth is impossible (e.g. a semaphore already gates the producer).
"We didn't want senders to block" is the problem, not the answer.

| Channel | Use for | Notes |
|---|---|---|
| `mpsc` (bounded) | work items, commands | Backpressure by design; capacity is a stated budget, not a tuning hack |
| `watch` | latest-value state (config, health) | Receivers see only the newest value; ideal for "what is it *now*" |
| `broadcast` | fan-out events | Decide the lagging-receiver policy up front — `Lagged(n)` is a contract, not a surprise |
| `oneshot` | single reply | Pairs with `mpsc` for request/response (§4, §5) |

---

## 8. Streams out (SSE / long-poll, generalized)

Produce with `async_stream`; **split on the protocol's frame delimiter (`\n\n` for
SSE), never on a single `\n`** — a chunk boundary can land mid-frame:

```rust
use async_stream::stream;
use futures_util::{Stream, StreamExt};

pub enum OutEvent { Delta(String), Done, Error { message: String } }

pub fn relay<E: std::fmt::Display>(
    mut upstream: impl Stream<Item = Result<bytes::Bytes, E>> + Unpin,
) -> impl Stream<Item = OutEvent> {
    stream! {
        let mut buf = String::new();
        while let Some(chunk) = upstream.next().await {
            match chunk {
                Ok(bytes) => {
                    buf.push_str(&String::from_utf8_lossy(&bytes));
                    // Drain COMPLETE frames; a partial frame waits in `buf` for the next chunk.
                    while let Some(end) = buf.find("\n\n") {
                        let frame = buf[..end].to_string();
                        buf.drain(..end + 2);
                        for data in frame.lines().filter_map(|l| l.strip_prefix("data: ")) {
                            yield OutEvent::Delta(data.to_string());
                        }
                    }
                }
                Err(e) => {
                    yield OutEvent::Error { message: e.to_string() };
                    return;
                }
            }
        }
        yield OutEvent::Done;
    }
}
```

Map `OutEvent` onto your HTTP framework's SSE type in the shell layer. **Once a
stream has started, errors are in-band events**: the status line went out with the
first byte, so a mid-stream failure is yielded as an `Error` event — an HTTP status
is no longer available.

---

## 9. Runtime discipline

- **One runtime per process.** A runtime per request spawns worker pools per call;
  one built *inside* another panics ("Cannot start a runtime from within a runtime").
- **Library code takes a `tokio::runtime::Handle`** — a constructor like
  `fn new(runtime: tokio::runtime::Handle) -> Self`, then `self.runtime.spawn(...)`;
  it never builds a runtime of its own, and every spawn's `JoinHandle` is returned or
  tracked per §1.
- **Tests touching process globals stay on the `current_thread` flavor** — plain
  `#[tokio::test]` already is; save/restore any env they mutate. Multi-threaded
  behavior gets an explicit `#[tokio::test(flavor = "multi_thread")]` (`testing.md`).

---

## 10. Pitfalls

| Anti-Pattern | Problem | Solution |
|---|---|---|
| Lock guard held across an `.await` | Every task sharing the lock stalls; deadlock when the awaited path needs the same lock | Lock → extract/clone → drop guard → then await (`architecture.md` §6); `clippy::await_holding_lock` denies it |
| Cancellation-unsafe op in a looped `select!` branch | Partial reads dropped with the losing future; protocol silently desynchronized | Framed/buffered reader pinned outside the loop; cancellation-safe ops only (§3) |
| `JoinHandle` dropped on the floor | Panics vanish unobserved; shutdown cannot wait for the task | Await it, put it in a `TaskTracker`, or document the detachment with a reason (§1) |
| Unbounded channel by default | Slow consumer + fast producer = unbounded memory growth, then the OOM killer | Bounded channels; unbounded only with a written impossibility argument (§7) |
| No ready handshake on a dedicated thread | Construction failure produces a dead thread; every later call fails far from the cause | READY handshake at init — the error surfaces in `open()` (§5) |
| Spawning tasks inside `Drop` | `Drop` cannot await; at shutdown the runtime may be gone before the task runs | Explicit async `close()` method; `Drop` only closes channels and joins threads (§5) |
| Sleep-polling a condition (`loop { sleep; check }`) | Latency floor equals the poll interval; CPU burned on nothing | `Notify`, `watch`, or a channel — wake on the event, not on a timer (§7) |
| `select!` without `biased;` where one branch is hot | Randomized polling lets the hot branch starve shutdown/control branches | `biased;` with control branches listed first (§3) |
| Timeout wrapping the wrong layer | A timeout around the whole retry loop (or only around connect) makes deadlines lie | Timeout at each external await boundary; the retry budget is a separate, outer decision (§7) |
| Runtime built per request / nested runtimes | Thread-pool churn per call; nested construction panics | One runtime per process; pass a `Handle` into library code (§9) |
