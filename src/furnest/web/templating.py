"""The shared Jinja2 environment. Autoescaping is on for every ``.html`` template.

Globals: ``version``, ``csrf_token(request)`` and ``csrf_field(request)`` (the hidden
form input every POST form must include; see ``furnest.web.csrf``).
"""

from pathlib import Path

from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.templating import Jinja2Templates

from furnest import __version__
from furnest.web.csrf import CSRF_FIELD, csrf_token

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["version"] = __version__


def csrf_field(request: Request) -> Markup:
    return Markup('<input type="hidden" name="{}" value="{}">').format(
        CSRF_FIELD, escape(csrf_token(request))
    )


templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["csrf_field"] = csrf_field
