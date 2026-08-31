"""Source interface + shared HTTP helper.

Every source - free or paid, YC or social - is a `Source` subclass that returns
a list of `Signal`. That is the whole contract. Adding a platform later
(Product Hunt, Bluesky, Crunchbase) means dropping one file in this folder and
registering it; nothing else changes.
"""

from __future__ import annotations

import abc
import logging

import httpx

from ..models import Signal

log = logging.getLogger("foxy.sources")

# A plain desktop UA. We only ever read public, unauthenticated pages.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=15.0)


def client(**kwargs) -> httpx.Client:
    """Pre-configured HTTP client. Redirects on, sane timeout, browser UA."""
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    headers.update(kwargs.pop("headers", {}))
    return httpx.Client(
        timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT),
        follow_redirects=True,
        headers=headers,
        **kwargs,
    )


class Source(abc.ABC):
    """One monitored surface."""

    name: str = "unnamed"
    label: str = "Unnamed"

    #: False when the source needs a key the user has not supplied. A disabled
    #: source is skipped cleanly and reported as such - never an error.
    @property
    def enabled(self) -> bool:
        return True

    #: Human-readable description of how this source is currently configured,
    #: e.g. "free (search engine)" vs "twitterapi.io". Shown in status output.
    @property
    def mode(self) -> str:
        return "default"

    @abc.abstractmethod
    def fetch(self) -> list[Signal]:
        """Return everything currently visible. Deduplication happens upstream,
        so a source should NOT try to remember what it returned last time - it
        just reports what it can see now."""
        raise NotImplementedError


class SourceError(RuntimeError):
    """Raised by a source when it genuinely cannot complete. The engine catches
    this, records it against the source, and carries on with the others."""
