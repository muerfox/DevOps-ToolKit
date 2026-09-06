import json

import markupsafe
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR

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


templates.env.filters["tojson"] = _tojson_filter
