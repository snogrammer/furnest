"""The shared Jinja2 environment. Autoescaping is on for every ``.html`` template.

Filters: ``description_html`` (a scraped description as safe HTML; see
``furnest.web.html``).

Globals: ``version``, ``csrf_token(request)`` and ``csrf_field(request)`` (the hidden
form input every POST form must include; see ``furnest.web.csrf``), and
``photo_src(request, url)``: where a page should load a listing photo from, the
same-origin proxy (``furnest.web.routes.photos``) unless ``IMAGES_PROXY_ENABLED``
is off.
"""

from pathlib import Path
from urllib.parse import urlencode

from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.templating import Jinja2Templates

from furnest import __version__
from furnest.web.csrf import CSRF_FIELD, csrf_token
from furnest.web.html import render_description

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["version"] = __version__


def csrf_field(request: Request) -> Markup:
    return Markup('<input type="hidden" name="{}" value="{}">').format(
        CSRF_FIELD, escape(csrf_token(request))
    )


def photo_src(request: Request, url: str) -> str:
    if not request.app.state.settings.images.proxy_enabled:
        return url
    return "/photo?" + urlencode({"src": url})


templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["csrf_field"] = csrf_field
templates.env.globals["photo_src"] = photo_src
templates.env.filters["description_html"] = render_description
