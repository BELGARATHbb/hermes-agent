"""CLI handlers for the ``hermes proxy`` subcommand."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any

from hermes_cli.proxy.adapters import ADAPTERS, get_adapter
from hermes_cli.proxy.server import (
    AIOHTTP_AVAILABLE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    is_loopback_host,
    run_server,
)

logger = logging.getLogger(__name__)

_MAX_CLIENT_AUTH_TOKEN_CHARS = 4096


def _read_client_auth_token(path: str) -> str:
    """Read a bounded token from an owner-only regular file."""
    token_path = Path(path).expanduser()
    metadata = token_path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Proxy auth token path must be a regular file (not a symlink).")
    if os.name != "nt":
        if metadata.st_mode & 0o077:
            raise ValueError("Proxy auth token file must be owner-only (mode 0600).")
        get_euid = getattr(os, "geteuid", None)
        if get_euid is not None and metadata.st_uid != get_euid():
            raise ValueError("Proxy auth token file must be owned by the current user.")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(token_path, flags)
    except OSError as exc:
        raise ValueError("Proxy auth token path must be a readable regular file.") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise ValueError("Proxy auth token file changed while it was opened.")
        if os.name != "nt":
            if opened.st_mode & 0o077:
                raise ValueError("Proxy auth token file must be owner-only (mode 0600).")
            get_euid = getattr(os, "geteuid", None)
            if get_euid is not None and opened.st_uid != get_euid():
                raise ValueError("Proxy auth token file must be owned by the current user.")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            token = handle.read(_MAX_CLIENT_AUTH_TOKEN_CHARS + 1).strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if not token:
        raise ValueError("Proxy auth token file is empty.")
    if len(token) > _MAX_CLIENT_AUTH_TOKEN_CHARS:
        raise ValueError("Proxy auth token exceeds the 4096-character limit.")
    if any(character.isspace() for character in token):
        raise ValueError("Proxy auth token must be a single value without whitespace.")
    return token


def _print_aiohttp_missing() -> None:
    print(
        "hermes proxy requires aiohttp. Run `hermes setup` to install it.",
        file=sys.stderr,
    )


def cmd_proxy_start(args: Any) -> int:
    """Run the proxy server in the foreground.

    Returns process exit code (0 on clean shutdown).
    """
    if not AIOHTTP_AVAILABLE:
        _print_aiohttp_missing()
        return 1

    provider = getattr(args, "provider", None) or "nous"
    try:
        adapter = get_adapter(provider)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not adapter.is_authenticated():
        auth_hint = getattr(adapter, "auth_hint", f"hermes auth add {adapter.name}")
        print(
            f"Not logged into {adapter.display_name}. "
            f"Run `{auth_hint}` first.",
            file=sys.stderr,
        )
        return 2

    host = getattr(args, "host", None) or DEFAULT_HOST
    port = getattr(args, "port", None) or DEFAULT_PORT
    if adapter.loopback_only and not is_loopback_host(host):
        print(
            f"Error: {adapter.display_name} proxy is loopback-only; "
            f"refusing bind host {host!r}.",
            file=sys.stderr,
        )
        return 2

    token_file = getattr(args, "auth_token_file", None)
    if adapter.requires_client_auth and not token_file:
        print(
            f"Error: {adapter.display_name} requires client authentication; "
            "provide an owner-only regular file with --auth-token-file.",
            file=sys.stderr,
        )
        return 2
    client_auth_token = None
    if token_file:
        try:
            client_auth_token = _read_client_auth_token(str(token_file))
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    client_auth_message = (
        "  Client auth:    required (bearer from owner-only token file)\n"
        if client_auth_token
        else "  Client auth:    any bearer accepted\n"
    )

    print(
        f"Starting Hermes proxy for {adapter.display_name}\n"
        f"  Listening on:  http://{host}:{port}/v1\n"
        f"  Forwarding to: (resolved per-request from your subscription)\n"
        f"{client_auth_message}"
        f"\n"
        f"Press Ctrl+C to stop.",
        file=sys.stderr,
    )

    try:
        asyncio.run(
            run_server(
                adapter,
                host=host,
                port=port,
                client_auth_token=client_auth_token,
            )
        )
    except KeyboardInterrupt:
        print("\nproxy: stopped", file=sys.stderr)
    except OSError as exc:
        print(f"proxy: failed to bind {host}:{port}: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_proxy_status(args: Any) -> int:
    """Print the status of each configured upstream adapter."""
    print("Hermes proxy upstream adapters\n")
    for name in sorted(ADAPTERS):
        adapter = get_adapter(name)
        if not adapter.is_authenticated():
            print(f"  [{name:8s}] {adapter.display_name} — not logged in")
            continue
        try:
            cred = adapter.get_credential()
        except Exception as exc:
            print(
                f"  [{name:8s}] {adapter.display_name} — credentials need attention "
                f"({exc})"
            )
            continue
        expires = f" (bearer expires {cred.expires_at})" if cred.expires_at else ""
        print(f"  [{name:8s}] {adapter.display_name} — ready{expires}")
    print(
        "\nStart the proxy with: hermes proxy start [--provider <name>]"
    )
    return 0


def cmd_proxy_list_providers(args: Any) -> int:
    """List available proxy upstream providers."""
    print("Available proxy upstream providers:")
    for name in sorted(ADAPTERS):
        adapter = get_adapter(name)
        print(f"  {name}  — {adapter.display_name}")
    return 0


def cmd_proxy(args: Any) -> int:
    """Dispatch ``hermes proxy <subcommand>``."""
    sub = getattr(args, "proxy_command", None)
    if sub == "start":
        return cmd_proxy_start(args)
    if sub == "status":
        return cmd_proxy_status(args)
    if sub in {"providers", "list"}:
        return cmd_proxy_list_providers(args)
    # No subcommand → print short help.
    print(
        "hermes proxy — local OpenAI-compatible proxy that attaches your\n"
        "OAuth-authenticated provider credentials to outbound requests.\n"
        "\n"
        "Subcommands:\n"
        "  hermes proxy start [--provider codex|nous|xai] [--host 127.0.0.1] [--port 8645]\n"
        "      [--auth-token-file PATH]\n"
        "      Run the proxy in the foreground.\n"
        "  hermes proxy status\n"
        "      Show which upstream adapters are ready.\n"
        "  hermes proxy providers\n"
        "      List available upstream providers.\n",
        file=sys.stderr,
    )
    return 0


__all__ = [
    "cmd_proxy",
    "cmd_proxy_start",
    "cmd_proxy_status",
    "cmd_proxy_list_providers",
]
