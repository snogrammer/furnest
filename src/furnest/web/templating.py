"""The shared Jinja2 environment. Autoescaping is on for every ``.html`` template."""

from pathlib import Path

from starlette.templating import Jinja2Templates

from furnest import __version__

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["version"] = __version__
