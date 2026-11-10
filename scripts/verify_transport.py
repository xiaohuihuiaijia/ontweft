"""Exercise the installed Uvicorn/WebSockets combination on an ephemeral loopback port."""

import asyncio
import json
import socket
import threading
import time
from importlib import metadata
from pathlib import Path

import httpx
import uvicorn
from websockets.asyncio.client import connect


async def app(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"sdk-runtime-ok"})
    elif scope["type"] == "websocket":
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        message = await receive()
        assert message["type"] == "websocket.receive"
        await send({"type": "websocket.send", "text": message["text"]})
        await send({"type": "websocket.close", "code": 1000})


async def exercise(port):
    async with connect(f"ws://127.0.0.1:{port}/echo", open_timeout=5) as client:
        await client.send("official-sdk-dependency-check")
        assert await client.recv() == "official-sdk-dependency-check"


def main():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "Ephemeral Uvicorn did not start"
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.get(f"http://127.0.0.1:{port}/health")
            assert response.status_code == 200 and response.text == "sdk-runtime-ok"
        asyncio.run(exercise(port))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
    assert not thread.is_alive(), "Ephemeral Uvicorn did not stop"
    result = {
        "uvicorn": metadata.version("uvicorn"),
        "websockets": metadata.version("websockets"),
        "lark_oapi": metadata.version("lark-oapi"),
        "http": "passed",
        "websocket": "passed",
        "server_stopped": True,
        "production_or_live_feishu": False,
    }
    (Path(__file__).resolve().parents[1] / "var" / "feishu-sdk-websocket-runtime.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
