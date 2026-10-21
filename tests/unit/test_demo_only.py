"""Contract §0 - DEMO ONLY (the owner's hard rule).

The only Trading 212 host anywhere in the repo is the demo host
(``broker_t212.DEMO_URL``); nothing in ``app/``, ``static/`` or ``tests/`` may
name the live host, and no code may read the live credentials or a live gate.
The scans are plain byte/AST scans over the working tree; the forbidden
strings are assembled from parts so this file does not contain them.

Run inside the service image (see test_prices.py for the command).
"""
import ast
import pathlib
import re

import pytest

from app.api import broker as broker_mod
from app.api import broker_t212

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCAN_DIRS = ("app", "static", "tests")

LIVE_HOST = ("live" + "." + "trading212" + "." + "com").encode()
LIVE_ENV_PREFIX = "T212" + "_LIVE"
LIVE_GATE = "LIVE" + "_TRADING" + "_ENABLED"
DEMO_HOST = "demo." + "trading212" + ".com"
API_HOST = "api." + "trading212" + ".com"
DEMO_URL = "https://" + DEMO_HOST + "/api/v0"
ALLOWED_SUBDOMAINS = {"demo", "docs"}       # docs.<host> is only the API-spec link in comments
HOST_RE = re.compile(rb"\b((?:[A-Za-z0-9-]+\.)+)trading212\.com")


def scan_files(root: pathlib.Path = ROOT, dirs=SCAN_DIRS):
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                yield p


def files_containing(needle: bytes, root: pathlib.Path = ROOT, dirs=SCAN_DIRS) -> list[str]:
    return [str(p.relative_to(root)) for p in scan_files(root, dirs)
            if needle in p.read_bytes()]


def trading212_hosts(root: pathlib.Path = ROOT, dirs=SCAN_DIRS) -> dict[str, set[str]]:
    """{host: {files}} for every ``<sub>.trading212.<tld>`` named in the tree."""
    found: dict[str, set[str]] = {}
    for p in scan_files(root, dirs):
        for m in HOST_RE.finditer(p.read_bytes()):
            host = (m.group(1) + b"trading212.com").decode("ascii", "replace")
            found.setdefault(host, set()).add(str(p.relative_to(root)))
    return found


def non_docstring_strings(tree: ast.AST):
    """Every string constant that is not a module/class/function docstring."""
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            yield node.value


def code_mentions(needles: tuple[str, ...], root: pathlib.Path = ROOT) -> list[str]:
    hits = []
    for p in scan_files(root, ("app",)):
        if p.suffix != ".py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        if any(n in s for s in non_docstring_strings(tree) for n in needles):
            hits.append(str(p.relative_to(root)))
    return hits


# ------------------------------------------------------------------- the scans

def test_the_scan_covers_the_tree_it_claims_to_cover():
    files = [str(p.relative_to(ROOT)) for p in scan_files()]
    assert len(files) > 50
    assert "app/api/broker_t212.py" in files and "tests/unit/test_demo_only.py" in files
    assert any(f.startswith("static/") for f in files)


def test_the_live_host_appears_nowhere_in_app_static_or_tests():
    assert files_containing(LIVE_HOST) == []


def test_only_the_demo_host_is_ever_named():
    hosts = trading212_hosts()
    assert DEMO_HOST in hosts                        # the DEMO_URL constant at least
    bad = {h: sorted(f) for h, f in hosts.items()
           if h.split(".")[0] not in ALLOWED_SUBDOMAINS or h.count(".") != 2}
    assert bad == {}


def test_no_code_reads_live_credentials_or_a_live_gate():
    assert code_mentions((LIVE_ENV_PREFIX, LIVE_GATE)) == []


def test_the_demo_url_constant_is_the_demo_host_and_live_has_no_broker():
    assert broker_t212.DEMO_URL == DEMO_URL
    saved = dict(broker_mod._singletons)
    try:
        broker_mod._singletons.clear()
        assert broker_mod.get_broker("live") is None
        assert broker_mod.get_broker("sandbox") is None
        assert broker_mod.get_broker("") is None
        assert broker_mod._singletons == {}          # a non-demo lookup never builds or caches a client
    finally:
        broker_mod._singletons.clear()
        broker_mod._singletons.update(saved)


# ------------------------------------------- the scanners must not pass vacuously

@pytest.fixture
def tree(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "static").mkdir()
    (tmp_path / "tests").mkdir()
    return tmp_path


def test_the_byte_scan_finds_the_live_host_in_any_file_type(tree):
    (tree / "static" / "x.js").write_bytes(b"fetch('https://" + LIVE_HOST + b"/api/v0')")
    (tree / "app" / "ok.py").write_text("URL = '" + DEMO_URL + "'\n")
    assert files_containing(LIVE_HOST, tree) == ["static/x.js"]


def test_the_host_scan_flags_any_other_trading212_subdomain(tree):
    (tree / "app" / "a.py").write_text("A = 'https://api." + "trading212" + ".com/x'\n")
    (tree / "app" / "b.py").write_text("B = 'https://demo." + "trading212" + ".com/x'\n")
    hosts = trading212_hosts(tree)
    assert set(hosts) == {API_HOST, DEMO_HOST}
    assert hosts[API_HOST] == {"app/a.py"}


def test_the_code_scan_sees_real_reads_but_not_docstrings_or_comments(tree):
    (tree / "app" / "reads.py").write_text(
        'import os\nKEY = os.getenv("' + LIVE_ENV_PREFIX + '_KEY")\n')
    (tree / "app" / "gate.py").write_text('GATE = "' + LIVE_GATE + '"\n')
    (tree / "app" / "talks.py").write_text(
        '"""Module doc mentions ' + LIVE_ENV_PREFIX + '_KEY."""\n'
        '# a comment about ' + LIVE_GATE + '\n'
        'def f():\n    """doc: ' + LIVE_GATE + '"""\n    return 1\n')
    assert code_mentions((LIVE_ENV_PREFIX, LIVE_GATE), tree) == ["app/gate.py", "app/reads.py"]
