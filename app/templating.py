import datetime as dt
import json

import markupsafe
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, INSECURE_SECRET_KEYS, settings
from .flash import pop_flashes

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _tojson_filter(value) -> markupsafe.Markup:
    """Plain Jinja2 (unlike Flask's render_template) has no `tojson` filter
    built in. This one is safe to drop into a *single*-quoted HTML attribute
    (e.g. onclick='...') -- json.dumps() always produces literal double
    quotes around every key/value, which would otherwise close a
    double-quoted attribute the moment a string contains one. Escaping
    <, >, &, and ' as \\uXXXX (same approach Flask's own tojson filter uses)
    keeps the value inert regardless of what's inside it, including a
    project name or script text with a stray quote or angle bracket."""
    dumped = json.dumps(value)
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("'", "\\u0027")):
        dumped = dumped.replace(ch, esc)
    return markupsafe.Markup(dumped)


def _as_datetime(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00")[:26])
        except ValueError:
            return None
        if parsed.tzinfo:
            parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def ago(value) -> str:
    """'3m ago' for a naive-UTC datetime (or ISO string)."""
    when = _as_datetime(value)
    if not when:
        return "—"
    seconds = (dt.datetime.utcnow() - when).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            text = f"{int(seconds // size)}{unit}"
            break
    else:
        text = f"{int(seconds)}s" if seconds >= 5 else ""
    if not text:
        return "just now"
    return f"in {text}" if future else f"{text} ago"


def duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def iso_utc(value) -> str:
    when = _as_datetime(value)
    return when.strftime("%Y-%m-%d %H:%M:%S UTC") if when else ""


templates.env.filters["tojson"] = _tojson_filter
templates.env.filters["ago"] = ago
templates.env.filters["duration"] = duration
templates.env.filters["iso_utc"] = iso_utc
templates.env.globals["get_flashes"] = pop_flashes
templates.env.globals["settings"] = settings
templates.env.globals["static_version"] = "{}-{}".format(
    settings.version,
    int(max(p.stat().st_mtime for p in (BASE_DIR / "app" / "static").rglob("*") if p.is_file())),
)
templates.env.globals["insecure_secret"] = settings.secret_key in INSECURE_SECRET_KEYS
