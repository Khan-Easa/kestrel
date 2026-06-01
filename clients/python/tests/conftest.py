"""Shared test helpers — build a KestrelClient wired to an httpx.MockTransport."""

from __future__ import annotations

from typing import Callable

import httpx

from kestrel_client import KestrelClient


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> KestrelClient:
    """A KestrelClient whose HTTP goes to an in-memory mock handler.

    The SDK still builds its own httpx.Client (so auth-header construction is
    exercised); only the transport is swapped for a MockTransport.
    """
    return KestrelClient("http://test", transport=httpx.MockTransport(handler), **kwargs)
