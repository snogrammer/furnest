"""The open-source hygiene check (``scripts/check_hygiene.py``).

Each test builds a throwaway git repository, stages files in it, and runs the script
on it the way CI does. Personal-looking values are assembled at runtime so this file
doesn't trip the real scan of the repository.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_hygiene.py"

# Personal-looking values, split so that no line of this file holds one.
PUBLIC_IP = ".".join(["8", "8", "4", "4"])
EMAIL = "ops" + "@" + "personal-domain.dev"
HOME_DOMAIN = "my-home" + ".dev"
DOMAIN = "git." + HOME_DOMAIN
PHONE = "(212) " + "867-5309"
NYC_LAT, NYC_LNG = "40." + "712776", "-74." + "005974"
AREA = '[[placeholder_areas]]\nname = "Springfield"\nlat = [39.5, 40.1]\nlng = [-89.9, -89.3]\n'


def run(root: Path, *paths: str, deny: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "HYGIENE_DENYLIST"}
    if deny is not None:
        env["HYGIENE_DENYLIST"] = deny
    return subprocess.run(  # noqa: S603 -- our own script, fixed argv
        [sys.executable, str(SCRIPT), "--root", str(root), *paths],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / ".hygiene.toml").write_text(AREA)
    return tmp_path


def stage(root: Path, name: str, content: str | bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    git(root, "add", name)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True)  # noqa: S603, S607 -- fixed argv


def test_clean_repository_passes(repo: Path) -> None:
    stage(repo, "app.py", 'URL = "https://furnest.example.com/x"\nMAIL = "person@example.com"\n')

    result = run(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (f'contact = "{EMAIL}"', "email"),
        (f'url = "https://{DOMAIN}/repo"', "domain"),
        (f'host = "{PUBLIC_IP}"', "ip"),
        (f'phone = "{PHONE}"', "phone"),
        (f'home = {{"lat": {NYC_LAT}, "lng": -89.65}}', "location"),
        (f"spot = ({NYC_LAT}, {NYC_LNG})", "location"),
        (f'<meta name="geo.position" content="{NYC_LAT};{NYC_LNG}">', "location"),
    ],
)
def test_planted_violation_fails(repo: Path, line: str, kind: str) -> None:
    stage(repo, "docs/notes.md", f"intro\n{line}\n")

    result = run(repo)

    assert result.returncode == 1
    assert f"docs/notes.md:2: {kind}: " in result.stdout
    assert "hygiene finding(s)" in result.stderr


@pytest.mark.parametrize(
    "line",
    [
        "bind = 127.0.0.1, 0.0.0.0, 10.0.0.1, 192.168.1.20, 172.16.0.0/12",
        "docs = 192.0.2.1, 198.51.100.9, 203.0.113.7, ::1, 2001:db8::5, ::ffff:198.51.100.9",
        "version = 1.2.3 and python 3.12.4; slice = x[::2]; time 12:30:45; std::map, a::b",
        "mail = a@example.org, b@mail.example.net, c@host.test, logo@2x.png",
        "url = https://user:pass@proxy.example.com and http://localhost:8000",
        "phone = 303-555-1234, (555) 555-0100",
        "rounded = {'lat': 40.71, 'lng': -74.01}; scores = 0.125, 0.875",
        "springfield = {'lat': 39.781721, 'lng': -89.650148}; LAT, LNG = 39.7817, -89.6501",
    ],
)
def test_placeholders_and_non_matches_pass(repo: Path, line: str) -> None:
    stage(repo, "a.py", line + "\n")

    result = run(repo)

    assert result.returncode == 0, result.stdout


def test_allowed_domains_cover_subdomains(repo: Path) -> None:
    (repo / ".hygiene.toml").write_text('allowed_domains = ["my-home.dev"]\n' + AREA)
    stage(repo, "a.md", f"https://{DOMAIN}/x and https://{HOME_DOMAIN}\n")

    assert run(repo).returncode == 0


def test_exemption_covers_only_its_kinds_and_paths(repo: Path) -> None:
    (repo / ".hygiene.toml").write_text(
        AREA + '[[exempt]]\npath = "tests/*"\nkinds = ["email"]\nreason = "inputs"\n'
    )
    stage(repo, "tests/test_x.py", f'"{EMAIL}"\n"{PUBLIC_IP}"\n')
    stage(repo, "src/x.py", f'"{EMAIL}"\n')

    result = run(repo)

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"src/x.py:1: email: {EMAIL}",
        f"tests/test_x.py:2: ip: {PUBLIC_IP}",
    ]


def test_inline_marker_allows_one_line(repo: Path) -> None:
    stage(
        repo,
        "a.py",
        f'A = "{EMAIL}"  # hygiene: allow[email]\n'
        f'B = "{PUBLIC_IP}"  # hygiene: allow\n'
        f'C = "{EMAIL} {PUBLIC_IP}"  # hygiene: allow[ip]\n',
    )

    result = run(repo)

    assert result.stdout.splitlines() == [f"a.py:3: email: {EMAIL}"]


def test_denylist_is_case_insensitive_and_not_echoed(repo: Path) -> None:
    stage(repo, "README.md", "Built by Rosalind Quill in Springfield.\n")

    result = run(repo, deny=" rosalind quill , ")

    assert result.returncode == 1
    assert result.stdout.strip() == "README.md:1: deny: term 1 of HYGIENE_DENYLIST"
    assert "rosalind" not in (result.stdout + result.stderr).lower()


def test_untracked_skipped_and_binary_files_are_ignored(repo: Path) -> None:
    (repo / "untracked.txt").write_text(EMAIL)
    stage(repo, "image.png", b"\x89PNG\0\0" + EMAIL.encode())
    stage(repo, "uv.lock", EMAIL)

    assert run(repo).returncode == 0


def test_explicit_paths_limit_the_scan(repo: Path) -> None:
    stage(repo, "bad.py", f'"{EMAIL}"\n')
    stage(repo, "good.py", "x = 1\n")

    assert run(repo, "good.py").returncode == 0
    assert run(repo, "bad.py").returncode == 1


@pytest.mark.parametrize(
    "config",
    [
        "allowed_domains = [",
        "allowed_domains = 'x.com'\n",
        "surprise = true\n",
        '[[exempt]]\npath = "x"\nkinds = ["email"]\n',
        '[[exempt]]\npath = "x"\nkinds = ["nope"]\nreason = "r"\n',
        "[[placeholder_areas]]\nlat = [1]\nlng = [1, 2]\n",
    ],
)
def test_bad_config_exits_2(repo: Path, config: str) -> None:
    (repo / ".hygiene.toml").write_text(config)

    result = run(repo)

    assert result.returncode == 2
    assert result.stderr.startswith("error: .hygiene.toml")


def test_this_repository_is_clean() -> None:
    result = run(REPO)

    assert result.returncode == 0, result.stdout
