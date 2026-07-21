#!/usr/bin/env python3
"""HTTP safety and response helpers for the local Immortal control center."""

from __future__ import annotations

from html import escape
from typing import Any


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_allowed_host(value: str, server_port: int) -> bool:
    """Allow only the loopback names used by the bound local server."""
    host = value.strip().lower()
    if not host:
        return False
    if host.startswith("["):
        name, separator, suffix = host[1:].partition("]")
        if not separator or name != "::1":
            return False
        return suffix in {"", f":{server_port}"}
    if host.count(":") > 1:
        return host == "::1"
    name, separator, port = host.partition(":")
    return name in LOOPBACK_HOSTS and (not separator or port == str(server_port))


def error_payload(
    code: str,
    message: str,
    *,
    detail: str = "",
    retryable: bool = False,
) -> dict[str, dict[str, Any]]:
    return {
        "error": {
            "code": code,
            "message": message,
            "detail": detail,
            "retryable": retryable,
        }
    }


def error_page(title: str, message: str, *, home_href: str = "/") -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        "<style>body{margin:0;background:#08090d;color:#e9edf3;font:15px/1.65 "
        "ui-sans-serif,system-ui;padding:48px;max-width:760px}"
        "a{color:#5ff0dc}code{color:#f6a35c}</style></head><body>"
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p>"
        f'<p><a href="{escape(home_href, quote=True)}">返回 Immortal Control Center</a></p>'
        "</body></html>"
    )


SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; img-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}

LEGACY_SECURITY_HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
}
