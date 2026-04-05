# flask_iris/extension.py
import asyncio
import functools
import logging
from typing import Optional, Callable, Union, Awaitable, Any
import iris
from iris import PySystemMessage
from flask import Flask

# Conditional import for robust JSON fallback
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    import json
    HAS_ORJSON = False

# Internal logger for handling serialization fallbacks silently
logger = logging.getLogger("flask_iris")

class FlaskIris:
    """
    Flask extension to integrate the Iris distributed runtime fabric.
    """
    # Re-expose core Iris components directly on the class for convenience
    PySystemMessage = PySystemMessage

    def __init__(self, app: Flask = None):
        self.rt = None
        self._deferred_setups = []
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask):
        """Initialize the Iris runtime and bind it to the Flask app context."""
        self.rt = iris.Runtime()
        iris.jit.set_jit_logging(True)

        # Execute any deferred actor/supervisor setups registered via decorators
        for setup_func in self._deferred_setups:
            setup_func(self.rt)
        self._deferred_setups.clear()

        if not hasattr(app, "extensions"):
            app.extensions = {}
        app.extensions["iris"] = self

    # --- High-Level Abstractions & Decorators ---

    @staticmethod
    def offload(strategy="jit", return_type="float", **kwargs):
        """
        Directly expose the Iris JIT offload decorator.
        Users can use @iris_ext.offload() without needing to import iris.
        """
        return iris.offload(strategy=strategy, return_type=return_type, **kwargs)

    def _parse_payload(self, msg: Any) -> Any:
        """
        Smart helper to convert incoming bytes from the Iris mesh into Python objects.
        Handles JSON fallback automatically.
        """
        if isinstance(msg, bytes):
            try:
                if HAS_ORJSON:
                    return orjson.loads(msg)
                else:
                    return json.loads(msg.decode('utf-8'))
            except Exception:
                pass # Fallback to raw message if decoding fails
        return msg

    def actor(self, name: str, budget: int = 100, release_gil: bool = False, auto_json: bool = True):
        """
        Decorator to automatically spawn and register a push actor on startup.

        If auto_json=True, incoming bytes are automatically parsed into Python dicts.

        Usage:
            @iris_ext.actor("logger")
            def log_worker(payload: dict): ...
        """
        def decorator(fn):
            @functools.wraps(fn)
            def wrapped_handler(msg: Union[bytes, PySystemMessage]):
                if auto_json:
                    msg = self._parse_payload(msg)
                return fn(msg)

            def setup(rt):
                pid = rt.spawn(wrapped_handler, budget=budget, release_gil=release_gil)
                rt.register(name, pid)

            if self.rt is None:
                self._deferred_setups.append(setup)
            else:
                setup(self.rt)
            return fn
        return decorator

    def supervised_actor(self, path: str, budget: int = 100, strategy: str = "restartone", auto_json: bool = True):
        """
        Decorator to automatically create a path-scoped supervisor and spawn a worker.
        """
        def decorator(fn):
            @functools.wraps(fn)
            def wrapped_handler(msg: Union[bytes, PySystemMessage]):
                if auto_json:
                    msg = self._parse_payload(msg)
                return fn(msg)

            def setup(rt):
                rt.create_path_supervisor(path)

                def factory():
                    pid = rt.spawn(wrapped_handler, budget=budget)
                    rt.register_path(f"{path}/worker", pid)
                    return pid

                initial_pid = factory()
                rt.path_supervise_with_factory(path, initial_pid, factory, strategy)

            if self.rt is None:
                self._deferred_setups.append(setup)
            else:
                setup(self.rt)
            return fn
        return decorator

    def _prepare_payload(self, payload: Any) -> bytes:
        """
        Smart helper to convert various types into bytes for the Iris mesh.
        Handles dicts, lists, strings, and raw bytes with JSON fallbacks.
        """
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, str):
            return payload.encode('utf-8')

        try:
            if HAS_ORJSON:
                return orjson.dumps(payload)
            else:
                return json.dumps(payload).encode('utf-8')
        except Exception as e:
            try:
                # Last resort fallback for non-serializable objects
                logger.error(f"Iris payload preparation failed for JSON: {e}")
                return str(payload).encode('utf-8')
            except Exception as inner_e:
                logger.error(f"Iris payload string-cast failed: {inner_e}")
                return b""

    def cast(self, target: Union[str, int], payload: Any) -> bool:
        """
        Smart helper to serialize and send a payload.
        Handles both registered names (str) and PIDs (int).
        """
        data = self._prepare_payload(payload)

        # If target is an integer, it is a PID. Use direct send.
        if isinstance(target, int):
            return self.rt.send(target, data)

        # If target is a string, resolve name and send.
        return self.rt.send_named(str(target), data)

    def cast_path(self, path: str, payload: Any) -> bool:
        """Smart helper to serialize and send a payload to a path-registered actor."""
        target_pid = self.whereis_path(path)
        if target_pid:
            return self.rt.send(target_pid, self._prepare_payload(payload))
        return False

    # --- Spawning & Actors ---

    def spawn(self, handler, budget: int = 100, release_gil: bool = False) -> int:
        """Spawn a push-based actor (green-thread)."""
        return self.rt.spawn(handler, budget=budget, release_gil=release_gil)

    def spawn_py_handler_bounded(self, handler, budget: int, capacity: int, release_gil: bool = False) -> int:
        """Spawn a Python handler with a bounded mailbox capacity."""
        return self.rt.spawn_py_handler_bounded(handler, budget, capacity, release_gil)

    def spawn_virtual(self, handler, budget: int = 100, idle_timeout_ms: Optional[int] = None) -> int:
        """Reserve a PID and lazily activate the actor on first message."""
        return self.rt.spawn_virtual(handler, budget=budget, idle_timeout_ms=idle_timeout_ms)

    def spawn_with_mailbox(self, handler, budget: int = 100) -> int:
        """Spawn a pull-based actor running in a dedicated OS thread."""
        return self.rt.spawn_with_mailbox(handler, budget=budget)

    def spawn_child(self, parent: int, handler, budget: int = 100, release_gil: bool = False) -> int:
        """Spawn a new actor whose lifetime is tied to `parent`."""
        return self.rt.spawn_child(parent, handler, budget, release_gil)

    def spawn_child_pool(self, parent: int, handler, workers: int, budget: int = 100, release_gil: bool = False):
        """Spawn a persistent pool of child actors tied to `parent`."""
        return self.rt.spawn_child_pool(parent, handler, workers, budget, release_gil)

    def spawn_child_with_mailbox(self, parent: int, handler, budget: int = 100) -> int:
        """Like spawn_with_mailbox but killed when parent PID exits."""
        return self.rt.spawn_child_with_mailbox(parent, handler, budget)

    # --- Messaging & Timers ---

    def send(self, pid: int, data: Any) -> bool:
        """Send user data to a PID (automatically serializes to bytes)."""
        return self.rt.send(pid, self._prepare_payload(data))

    def send_named(self, name: str, data: Any) -> bool:
        """Resolve and send by registered name (automatically serializes to bytes)."""
        return self.rt.send_named(name, self._prepare_payload(data))

    def send_after(self, pid: int, delay_ms: int, data: Any) -> int:
        """Schedule a one-shot message to be sent after `delay_ms` milliseconds."""
        return self.rt.send_after(pid, delay_ms, self._prepare_payload(data))

    def send_interval(self, pid: int, interval_ms: int, data: Any) -> int:
        """Schedule a repeating message to be sent every `interval_ms` milliseconds."""
        return self.rt.send_interval(pid, interval_ms, self._prepare_payload(data))

    def set_overflow_policy(self, pid: int, policy: str, target: Optional[int] = None):
        """Configure how a bounded mailbox handles overflow."""
        return self.rt.set_overflow_policy(pid, policy, target)

    def cancel_timer(self, timer_id: int) -> bool:
        """Cancel a previously scheduled timer/interval."""
        return self.rt.cancel_timer(timer_id)

    def send_buffer(self, pid: int, buffer_id: int) -> bool:
        """Zero-Copy send via Buffer ID."""
        return self.rt.send_buffer(pid, buffer_id)

    # --- Local Registry ---

    def register(self, name: str, pid: int):
        """Register a local name."""
        self.rt.register(name, pid)

    def unregister(self, name: str):
        """Unregister a named PID."""
        self.rt.unregister(name)

    def resolve(self, name: str) -> Optional[int]:
        """Look up the PID associated with a name locally."""
        return self.rt.resolve(name)

    def whereis(self, name: str) -> Optional[int]:
        """Alias for resolve (Erlang style)."""
        return self.rt.whereis(name)

    # --- Path-based Registry & Supervision ---

    def register_path(self, path: str, pid: int):
        """Register under a hierarchical path (e.g., /svc/payments/one)."""
        self.rt.register_path(path, pid)

    def unregister_path(self, path: str):
        """Remove a hierarchical path registration."""
        self.rt.unregister_path(path)

    def whereis_path(self, path: str) -> Optional[int]:
        """Exact path resolution."""
        return self.rt.whereis_path(path)

    def list_children(self, prefix: str):
        """List registered entries under a path prefix."""
        return self.rt.list_children(prefix)

    def list_children_direct(self, prefix: str):
        """List only direct children one level below `prefix`."""
        return self.rt.list_children_direct(prefix)

    def watch_path(self, prefix: str):
        """Register (shallow) watch on all direct children under `prefix`."""
        self.rt.watch_path(prefix)

    def children_count(self) -> int:
        """Return number of children registered with the supervisor."""
        return self.rt.children_count()

    def child_pids(self):
        """Return a list of child PIDs currently registered with the supervisor."""
        return self.rt.child_pids()

    def create_path_supervisor(self, path: str):
        """Create a path-scoped supervisor."""
        self.rt.create_path_supervisor(path)

    def remove_path_supervisor(self, path: str):
        """Remove a path-scoped supervisor."""
        self.rt.remove_path_supervisor(path)

    def path_supervisor_watch(self, path: str, pid: int):
        """Register pid with path supervisor."""
        self.rt.path_supervisor_watch(path, pid)

    def path_supervisor_children(self, path: str) -> list:
        """List children supervised by path supervisor."""
        return self.rt.path_supervisor_children(path)

    def spawn_with_path_observed(self, budget: int, path: str) -> int:
        """Spawn an observed handler and register it under `path`."""
        return self.rt.spawn_with_path_observed(budget, path)

    def path_supervise_with_factory(self, path: str, pid: int, factory, strategy: str):
        """Attach a Python factory to restart actors (restartone / restartall)."""
        self.rt.path_supervise_with_factory(path, pid, factory, strategy)

    # --- Remote & Distributed Mesh ---

    def resolve_remote(self, addr: str, name: str) -> Optional[int]:
        """Query a remote node for a PID by name (Blocking)."""
        return self.rt.resolve_remote(addr, name)

    def resolve_remote_py(self, addr: str, name: str) -> Awaitable[Optional[int]]:
        """Query a remote node for a PID by name (Async/Awaitable)."""
        return self.rt.resolve_remote_py(addr, name)

    def is_node_up(self, addr: str) -> bool:
        """Quick network probe to check if a remote node is reachable."""
        return self.rt.is_node_up(addr)

    def listen(self, addr: str):
        """Start TCP server for remote messages and name resolution."""
        self.rt.listen(addr)

    def send_remote(self, addr: str, pid: int, data: Any):
        """Send data to a PID on a remote node (automatically serializes to bytes)."""
        self.rt.send_remote(addr, pid, self._prepare_payload(data))

    def monitor_remote(self, addr: str, pid: int):
        """Watch a remote PID; triggers local supervisor on failure."""
        self.rt.monitor_remote(addr, pid)

    # --- Hot-Swapping ---

    def hot_swap(self, pid: int, new_handler):
        """Update actor logic at runtime."""
        self.rt.hot_swap(pid, new_handler)

    def behavior_version(self, pid: int) -> int:
        """Return the current behavior version for an actor PID."""
        return self.rt.behavior_version(pid)

    def rollback_behavior(self, pid: int, steps: int = 1) -> int:
        """Rollback actor behavior by `steps` hot-swapped versions."""
        return self.rt.rollback_behavior(pid, steps)

    # --- Lifecycle, Mailbox & State ---

    def stop(self, pid: int):
        """Stop an actor and close its mailbox."""
        self.rt.stop(pid)

    def join(self, pid: int):
        """Block until the specified actor exits."""
        self.rt.join(pid)

    def is_alive(self, pid: int) -> bool:
        """Check if an actor is currently alive."""
        return self.rt.is_alive(pid)

    def mailbox_size(self, pid: int) -> Optional[int]:
        """Return the number of queued user messages for the actor with `pid`."""
        return self.rt.mailbox_size(pid)

    def selective_recv(self, pid: int, matcher: Callable, timeout: Optional[float] = None, auto_json: bool = True) -> Awaitable[Optional[Any]]:
        """Return an awaitable that resolves when `matcher(msg)` is True."""
        async def _wrapper():
            res = await self.rt.selective_recv(pid, matcher, timeout)
            if res is not None and auto_json:
                return self._parse_payload(res)
            return res
        return _wrapper()

    def selective_recv_blocking(self, pid: int, matcher: Callable, timeout: Optional[float] = None, auto_json: bool = True) -> Optional[Any]:
        """Blocking convenience wrapper around `selective_recv` for sync code."""
        res = self.rt.selective_recv_blocking(pid, matcher, timeout)
        if res is not None and auto_json:
            return self._parse_payload(res)
        return res

    # --- Global Limits ---

    def set_release_gil_limits(self, max_threads: int, pool_size: int):
        """Set programmatic limits for `release_gil` behavior."""
        self.rt.set_release_gil_limits(max_threads, pool_size)

    def set_release_gil_strict(self, strict: bool):
        """When True, spawning with `release_gil=True` returns an error if limits are exceeded."""
        self.rt.set_release_gil_strict(strict)
