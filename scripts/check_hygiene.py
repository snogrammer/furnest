"""Open-source hygiene: fail when a tracked file carries something personal.

The repository is public, so a real email address, a home domain, a public IP, a
phone number or the coordinates of somewhere real must never be committed. Fixtures
recorded from live pages are scrubbed by :mod:`furnest.sources.sanitize`; this script
is the backstop for everything else (code, docs, config, hand-written fixtures). It
runs in CI and as a pre-commit hook. Secrets are gitleaks' job (``.gitleaks.toml``).

==========  ===========================================================================
Kind        Fails on
==========  ===========================================================================
email       an address whose domain isn't in ``allowed_email_domains``
            (``example.com`` and friends by default)
domain      a ``scheme://host`` URL whose host isn't in ``allowed_domains``
ip          an IPv4/IPv6 literal that is globally routable; loopback, private and the
            documentation ranges (``192.0.2.0/24``, ``2001:db8::/32``, ...) pass
phone       a US number written with separators, unless its exchange is ``555``
location    a coordinate with 3+ decimals (``lat=``/``"lng":``/... or a ``lat, lng``
            pair) outside every ``placeholder_areas`` box; rounded ones pass
deny        any term in the ``HYGIENE_DENYLIST`` environment variable (comma-separated,
            case-insensitive): a CI secret with the names and places that can't be
            listed in the repository itself
==========  ===========================================================================

Configuration is ``.hygiene.toml`` at the repository root. Domain lists match a host
or any subdomain of it. ``[[exempt]]`` entries skip some kinds for files matching a
glob, each with a ``reason``; for a one-off line, put ``hygiene: allow`` (every kind)
or ``hygiene: allow[email,domain]`` in a comment on it.

    uv run --no-project scripts/check_hygiene.py [--root DIR] [PATH ...]

With no paths, every file ``git ls-files`` lists is scanned. Exit status: 0 clean,
1 findings, 2 bad configuration.
"""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

KINDS = ("email", "domain", "ip", "phone", "location", "deny")
CONFIG_NAME = ".hygiene.toml"
DENYLIST_ENV = "HYGIENE_DENYLIST"

# Reserved for documentation and testing (RFC 2606/6761), so always allowed, for URLs
# and emails alike. The same rule as the fixture sanitizer's ``_SAFE_EMAIL_DOMAIN``.
RESERVED_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example",
    "test",
    "invalid",
    "localhost",
)
_DEFAULT_SKIP = ("uv.lock",)


class ConfigError(Exception):
    """``.hygiene.toml`` is unreadable or has a key of the wrong shape."""


@dataclass(frozen=True)
class Area:
    """A box that coordinates may sit in: the fictional places fixtures use."""

    name: str
    lat: tuple[float, float]
    lng: tuple[float, float]


@dataclass(frozen=True)
class Exemption:
    path: str
    kinds: frozenset[str]


@dataclass(frozen=True)
class Config:
    allowed_domains: tuple[str, ...] = ()
    allowed_email_domains: tuple[str, ...] = ()
    placeholder_areas: tuple[Area, ...] = ()
    skip: tuple[str, ...] = _DEFAULT_SKIP
    exempt: tuple[Exemption, ...] = ()
    deny: tuple[str, ...] = ()

    def exempt_kinds(self, path: str) -> frozenset[str]:
        kinds: set[str] = set()
        for exemption in self.exempt:
            if fnmatch.fnmatchcase(path, exemption.path):
                kinds |= exemption.kinds
        return frozenset(kinds)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    kind: str
    value: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.value}"


# --- Loading the configuration ---------------------------------------------------


def load_config(root: Path, environ: dict[str, str]) -> Config:
    path = root / CONFIG_NAME
    raw: dict[str, object] = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{CONFIG_NAME}: {exc}") from exc
    unknown = set(raw) - {
        "allowed_domains",
        "allowed_email_domains",
        "placeholder_areas",
        "skip",
        "exempt",
    }
    if unknown:
        raise ConfigError(f"{CONFIG_NAME}: unknown keys {sorted(unknown)}")
    deny = tuple(t.strip().lower() for t in environ.get(DENYLIST_ENV, "").split(",") if t.strip())
    return Config(
        allowed_domains=_domains(raw, "allowed_domains"),
        allowed_email_domains=_domains(raw, "allowed_email_domains"),
        placeholder_areas=tuple(_area(a) for a in _list(raw, "placeholder_areas", dict)),
        skip=tuple(_list(raw, "skip", str)) if "skip" in raw else _DEFAULT_SKIP,
        exempt=tuple(_exemption(e) for e in _list(raw, "exempt", dict)),
        deny=deny,
    )


def _list[T](raw: dict[str, object], key: str, item: type[T]) -> list[T]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, item) for v in value):
        raise ConfigError(f"{CONFIG_NAME}: {key} must be a list of {item.__name__}")
    return value


def _domains(raw: dict[str, object], key: str) -> tuple[str, ...]:
    return RESERVED_DOMAINS + tuple(d.lower().strip(".") for d in _list(raw, key, str))


def _area(raw: dict[str, object]) -> Area:
    lat, lng = _range(raw.get("lat")), _range(raw.get("lng"))
    if lat is None or lng is None:
        msg = f"{CONFIG_NAME}: placeholder_areas entries need lat = [min, max], lng = [min, max]"
        raise ConfigError(msg)
    return Area(str(raw.get("name", "")), lat, lng)


def _range(value: object) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value):
        return None
    low, high = sorted(float(v) for v in value)
    return (low, high)


def _exemption(raw: dict[str, object]) -> Exemption:
    path, kinds, reason = raw.get("path"), raw.get("kinds"), raw.get("reason")
    if not isinstance(path, str) or not isinstance(reason, str) or not reason.strip():
        raise ConfigError(f"{CONFIG_NAME}: exempt entries need a path and a reason")
    if not isinstance(kinds, list) or not kinds or not set(kinds) <= set(KINDS):
        raise ConfigError(f"{CONFIG_NAME}: exempt {path!r}: kinds must be some of {KINDS}")
    return Exemption(path, frozenset(kinds))


# --- Patterns --------------------------------------------------------------------

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
# ``logo@2x.png`` is an asset name, not an address.
_ASSET_SUFFIX = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|avif|ico|css|js)$", re.IGNORECASE)
# Userinfo (``user:pass@``) is skipped so the host is what's checked.
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://(?:[^/\s@'\"<>]*@)?([A-Za-z0-9.-]+)")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
_IPV6 = re.compile(
    r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}(?:(?:\d{1,3}\.){3}\d{1,3}|[0-9A-Fa-f]{0,4})(?![\w:.])"
)
_PHONE = re.compile(r"(?<![\w+])(?:\+?1[-. ])?\(?([2-9]\d{2})\)?[-. ]([2-9]\d{2})[-. ]\d{4}(?!\d)")
_COORDINATE = r"-?\d{1,3}\.\d+"
# Not after a comma: in ``LAT, LNG = 39.78, -89.65`` the pair rule reads the values.
_KEYED_COORDINATE = re.compile(
    r"(?<![A-Za-z])(?<!,)(?<!, )[\"']?(lat|latitude|lng|lon|long|longitude)[\"']?\s*[:=]\s*[\"']?"
    rf"({_COORDINATE})(?![\d.])",
    re.IGNORECASE,
)
_PRECISE = r"-?\d{1,3}\.\d{3,}"
_COORDINATE_PAIR = re.compile(rf"(?<![\w.])({_PRECISE})\s*[,;]\s*({_PRECISE})(?![\w.])")
_ALLOW = re.compile(r"hygiene:\s*allow(?:\[([a-z, ]+)\])?")
_LAT_KEYS = {"lat", "latitude"}


# --- Checks ----------------------------------------------------------------------


def scan_line(line: str, config: Config) -> Iterator[tuple[str, str]]:
    """Every ``(kind, value)`` in one line that isn't allowed."""
    for match in _EMAIL.finditer(line):
        domain = match.group(1).lower()
        if not _ASSET_SUFFIX.search(domain) and not _listed(domain, config.allowed_email_domains):
            yield "email", match.group(0)
    for match in _URL.finditer(line):
        host = match.group(1).lower().strip(".-")
        if "." in host and not _is_ip(host) and not _listed(host, config.allowed_domains):
            yield "domain", host
    for match in (*_IPV4.finditer(line), *_IPV6.finditer(line)):
        if _is_public_ip(match.group(0)):
            yield "ip", match.group(0)
    for match in _PHONE.finditer(line):
        if match.group(2) != "555":
            yield "phone", match.group(0)
    yield from _locations(line, config.placeholder_areas)
    lowered = line.lower()
    for index, term in enumerate(config.deny, 1):
        if term in lowered:
            # The term itself stays out of the log: it is what the secret is hiding.
            yield "deny", f"term {index} of {DENYLIST_ENV}"


def _listed(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _is_public_ip(text: str) -> bool:
    # Colon-separated text with under two groups, or with no digit at all, is slicing,
    # a scope or a word pair (``x[::2]``, ``std::map``), not an address.
    if ":" in text and (
        sum(1 for group in text.split(":") if group) < 2 or not any(c.isdigit() for c in text)
    ):
        return False
    try:
        return ipaddress.ip_address(text).is_global
    except ValueError:
        return False


def _locations(line: str, areas: tuple[Area, ...]) -> Iterator[tuple[str, str]]:
    for match in _KEYED_COORDINATE.finditer(line):
        key, text = match.group(1).lower(), match.group(2)
        value = float(text)
        if not _precise(text):
            continue
        is_lat = key in _LAT_KEYS
        if abs(value) > (90 if is_lat else 180):
            continue
        if not any(_within(value, a.lat if is_lat else a.lng) for a in areas):
            yield "location", match.group(0)
    for match in _COORDINATE_PAIR.finditer(line):
        first, second = float(match.group(1)), float(match.group(2))
        # Small numbers in pairs are far likelier to be scores than a spot on the equator.
        if min(abs(first), abs(second)) < 1 or not _valid_pair(first, second):
            continue
        if not any(_in_area(first, second, a) or _in_area(second, first, a) for a in areas):
            yield "location", match.group(0)


def _precise(text: str) -> bool:
    return len(text.partition(".")[2]) >= 3


def _within(value: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] <= value <= bounds[1]


def _valid_pair(first: float, second: float) -> bool:
    lat_lng = abs(first) <= 90 and abs(second) <= 180
    lng_lat = abs(first) <= 180 and abs(second) <= 90
    return lat_lng or lng_lat


def _in_area(lat: float, lng: float, area: Area) -> bool:
    return _within(lat, area.lat) and _within(lng, area.lng)


def _allowed_by_marker(line: str) -> frozenset[str]:
    match = _ALLOW.search(line)
    if match is None:
        return frozenset()
    if match.group(1) is None:
        return frozenset(KINDS)
    return frozenset(k.strip() for k in match.group(1).split(","))


# --- Files -----------------------------------------------------------------------


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 -- git from PATH, as in CI
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def scan_file(root: Path, path: str, config: Config) -> Iterator[Finding]:
    exempt = config.exempt_kinds(path)
    if exempt >= set(KINDS):
        return
    try:
        data = (root / path).read_bytes()
    except (FileNotFoundError, IsADirectoryError):  # deleted in the work tree, or a submodule
        return
    if b"\0" in data[:8192]:
        return
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        skip = exempt | _allowed_by_marker(line)
        for kind, value in scan_line(line, config):
            if kind not in skip:
                yield Finding(path, number, kind, value)


def scan(root: Path, paths: list[str], config: Config) -> list[Finding]:
    files = paths or tracked_files(root)
    findings: set[Finding] = set()
    for path in files:
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in config.skip):
            continue
        findings.update(scan_file(root, path, config))
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root.")
    parser.add_argument("paths", nargs="*", help="Files to scan, relative to the root.")
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    try:
        config = load_config(root, dict(os.environ))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    findings = scan(root, args.paths, config)
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"\n{len(findings)} hygiene finding(s). Replace them with placeholders "
            f"(example.com, 555 numbers, the placeholder area), or allow them in {CONFIG_NAME} "
            "with a reason. See CONTRIBUTING.md (Open-source hygiene).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
