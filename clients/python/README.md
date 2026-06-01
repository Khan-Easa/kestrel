# kestrel-client

Python client SDK for [Kestrel](https://github.com/Khan-Easa/kestrel) — a self-hosted, sandboxed Python code execution service.

```python
from kestrel_client import KestrelClient

with KestrelClient("http://localhost:8000", api_key="kestrel_...") as kestrel:
    result = kestrel.execute("print(2 + 2)")
    print(result.stdout)  # "4\n"
```

Sync (`KestrelClient`) and async (`AsyncKestrelClient`) clients cover stateless
execution, sessions, rich outputs, streaming, and the polling fallback. Full
quickstart in the Phase 8 docs.
