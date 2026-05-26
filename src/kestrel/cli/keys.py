from __future__ import annotations

"""Phase 7 substep 4: ``kestrel-keys`` CLI.

Operator-facing tool for managing API keys against the same Postgres
database the running service uses. The DB URL is read from
``KESTREL_DATABASE_URL`` only (decision 7.4-url-source) — no flag.

Three subcommands:

- ``create <label> [--scope SCOPE ...]`` — mints a key. The plaintext
token is printed to stdout exactly once and is never recoverable from
the DB. ``--scope`` is repeatable (decision 7.4-scope-shape); omit it
to take ``PostgresApiKeyStore``'s default of ``["execute"]``.
- ``list [--json]`` — prints all keys (active + revoked). Default is a
fixed-width text table for humans; ``--json`` emits a stable JSON
array for scripts (decision 7.4-list-format).
- ``revoke <id>`` — marks the key with the given UUID revoked. UUIDs
only (decision 7.4-revoke-target); operators run ``list`` first to
find the id.

Exit codes: 0 on success, 1 when ``KESTREL_DATABASE_URL`` is missing,
2 when ``revoke`` can't find an active key with the given id (or the
id isn't a valid UUID).

Wired in ``pyproject.toml`` as ``kestrel-keys = "kestrel.cli.keys:main"``.
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime

import structlog

from kestrel.api_keys import PostgresApiKeyStore
from kestrel.config import Settings
from kestrel.db.session import build_engine


def _silence_diagnostic_logging() -> None:
    """Filter INFO-level structlog calls and route any survivors to stderr.

    ``PostgresApiKeyStore.start`` / ``.aclose`` log
    ``postgres_api_key_store_started`` / ``..._stopped`` on every CLI
    invocation. In the long-running service those are useful operational
    breadcrumbs; in an admin CLI they pollute stdout and break the JSON
    contract for ``list --json`` (decision 7.4-list-format). WARNING+
    still surfaces, on stderr, so real failures aren't hidden.
    """
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kestrel-keys",
        description=(
            "Manage Kestrel API keys against the configured Postgres database. "
            "Reads KESTREL_DATABASE_URL from the environment."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Mint a new API key.")
    create.add_argument("label", help="Human-readable label for the key.")
    create.add_argument(
        "--scope",
        action="append",
        default=None,
        metavar="SCOPE",
        help="Grant a scope (repeat for multiple). Default: execute.",
    )

    list_p = subparsers.add_parser(
        "list", help="List all keys (active + revoked)."
    )
    list_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit JSON instead of the default text table.",
    )

    revoke = subparsers.add_parser("revoke", help="Revoke a key by id.")
    revoke.add_argument("key_id", help="UUID of the key to revoke.")

    return parser


def _fmt_dt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


async def _cmd_create(
    store: PostgresApiKeyStore, label: str, scopes: list[str] | None
) -> int:
    token, info = await store.create(label=label, scopes=scopes)
    print("Token (copy now — never recoverable from the DB):")
    print(f"  {token}")
    print()
    print(f"  id:         {info.id}")
    print(f"  label:      {info.label}")
    print(f"  scopes:     {', '.join(info.scopes)}")
    print(f"  created_at: {_fmt_dt(info.created_at)}")
    return 0


async def _cmd_list(store: PostgresApiKeyStore, as_json: bool) -> int:
    keys = await store.list()
    if as_json:
        payload = [
            {
                "id": str(k.id),
                "label": k.label,
                "scopes": list(k.scopes),
                "created_at": k.created_at.isoformat(),
                "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
            }
            for k in keys
        ]
        print(json.dumps(payload, indent=2))
        return 0
    if not keys:
        print("(no keys)")
        return 0
    rows = [
        (
            str(k.id),
            k.label,
            ",".join(k.scopes),
            _fmt_dt(k.created_at),
            _fmt_dt(k.revoked_at) if k.revoked_at else "-",
        )
        for k in keys
    ]
    headers = ("ID", "LABEL", "SCOPES", "CREATED", "REVOKED")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))
    return 0


async def _cmd_revoke(store: PostgresApiKeyStore, key_id_str: str) -> int:
    try:
        key_id = uuid.UUID(key_id_str)
    except ValueError:
        print(f"error: not a valid UUID: {key_id_str}", file=sys.stderr)
        return 2
    revoked = await store.revoke(key_id)
    if revoked:
        print(f"revoked: {key_id}")
        return 0
    print(
        f"error: no active key found with id {key_id}",
        file=sys.stderr,
    )
    return 2


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        print(
            "error: KESTREL_DATABASE_URL is not set; kestrel-keys needs a Postgres URL.",
            file=sys.stderr,
        )
        return 1
    engine = build_engine(settings)
    try:
        store = PostgresApiKeyStore(engine)
        await store.start()
        try:
            if args.command == "create":
                return await _cmd_create(store, args.label, args.scope)
            if args.command == "list":
                return await _cmd_list(store, args.as_json)
            if args.command == "revoke":
                return await _cmd_revoke(store, args.key_id)
            return 1  # unreachable — argparse enforces required=True
        finally:
            await store.aclose()
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    _silence_diagnostic_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())