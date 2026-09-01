"""Hosted mode: one Foxy serving several Slack workspaces.

The monitored data is identical for everyone, so a sweep fetches the five
sources **once** and then fans the results out. Adding a workspace costs one
extra `chat.postMessage`, not another set of scrapes.

Two things are deliberately per-workspace rather than shared:

* **What has been seen.** If the seen-set were global, the second workspace to
  install would be told about nothing, because the first one already consumed
  every company. Each install carries its own namespace.
* **Optional API keys.** Whoever installs can paste in their own serper or
  Anthropic key from the settings page, so one workspace's spend never lands on
  another's.

Bot tokens are encrypted at rest with a key held only in the environment. That
is not a substitute for keeping the database private, but it means a leaked
dump is not immediately a set of live Slack credentials.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import logging
import secrets
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, select

from .config import settings
from .db import Base, Session, session
from sqlalchemy.orm import Mapped, mapped_column

log = logging.getLogger("foxy.installs")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# token encryption
# ---------------------------------------------------------------------------
#
# A small authenticated stream cipher built on HMAC-SHA256, so hosted mode adds
# no dependency. Each value gets a fresh random nonce; the keystream is derived
# from that nonce, and a tag over the ciphertext detects tampering. This is not
# a general-purpose crypto library, and it is not trying to be: it protects one
# short secret at rest against a leaked database dump.


def _root_key() -> bytes:
    raw = settings.encryption_key
    if not raw:
        raise RuntimeError(
            "Set ENCRYPTION_KEY before storing Slack tokens in hosted mode."
        )
    return hashlib.sha256(raw.encode()).digest()


def key_fingerprint() -> str:
    """A short, non-secret id for the key in this environment.

    Stamped into every blob. The web app and the scheduler are separate
    deployments with separately configured secrets, and when their keys drifted
    apart the only symptom was silence: tokens decrypted to "", every sweep took
    the dry-run path, and hundreds of alerts were recorded and never sent. A
    fingerprint turns that into a statement of fact - this row was written by a
    different key - instead of a shrug.
    """
    return hashlib.sha256(_root_key()).hexdigest()[:8]


VERSION = b""


def _keystream(nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(
            _root_key(), nonce + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        counter += 1
    return out[:length]


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    nonce = secrets.token_bytes(16)
    data = plain.encode()
    body = bytes(a ^ b for a, b in zip(data, _keystream(nonce, len(data))))
    tag = hmac.new(_root_key(), nonce + body, hashlib.sha256).digest()[:16]
    stamp = key_fingerprint().encode()  # 8 bytes, not secret
    return base64.urlsafe_b64encode(VERSION + stamp + nonce + tag + body).decode()


class WrongKey(Exception):
    """The blob was encrypted by a different ENCRYPTION_KEY than this one."""


def _decrypt(blob: str) -> str:
    """Decrypt, or raise WrongKey. `decrypt` is the forgiving wrapper."""
    raw = base64.urlsafe_b64decode(blob.encode())

    if raw[:1] == VERSION:
        stamp, raw = raw[1:9].decode(), raw[9:]
        if stamp != key_fingerprint():
            raise WrongKey(
                f"stored under key {stamp}, this environment holds "
                f"{key_fingerprint()}"
            )

    nonce, tag, body = raw[:16], raw[16:32], raw[32:]
    expected = hmac.new(_root_key(), nonce + body, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected):
        # No stamp and a failed tag: an older blob from a different key.
        raise WrongKey("integrity check failed; this is not the key that wrote it")
    return bytes(a ^ b for a, b in zip(body, _keystream(nonce, len(body)))).decode()


def decrypt(blob: str) -> str:
    if not blob:
        return ""
    try:
        return _decrypt(blob)
    except WrongKey as exc:
        log.error("stored token is unreadable here: %s", exc)
        return ""
    except Exception:  # noqa: BLE001 - a malformed blob is not worth crashing over
        log.error("could not decrypt a stored token")
        return ""


def token_problem(blob: str) -> str:
    """Why a stored token cannot be used, or "" when it is fine.

    Reported by the doctor and the health endpoint, so a key mismatch is
    something you read rather than something you deduce from an empty channel.
    """
    if not blob:
        return "no token stored"
    try:
        _decrypt(blob)
        return ""
    except WrongKey as exc:
        return f"encrypted with a different ENCRYPTION_KEY ({exc})"
    except Exception as exc:  # noqa: BLE001
        return f"unreadable: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


class Install(Base):
    """One Slack workspace that has added Foxy."""

    __tablename__ = "installs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(64), index=True)
    team_name: Mapped[str] = mapped_column(String(255), default="")

    token_enc: Mapped[str] = mapped_column(Text, default="")
    channel_id: Mapped[str] = mapped_column(String(64), default="")

    # Optional per-workspace keys, so one install's spend is its own.
    serper_key_enc: Mapped[str] = mapped_column(Text, default="")
    anthropic_key_enc: Mapped[str] = mapped_column(Text, default="")

    min_confidence: Mapped[str] = mapped_column(String(8), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- plan and quota ----------------------------------------------------
    # The free plan is metered by alerts delivered, because that is the thing
    # of value: a workspace that gets nothing has consumed nothing.
    plan: Mapped[str] = mapped_column(String(16), default="free")
    alerts_used: Mapped[int] = mapped_column(Integer, default=0)
    quota_notified: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    last_alert_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- convenience --------------------------------------------------------

    @property
    def token(self) -> str:
        return decrypt(self.token_enc)

    @property
    def serper_key(self) -> str:
        return decrypt(self.serper_key_enc)

    @property
    def anthropic_key(self) -> str:
        return decrypt(self.anthropic_key_enc)

    @property
    def confidence(self) -> float:
        try:
            return float(self.min_confidence)
        except (TypeError, ValueError):
            return settings.min_confidence

    @property
    def quota(self) -> int:
        """Alerts included in this plan. 0 means unlimited."""
        return 0 if self.plan != "free" else settings.free_alert_quota

    @property
    def remaining(self) -> int:
        """Alerts left. A very large number when the plan is unmetered."""
        if self.quota == 0:
            return 10**9
        return max(0, self.quota - (self.alerts_used or 0))

    @property
    def quota_exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def namespace(self) -> str:
        """Prefix for this workspace's seen-set, so installs never consume each
        other's detections."""
        return f"i:{self.id}:"


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


def new_id() -> str:
    return secrets.token_urlsafe(18)


def upsert(
    s: Session,
    *,
    team_id: str,
    team_name: str,
    token: str,
) -> Install:
    """Record an install. Re-installing the same workspace updates it in place
    rather than creating a duplicate that would double every alert."""
    row = s.execute(select(Install).where(Install.team_id == team_id)).scalars().first()
    if row is None:
        row = Install(id=new_id(), team_id=team_id, team_name=team_name)
        s.add(row)
    row.team_name = team_name or row.team_name
    row.token_enc = encrypt(token)
    row.active = True
    row.last_error = None
    return row


def get(s: Session, install_id: str) -> Install | None:
    return s.get(Install, install_id)


def active_installs(s: Session) -> list[Install]:
    rows = s.execute(select(Install).where(Install.active.is_(True))).scalars().all()
    return [r for r in rows if r.channel_id and r.token_enc]


def deactivate(s: Session, install_id: str) -> None:
    row = s.get(Install, install_id)
    if row:
        row.active = False


def summary(s: Session) -> list[dict[str, Any]]:
    """Non-secret view, for the status page and health checks."""
    out = []
    for r in s.execute(select(Install)).scalars().all():
        out.append(
            {
                "team": r.team_name,
                "active": r.active,
                "configured": bool(r.channel_id and r.token_enc),
                "last_alert_at": r.last_alert_at.isoformat() if r.last_alert_at else None,
                "last_error": r.last_error,
            }
        )
    return out


def hosted_enabled() -> bool:
    """Hosted mode needs somewhere durable to keep installs. On SQLite this is
    almost certainly a serverless filesystem that vanishes between requests, so
    it stays off unless a real database is configured."""
    return bool(settings.database_url and not settings.database_url.startswith("sqlite"))
