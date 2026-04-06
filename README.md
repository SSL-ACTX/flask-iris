# Flask-Iris 🌸

Bring the power of Erlang-style actors, distributed service mesh to your Flask applications.

**Flask-Iris** is an extension that wraps the [Iris distributed runtime fabric](https://github.com/SSL-ACTX/iris). It allows your synchronous HTTP routes to dispatch asynchronous, fault-tolerant background tasks using high-concurrency actors.

📖 **[Full Usage Documentation →](docs/usage.md)**

---

## ⚡ Features

* **Zero-Boilerplate Actors**: Use the `@actor` decorator to spin up background workers that process events outside the web request cycle.
* **Self-Healing Workers**: Use `@supervised_actor` to create path-scoped actors that automatically restart if they crash.
* **Seamless Serialization**: Use `.cast()` and `.cast_path()` to automatically serialize Python dictionaries into JSON bytes and route them to registered actors.
* **Distributed Mesh Ready**: Communicate across node boundaries using Iris's built-in TCP mesh protocol.

---

## 📦 Installation

Since this relies on the underlying Iris runtime, ensure you have it installed:

```bash
pip install flask-iris
```

Or install the latest version directly from GitHub:

```bash
pip install git+https://github.com/SSL-ACTX/flask-iris.git
```

*(Note: Requires Python >= 3.9 and a working installation of the `iris` core package).*

---

## 🚀 Quick Start

Here is a complete example of how to use Flask-Iris to handle fire-and-forget background tasks and supervised fragile workers.

```python
from flask import Flask, jsonify, request
from flask_iris import FlaskIris
import iris

app = Flask(__name__)
iris_ext = FlaskIris(app)

# 1. Spawn a high-performance push actor automatically on startup
@iris_ext.actor("logger", budget=50)
def log_worker(msg: bytes):
    print(f"[Logger] Background task received: {msg.decode('utf-8')}")

# 2. Spawn a fault-tolerant supervised actor
@iris_ext.supervised_actor("/svc/payments", budget=100)
def payment_worker(msg: bytes):
    text = msg.decode('utf-8')
    if "crash" in text:
        raise RuntimeError("Payment gateway failed! Restarting...")
    print(f"[Payments] Processed: {text}")

@app.route("/api/log", methods=["POST"])
def track_event():
    # .cast() automatically converts dicts to JSON and sends to the named actor
    iris_ext.cast("logger", request.json or {})
    return jsonify({"status": "dispatched to logger"})

@app.route("/api/pay", methods=["POST"])
def process_payment():
    # .cast_path() routes to supervised actors in the path registry
    iris_ext.cast_path("/svc/payments/worker", request.json or {})
    return jsonify({"status": "dispatched to payment service"})

if __name__ == "__main__":
    app.run(port=5000)

```

---

## 📚 Documentation

For the complete API reference covering all spawn variants, timers, distributed mesh, hot-swapping, mailbox inspection, and more, see the **[Full Usage Guide](docs/usage.md)**.
