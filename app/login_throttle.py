"""Escalating rate-limit / ban for the login page, keyed by client IP.

Every 3 failed attempts trip a ban, and the ban duration escalates each time
it's tripped again:

    3 fails  -> banned 10 minutes
    3 more   -> banned 1 hour
    3 more   -> banned 1 day (stays at 1 day for any further violations)

A successful login clears the IP's record entirely. State is persisted in
SQLite (LoginThrottle, one row per IP) so a restart/redeploy doesn't quietly
lift an active ban.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from . import models

FAILS_PER_BAN = 3
MAX_BAN_LEVEL = 3
BAN_DURATIONS: dict[int, dt.timedelta] = {
    1: dt.timedelta(minutes=10),
    2: dt.timedelta(hours=1),
    3: dt.timedelta(days=1),
}


def client_ip(request) -> str:
    return request.client.host if request.client else "unknown"


def check(db: Session, ip: str) -> dict:
    """Is this IP currently banned from attempting a login?"""
    row = db.get(models.LoginThrottle, ip)
    if not row or not row.banned_until:
        return {"banned": False}
    remaining = int((row.banned_until - dt.datetime.utcnow()).total_seconds())
    if remaining <= 0:
        return {"banned": False}
    return {"banned": True, "retry_after_seconds": remaining}


def record_failure(db: Session, ip: str) -> dict:
    row = db.get(models.LoginThrottle, ip)
    if not row:
        row = models.LoginThrottle(ip=ip, fail_count=0, ban_level=0)
        db.add(row)

    row.fail_count += 1
    if row.fail_count >= FAILS_PER_BAN:
        row.ban_level = min(row.ban_level + 1, MAX_BAN_LEVEL)
        duration = BAN_DURATIONS[row.ban_level]
        row.banned_until = dt.datetime.utcnow() + duration
        row.fail_count = 0
        db.commit()
        return {"banned": True, "retry_after_seconds": int(duration.total_seconds()), "ban_level": row.ban_level}

    db.commit()
    return {"banned": False, "attempts_remaining": FAILS_PER_BAN - row.fail_count}


def record_success(db: Session, ip: str) -> None:
    row = db.get(models.LoginThrottle, ip)
    if row:
        db.delete(row)
        db.commit()


def format_duration(seconds: int) -> str:
    seconds = max(seconds, 0)
    if seconds >= 86400:
        return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"
