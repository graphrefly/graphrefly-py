---
title: 'from_webhook'
description: 'Bridge HTTP webhook callbacks into a GraphReFly source.'
---

Bridge HTTP webhook callbacks into a GraphReFly source.

## Signature

```python
def from_webhook(
    register: Callable[
        [
            Callable[[Any], None],
            Callable[[BaseException | Any], None],
            Callable[[], None],
        ],
        Callable[[], None] | None,
    ],
) -> Node[Any]
```

## Documentation

Bridge HTTP webhook callbacks into a GraphReFly source.

The ``register`` callback wires your runtime/framework callback into GraphReFly and may return
cleanup. It receives three functions: ``emit(payload)``, ``error(err)``, and ``complete()``.

This mirrors the source-adapter style of :func:`from_event_emitter`, but targets HTTP webhook
handlers from frameworks like FastAPI or Flask.

Example (FastAPI):
    ```python
    from fastapi import FastAPI, Request
    from graphrefly.extra import from_webhook

    app = FastAPI()
    bridge: dict[str, object] = {}

    def register(emit, error, complete):
        bridge["emit"] = emit
        bridge["error"] = error
        bridge["complete"] = complete
        return None

    webhook_node = from_webhook(register)

    @app.post("/webhook")
    async def webhook(request: Request):
        payload = await request.json()
        bridge["emit"](payload)
        return {"ok": True}
    ```

Example (Flask):
    ```python
    from flask import Flask, jsonify, request
    from graphrefly.extra import from_webhook

    app = Flask(__name__)
    bridge: dict[str, object] = {}

    def register(emit, error, complete):
        bridge["emit"] = emit
        bridge["error"] = error
        bridge["complete"] = complete
        return None

    webhook_node = from_webhook(register)

    @app.post("/webhook")
    def webhook():
        try:
            bridge["emit"](request.get_json(force=True))
            return jsonify({"ok": True}), 200
        except Exception as exc:
            bridge["error"](exc)
            return jsonify({"ok": False}), 500
    ```
