# Flask-Iris — Full Usage Guide

> **Flask-Iris** wraps the [Iris distributed runtime fabric](https://github.com/SSL-ACTX/iris) and exposes its full API through a clean Flask-style extension. This document covers every public method, decorator, and pattern available.

---

## Table of Contents

1. [Initialization](#1-initialization)
2. [Decorators](#2-decorators)
   - [@actor](#21-actor)
   - [@supervised_actor](#22-supervised_actor)
3. [Smart Send Helpers](#3-smart-send-helpers)
4. [Spawning Actors](#4-spawning-actors)
5. [Messaging & Timers](#5-messaging--timers)
6. [Local Name Registry](#6-local-name-registry)
7. [Path Registry & Supervision](#7-path-registry--supervision)
8. [Distributed Mesh (Multi-Node)](#8-distributed-mesh-multi-node)
9. [Hot-Swapping Behavior](#9-hot-swapping-behavior)
10. [Lifecycle & Mailbox Inspection](#10-lifecycle--mailbox-inspection)
11. [GIL & Thread Limits](#11-gil--thread-limits)
12. [System Messages (PySystemMessage)](#12-system-messages-pysystemmessage)
13. [Full Examples](#13-full-examples)

---

## 1. Initialization

Flask-Iris supports both the direct and application-factory patterns.

### Direct initialization

```python
from flask import Flask
from flask_iris import FlaskIris

app = Flask(__name__)
iris_ext = FlaskIris(app)
```

### Application factory (recommended)

```python
from flask import Flask
from flask_iris import FlaskIris

iris_ext = FlaskIris()

def create_app():
    app = Flask(__name__)
    iris_ext.init_app(app)
    return app
```

`init_app` starts the Iris runtime and fires any actor/supervisor decorators that were registered before the runtime was available.

---

## 2. Decorators

### 2.1 `@actor`

Spawns a **push actor** (green-thread) and registers it under a name at startup.

```python
iris_ext.actor(name, budget=100, release_gil=False, auto_json=True)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | required | Name used to target this actor via `cast()` / `send_named()` |
| `budget` | `int` | `100` | Max messages processed per scheduling tick |
| `release_gil` | `bool` | `False` | Run handler in a thread that releases the GIL (for CPU-bound native code) |
| `auto_json` | `bool` | `True` | Automatically deserialize incoming `bytes` as JSON into a `dict` before calling your handler |

```python
@iris_ext.actor("order_events", budget=200)
def handle_order(payload: dict):
    print(f"New order: {payload['order_id']}")
```

When `auto_json=True` and the bytes cannot be decoded as JSON, the raw `bytes` object is passed through to the handler unchanged.

---

### 2.2 `@supervised_actor`

Spawns a **fault-tolerant worker** under a path-scoped supervisor. If the worker crashes, the supervisor restarts it automatically using the provided factory.

```python
iris_ext.supervised_actor(path, budget=100, strategy="restartone", auto_json=True)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | Hierarchical path prefix (e.g. `/svc/payments`) |
| `budget` | `int` | `100` | Messages processed per tick |
| `strategy` | `str` | `"restartone"` | `"restartone"` restarts only the crashed actor; `"restartall"` restarts all children |
| `auto_json` | `bool` | `True` | Deserialize incoming bytes as JSON |

The worker is automatically registered at `{path}/worker` and can be targeted with `cast_path("{path}/worker", payload)`.

```python
@iris_ext.supervised_actor("/svc/payments", budget=100, strategy="restartone")
def payment_handler(payload: dict):
    if payload.get("amount", 0) < 0:
        raise ValueError("Negative amount — crashing for demo")
    print(f"Processing payment: {payload}")
```

---

## 3. Smart Send Helpers

These helpers handle serialization automatically so you never manually call `orjson.dumps`.

### `cast(name, payload)`

Serialize and dispatch a payload to a **named actor**.

```python
iris_ext.cast("order_events", {"order_id": 42, "item": "book"})
iris_ext.cast("order_events", "plain string")      # strings are UTF-8 encoded
iris_ext.cast("order_events", b"\x00raw\xff")      # bytes passed through unchanged
```

Returns `True` if the message was delivered to the mailbox, `False` otherwise.

### `cast_path(path, payload)`

Serialize and dispatch to a **path-registered actor**.

```python
iris_ext.cast_path("/svc/payments/worker", {"amount": 9.99, "currency": "USD"})
```

Returns `False` if the path is not currently registered (e.g. the actor is mid-restart).

### `_prepare_payload(payload)` *(internal)*

Converts `dict`/`list` → `bytes` via `orjson.dumps`, `str` → UTF-8 bytes, and passes `bytes` through unchanged. Used internally by `cast` and `cast_path`.

---

## 4. Spawning Actors

Use these when you need full control over actor type and lifetime instead of the decorator API.

| Method | Description |
|---|---|
| `spawn(handler, budget, release_gil)` | Push actor backed by a green-thread |
| `spawn_py_handler_bounded(handler, budget, capacity, release_gil)` | Push actor with a fixed-size mailbox |
| `spawn_virtual(handler, budget, idle_timeout_ms)` | Lazily activated actor — PID reserved, actor starts only on first message |
| `spawn_with_mailbox(handler, budget)` | Pull actor in a dedicated OS thread (useful for blocking I/O) |
| `spawn_child(parent, handler, budget, release_gil)` | Actor whose lifetime is tied to `parent` PID |
| `spawn_child_pool(parent, handler, workers, budget, release_gil)` | Persistent pool of child workers |
| `spawn_child_with_mailbox(parent, handler, budget)` | Pull-based child actor |

```python
# Manually spawn a push actor and register it
pid = iris_ext.spawn(my_handler, budget=50)
iris_ext.register("my_service", pid)

# Spawn a virtual actor that activates lazily
vpid = iris_ext.spawn_virtual(my_handler, budget=100, idle_timeout_ms=5000)

# Spawn a pool of 4 child workers tied to a parent PID
pool = iris_ext.spawn_child_pool(parent_pid, worker_fn, workers=4, budget=100)
```

---

## 5. Messaging & Timers

### Direct send

```python
iris_ext.send(pid, b"raw bytes")           # send to PID directly
iris_ext.send_named("my_service", b"data") # resolve by name and send
```

### Timers

```python
# One-shot: send once after 500 ms
timer_id = iris_ext.send_after(pid, 500, b"tick")

# Recurring: send every 1000 ms
interval_id = iris_ext.send_interval(pid, 1000, b"heartbeat")

# Cancel either
iris_ext.cancel_timer(timer_id)
iris_ext.cancel_timer(interval_id)
```

### Zero-copy send

```python
iris_ext.send_buffer(pid, buffer_id)  # send a pre-allocated Buffer ID (no copy)
```

### Overflow policy (bounded mailboxes)

```python
# "drop_newest" | "drop_oldest" | "redirect" | "block"
iris_ext.set_overflow_policy(pid, "drop_oldest")

# Redirect overflow to another actor
iris_ext.set_overflow_policy(pid, "redirect", target=overflow_pid)
```

### Selective receive

```python
# Async — await inside a coroutine
msg = await iris_ext.selective_recv(pid, lambda m: b"order" in m, timeout=2.0)

# Blocking — use inside sync Flask route
msg = iris_ext.selective_recv_blocking(pid, lambda m: b"order" in m, timeout=2.0)
```

Returns the matched message, or `None` on timeout.

---

## 6. Local Name Registry

```python
iris_ext.register("analytics", pid)      # bind name → PID
iris_ext.unregister("analytics")         # remove binding

pid = iris_ext.resolve("analytics")      # None if not found
pid = iris_ext.whereis("analytics")      # Erlang-style alias for resolve()
```

---

## 7. Path Registry & Supervision

The path registry uses hierarchical string paths (similar to a filesystem) for service discovery.

```python
iris_ext.register_path("/svc/auth/primary", pid)
iris_ext.unregister_path("/svc/auth/primary")

pid = iris_ext.whereis_path("/svc/auth/primary")   # exact lookup

# List all entries under a prefix
all_svc = iris_ext.list_children("/svc")

# Only direct children one level below prefix
direct = iris_ext.list_children_direct("/svc")

# Shallow watch — get notified when direct children change
iris_ext.watch_path("/svc/auth")
```

### Path supervisors (manual)

```python
iris_ext.create_path_supervisor("/svc/worker")

def factory():
    pid = iris_ext.spawn(my_handler, budget=100)
    iris_ext.register_path("/svc/worker/instance", pid)
    return pid

initial_pid = factory()
iris_ext.path_supervise_with_factory("/svc/worker", initial_pid, factory, "restartone")

# Introspect
iris_ext.path_supervisor_watch("/svc/worker", some_pid)
children = iris_ext.path_supervisor_children("/svc/worker")
count = iris_ext.children_count()
pids  = iris_ext.child_pids()

# Tear down
iris_ext.remove_path_supervisor("/svc/worker")
```

---

## 8. Distributed Mesh (Multi-Node)

Flask-Iris can bridge multiple processes or machines over TCP using the Iris mesh protocol.

### Starting a listener

```python
iris_ext.listen("0.0.0.0:9000")   # accept remote messages and remote resolves
```

### Resolving and messaging remote actors

```python
# Blocking remote resolve — returns a local proxy PID
proxy = iris_ext.resolve_remote("192.168.1.10:9000", "data_processor")
if proxy:
    iris_ext.send(proxy, b"hello from this node")

# Async resolve inside a coroutine
proxy = await iris_ext.resolve_remote_py("192.168.1.10:9000", "data_processor")

# Health probe
alive = iris_ext.is_node_up("192.168.1.10:9000")     # True / False

# Send directly (no proxy resolution)
iris_ext.send_remote("192.168.1.10:9000", remote_pid, b"data")

# Watch a remote PID — local supervisor is triggered if it goes down
iris_ext.monitor_remote("192.168.1.10:9000", remote_pid)
```

### Two-node example

See [examples/multi_node.py](../examples/multi_node.py) for a complete runnable cluster that starts two Flask apps in separate processes and routes HTTP requests over the Iris mesh.

---

## 9. Hot-Swapping Behavior

Replace actor logic at runtime without stopping the actor or losing mailbox state.

```python
def v2_handler(msg: bytes):
    print(f"[v2] {msg}")

iris_ext.hot_swap(pid, v2_handler)

version = iris_ext.behavior_version(pid)   # e.g. 2
iris_ext.rollback_behavior(pid, steps=1)   # revert to previous version
```

---

## 10. Lifecycle & Mailbox Inspection

```python
iris_ext.is_alive(pid)           # True / False
iris_ext.mailbox_size(pid)       # int message count, or None if not applicable
iris_ext.stop(pid)               # gracefully stop and close mailbox
iris_ext.join(pid)               # block caller until actor exits
```

---

## 11. GIL & Thread Limits

When spawning actors with `release_gil=True`, you can cap system-wide thread usage:

```python
# Allow at most 8 concurrent GIL-release threads backed by a pool of 4
iris_ext.set_release_gil_limits(max_threads=8, pool_size=4)

# Raise an error instead of silently falling back when limits are hit
iris_ext.set_release_gil_strict(strict=True)
```

---

## 12. System Messages (`PySystemMessage`)

Iris sends lifecycle notifications to actors as `PySystemMessage` objects (e.g. child crash, monitor down). Import and check for them inside your handlers:

```python
from flask_iris import PySystemMessage

@iris_ext.actor("supervisor_aware", auto_json=False)
def smart_handler(msg):
    if isinstance(msg, PySystemMessage):
        print(f"System event: {msg}")
    else:
        print(f"User message: {msg}")
```

`PySystemMessage` is also available on the class itself: `FlaskIris.PySystemMessage`.

---

## 13. Full Examples

### Single-node app with actors and supervision

```python
from flask import Flask, jsonify, request
from flask_iris import FlaskIris

app = Flask(__name__)
iris_ext = FlaskIris(app)

@iris_ext.actor("logger", budget=50)
def log_worker(payload: dict):
    print(f"[Logger] {payload}")

@iris_ext.supervised_actor("/svc/fragile", budget=50)
def fragile(payload: dict):
    if payload.get("crash"):
        raise RuntimeError("intentional crash")
    print(f"[Fragile] {payload}")

@app.route("/log", methods=["POST"])
def log():
    iris_ext.cast("logger", request.json or {})
    return jsonify({"status": "ok"})

@app.route("/fragile", methods=["POST"])
def call_fragile():
    ok = iris_ext.cast_path("/svc/fragile/worker", request.json or {})
    return jsonify({"dispatched": ok})

if __name__ == "__main__":
    app.run(port=5000)
```

### Application factory pattern

```python
from flask import Flask
from flask_iris import FlaskIris

iris_ext = FlaskIris()

@iris_ext.actor("events")
def handle_event(payload: dict):
    print(payload)

def create_app():
    app = Flask(__name__)
    iris_ext.init_app(app)   # runtime starts here; deferred actors are registered now
    return app
```

### Timed heartbeat

```python
pid = iris_ext.spawn(lambda msg: print("beat"), budget=10)
iris_ext.send_interval(pid, 1000, b"pulse")   # fires every 1 s
```

### Manual spawn + hot-swap

```python
def v1(msg: bytes): print(f"v1: {msg}")
def v2(msg: bytes): print(f"v2: {msg}")

pid = iris_ext.spawn(v1, budget=100)
iris_ext.register("worker", pid)

# Later, swap to v2 without downtime
iris_ext.hot_swap(pid, v2)
```
