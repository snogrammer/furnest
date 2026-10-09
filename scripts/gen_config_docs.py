"""Generate the README configuration table from the ``Settings`` env groups.

    uv run python scripts/gen_config_docs.py           # rewrite the table in README.md
    uv run python scripts/gen_config_docs.py --check   # fail if README.md is out of date

The table lives between the ``config-table`` markers in README.md. CI runs
``--check`` so a new or changed setting cannot ship without it.
"""

from __future__ import annotations

import argparse
import re
import sys
import types
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from furnest.config import env_groups, env_var_name

README = Path(__file__).resolve().parent.parent / "README.md"
BEGIN = "<!-- config-table:begin -->"
END = "<!-- config-table:end -->"
_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)


_SCALARS = {
    "str": "string",
    "int": "integer",
    "bool": "boolean",
    "float": "number",
    "httpurl": "URL",
    "anyurl": "URL",
}


def _type_name(annotation: Any) -> str:  # noqa: ANN401
    """A short, readable type for an env field."""
    origin = get_origin(annotation)
    if origin is not None and getattr(annotation, "__metadata__", None):  # Annotated[...]
        return _type_name(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        parts = [a for a in get_args(annotation) if a is not type(None)]
        return " | ".join(_type_name(a) for a in parts)
    if origin in (list, tuple):
        return "list"
    if origin is dict:
        return "JSON object"
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return " \\| ".join(str(m.value) for m in annotation)
        if issubclass(annotation, SecretStr):
            return "secret"
        return _SCALARS.get(annotation.__name__, annotation.__name__.lower())
    return str(annotation)


def _default(info: FieldInfo) -> str:
    if info.default_factory is not None:
        value: Any = info.default_factory()  # type: ignore[call-arg]
    elif info.default is PydanticUndefined:
        return "required"
    else:
        value = info.default
    if value is None or value in ([], {}, ()):
        return "unset"
    if isinstance(value, Enum):
        return f"`{value.value}`"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    return f"`{value}`"


def render() -> str:
    lines = [
        BEGIN,
        "",
        "| Variable | Type | Default |",
        "| --- | --- | --- |",
    ]
    for group in env_groups().values():
        for name, info in group.model_fields.items():
            var = env_var_name(group, name)
            lines.append(f"| `{var}` | {_type_name(info.annotation)} | {_default(info)} |")
    lines += ["", END]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if README.md is stale")
    args = parser.parse_args()
    text = README.read_text()
    if not _BLOCK.search(text):
        print(f"{README.name}: config-table markers not found", file=sys.stderr)
        return 1
    updated = _BLOCK.sub(lambda _: render(), text)
    if updated == text:
        return 0
    if args.check:
        print(
            "README.md configuration table is out of date; "
            "run `uv run python scripts/gen_config_docs.py`",
            file=sys.stderr,
        )
        return 1
    README.write_text(updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
