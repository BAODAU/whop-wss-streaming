from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Whop Pulse Hook", version="0.1.0")


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "message": "Barebones FastAPI service is up.",
        "hook_script": "Run `python -m app.pulse_client` to watch the Pulse WebSocket.",
    }
