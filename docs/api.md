# Kestrel API Reference

Base URL: `http://<host>:8000`. All request and response bodies are JSON.
Interactive, always-current docs are served live at `/docs` (Swagger UI) and
`/redoc` — this file is the prose reference.

## Authentication

Most endpoints require a bearer token:

```
Authorization: Bearer kestrel_<token>
```

Tokens are minted with the [`kestrel-keys`](../src/kestrel/cli/keys.py) CLI or the
admin API. `/health` and `/metrics` are open. A missing or invalid token returns
`401`. Admin endpoints additionally require the `admin` scope on the key (else
`403`). For local development, setting `KESTREL_DEV_API_KEY` enables a single
"dev shim" token without a database.

## Conventions

- **`X-Request-ID`** — every response carries one (echoed if the client supplied it,
  else generated). Use it to correlate with server logs and audit rows.
- **Rate limits** — per `(api_key, route_class)` token buckets. Exceeding a limit
  returns `429` with a `Retry-After` header. Route classes: `execute`,
  `session_lifecycle`, `admin`.
- **Timeout is data, not an error** — code that exceeds the wall-clock limit returns
  `200` with `timed_out: true` and `exit_code: -1`, never a `5xx`.
- **Errors** — failures return the matching HTTP status with a JSON `{"detail": "..."}`
  body. Common: `401` (auth), `403` (scope), `404` (session unknown/expired),
  `409` (session busy), `410` (session terminated), `422` (validation), `429`
  (rate limited), `503` (a required backend isn't configured).

---

## `GET /health`

Open liveness probe. → `200 {"status": "ok"}`.

## `GET /metrics`

Open Prometheus metrics (text exposition format). No high-cardinality labels.

---

## `POST /execute`

Run code once in a fresh, throwaway sandbox. Auth: bearer. Rate class: `execute`.

**Request**

```json
{ "code": "print(2 + 2)" }
```

`code` is required, 1–100,000 characters.

**Response** `200`

```json
{
  "stdout": "4\n",
  "stderr": "",
  "exit_code": 0,
  "duration_ms": 42,
  "timed_out": false,
  "stdout_truncated": true,
  "stderr_truncated": false
}
```

`*_truncated` flags indicate output that exceeded the per-stream byte cap.

---

## Sessions

A session is a long-lived container with a persistent Python REPL kernel;
variables and imports persist across executes. The `session_id` is an unguessable
UUID — knowing it is the access right for that session.

### `POST /sessions`

Create a session. Auth: bearer. Rate class: `session_lifecycle`. → `201`

```json
{ "session_id": "…", "created_at": "2026-06-01T08:00:00+00:00", "last_used": "2026-06-01T08:00:00+00:00" }
```

### `GET /sessions`

List active sessions. → `200 {"sessions": [ <session>, … ]}`.

### `GET /sessions/{session_id}`

Session metadata. → `200` (a session object) or `404`.

### `DELETE /sessions/{session_id}`

Terminate a session and clean up its container. → `204` (idempotent) or `404`.

### `POST /sessions/{session_id}/execute`

Run code in the session. Auth: bearer. Rate class: `execute`. → `200`

The body is `{ "code": "…" }`. The response is the `/execute` shape **plus** rich
outputs:

```json
{
  "stdout": "", "stderr": "", "exit_code": 0, "duration_ms": 120,
  "timed_out": false, "stdout_truncated": false, "stderr_truncated": false,
  "outputs": [
    { "type": "plot", "mime_type": "image/png", "data": "<base64>" },
    { "type": "dataframe", "mime_type": "application/json",
      "data": {"index": [...], "columns": [...], "data": [[...]]}, "shape": [10, 3] },
    { "type": "file", "mime_type": "text/csv", "filename": "out.csv", "data": "<base64>" }
  ],
  "dropped_outputs": [
    { "type": "file", "reason": "per_output_cap", "size_bytes": 9000000, "filename": "big.bin" }
  ]
}
```

`outputs` are captured plots (matplotlib), DataFrames (pandas, via the last
expression), and files written to `/workspace/outputs/`. Outputs exceeding a
size/count cap are reported in `dropped_outputs` (`reason` ∈ `per_output_cap` /
`total_cap` / `file_count_cap`) rather than truncated. Errors: `404`, `409`
(another execute already in flight), `410` (kernel dead).

---

## Streaming

### `WS /sessions/{session_id}/execute/stream`

Stream a session execute over a WebSocket. Auth: bearer via the `Authorization`
header **or** a `?token=` query parameter (for browser clients that can't set
headers). Rate class: `execute` (one token per execute message).

1. Connect. The server authenticates at the handshake.
2. Send one text frame: `{ "code": "…" }`.
3. Receive a sequence of JSON message frames, each discriminated by `type`:

| `type` | Fields | Meaning |
|---|---|---|
| `stdout` | `data` | a chunk of stdout |
| `stderr` | `data` | a chunk of stderr |
| `heartbeat` | `elapsed_ms` | keep-alive during silent intervals |
| `result` | full session-execute result + `request_id` | terminal — the final result |
| `error` | `code`, `detail`, `request_id` | terminal — a stream-level error |

Close codes: `1000` normal, `1011` server error, `4401` auth, `4404` not found,
`4409` busy, `4410` terminated, `4429` rate limited.

### `POST /sessions/{session_id}/execute/polling`

HTTP fallback for clients that can't use WebSockets. Starts an execute and returns
a handle immediately. Rate class: `execute`. → `200 { "execution_id": "…" }`.

### `GET /sessions/{session_id}/executions/{execution_id}`

Read accumulated output for a polling execute. Rate class: `session_lifecycle`.
Query params: `since` (cursor, default 0) and `wait` (long-poll seconds, clamped
server-side). → `200`

```json
{ "messages": [ <stream message>, … ], "next_cursor": 7, "done": false, "request_id": "…" }
```

`messages` are the same discriminated-union frames as the WebSocket. Pass
`next_cursor` as the next `since`. When `done` is `true`, the execute has finished
and all messages have been delivered — stop polling.

---

## Admin

All admin endpoints require a key with the `admin` scope (`403` otherwise) and
return `503` when the relevant backend (`postgres` key store / audit log) isn't
configured. Rate class: `admin`.

### `GET /admin/keys`
List all API keys (active + revoked), newest first. → `200 {"keys": [ … ]}`. Key
objects expose `id`, `label`, `created_at`, `revoked_at`, `scopes` — never a token.

### `POST /admin/keys`
Mint a key. Body `{ "label": "...", "scopes": ["execute"] | ["execute","admin"] | null }`
(null → default `["execute"]`). → `201` — the key object **plus** a `token` field,
returned this once and never recoverable afterward.

### `DELETE /admin/keys/{key_id}`
Revoke a key. → `204` (idempotent — already-revoked also `204`) or `404` (unknown id).

### `GET /admin/sessions`
List sessions across the process. → `200 {"sessions": [ … ]}`.

### `GET /admin/audit`
Read the audit log, newest first. Query params: `limit` (1–999) and `before_ts`
(cursor). → `200 {"events": [ … ], "next_before_ts": "…"|null}`. Pass
`next_before_ts` as the next `before_ts`; `null` means the last page.
