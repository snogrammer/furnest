"""furnest: a self-hosted monitor for furnished rental listings.

One package, three processes: ``furnest web`` (dashboard + Discord
interactions), ``furnest worker`` (scheduled scraping and background jobs) and
``furnest migrate`` (one-shot Alembic upgrade). See ``docs/ARCHITECTURE.md``.
"""

from importlib.metadata import version

__version__ = version("furnest")
