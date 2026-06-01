"""Manual live smoke for kestrel-client against a running Kestrel server.

Not a unit test — run by hand against a real stack (e.g. docker compose up).
First mint a key inside the API container:
    docker compose exec api kestrel-keys create smoke --scope admin

Then:
    KESTREL_URL=http://localhost:8000 KESTREL_API_KEY=kestrel_... \
        uv run python smoke.py
"""

from __future__ import annotations

import asyncio
import os

from kestrel_client import AsyncKestrelClient, KestrelClient

URL = os.environ.get("KESTREL_URL", "http://localhost:8000")
KEY = os.environ.get("KESTREL_API_KEY")


def _describe(message) -> str:
    return f"{type(message).__name__}({getattr(message, 'data', getattr(message, 'elapsed_ms', ''))!r})"


def sync_smoke() -> None:
    print("── sync client ──")
    with KestrelClient(URL, api_key=KEY) as kestrel:
        print("execute:", kestrel.execute("print(2 + 2)").stdout.strip())

        session = kestrel.create_session()
        print("session:", session.session_id[:8])
        kestrel.session_execute(session.session_id, "x = 41")
        print("session_execute:", kestrel.session_execute(session.session_id, "print(x + 1)").stdout.strip())

        print("stream (polling):")
        for message in kestrel.stream(session.session_id, "for i in range(3): print(i)"):
            print("   ", _describe(message))

        kestrel.delete_session(session.session_id)
        print("deleted session")


async def async_smoke() -> None:
    print("── async client ──")
    async with AsyncKestrelClient(URL, api_key=KEY) as kestrel:
        result = await kestrel.execute("print(6 * 7)")
        print("execute:", result.stdout.strip())

        session = await kestrel.create_session()
        print("session:", session.session_id[:8])

        print("stream (websocket):")
        async for message in kestrel.stream(session.session_id, "for i in range(3): print(i)"):
            print("   ", _describe(message))

        await kestrel.delete_session(session.session_id)
        print("deleted session")


if __name__ == "__main__":
    sync_smoke()
    asyncio.run(async_smoke())