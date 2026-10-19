"""One-shot messages shown as toasts on the next rendered page.

Stored in the signed session cookie, so keep them short (cookies cap at ~4KB).
"""

from fastapi import Request

_KEY = "_flash"
_MAX_LEN = 600


def flash(request: Request, message: str, level: str = "info") -> None:
    """level: info | success | error"""
    msgs = list(request.session.get(_KEY, []))
    text = str(message)
    if len(text) > _MAX_LEN:
        text = text[: _MAX_LEN - 1] + "…"
    msgs.append({"m": text, "l": level})
    request.session[_KEY] = msgs[-5:]


def pop_flashes(request: Request) -> list[dict]:
    if not hasattr(request, "session"):
        return []
    return request.session.pop(_KEY, [])
