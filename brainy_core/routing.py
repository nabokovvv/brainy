"""Provider-neutral request routing intent."""

from __future__ import annotations

from enum import Enum


class RouteIntent(str, Enum):
    """The user-selected path captured when a request is accepted."""

    LOCAL = "local"
    WEB = "web"
