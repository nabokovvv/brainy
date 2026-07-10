"""Pure URL admission checks shared by dormant research fetchers."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def is_global_ip_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def is_safe_public_http_url(url: str) -> bool:
    """Reject malformed, credentialed, local, and literal non-public HTTP targets.

    Hostname DNS resolution and redirect revalidation remain mandatory in the future
    Stage 2 adapter; this function intentionally performs no network I/O.
    """

    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    if parsed.username or parsed.password or parsed.fragment:
        return False

    normalized_host = hostname.rstrip(".").lower()
    if (
        normalized_host == "localhost"
        or normalized_host.startswith("localhost.")
        or normalized_host.endswith((".local", ".internal"))
    ):
        return False
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        return "." in normalized_host
    return is_global_ip_address(normalized_host)
