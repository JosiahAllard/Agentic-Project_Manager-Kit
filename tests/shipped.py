"""What the shipped-text scrub tests share: the forbidden patterns (no ruling ids, user or machine
paths, and none of the install's own names: the machine config's `scrub_patterns`) and the
frontmatter parse, as in tests/agents/test_agents.py."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


NO_INSTALL_NAMES = "no install names to scrub for: %s"


def install_patterns(home=None):
    """The install's own names (repositories, owner, accounts) from the machine config's
    `scrub_patterns`, case-insensitive. Under `home` when given (none when it sets none); with no
    home, the real machine config's through the home fence's one exception
    (tests.real_install_config), and when that has none the calling test is skipped
    (unittest.SkipTest, NO_INSTALL_NAMES) rather than passing without them. Read at call time,
    never at import, so a malformed machine config fails the test that reads it (ConfigError)
    instead of breaking test collection."""
    if home is None:
        from tests import real_install_config
        cfg, why = real_install_config()
        if cfg is not None and not cfg['scrub_patterns']:
            why = 'the machine config sets no scrub_patterns'
        if cfg is None or why:
            raise unittest.SkipTest(NO_INSTALL_NAMES % why)
        pats = cfg['scrub_patterns']
    else:
        from kitlib.config.user import user_config
        pats = user_config(home)["scrub_patterns"] or []
    return [(re.compile(p, re.I), "install name") for p in pats]


BASE_FORBIDDEN = [(re.compile(r"\bR-\d+"), "ruling id"),
                  (re.compile(r"[A-Za-z]:[\\/]+Users[\\/]", re.I), "Windows user path"),
                  (re.compile(r"[A-Za-z]:[\\/]+(GitHub|AgentTemp|research)[\\/]", re.I),
                   "machine path")]


def forbidden(home=None):
    """BASE_FORBIDDEN plus the install's own names (install_patterns)."""
    return BASE_FORBIDDEN + install_patterns(home)


TEXT_SUFFIXES = {".md", ".json", ".py", ".pyw", ".ps1", ".cmd", ".yml", ".txt"}


def frontmatter(text):
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    return out


def shipped_text_files(folder):
    return sorted(p for p in Path(folder).rglob("*")
                  if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES and "__pycache__" not in p.parts)


def violations(folder, home=None, root=ROOT):
    """[(file, what)] for every forbidden pattern (forbidden(home)) found in a text file under
    folder; each file is named relative to root."""
    patterns = forbidden(home)
    found = []
    for p in shipped_text_files(folder):
        text = p.read_text(encoding="utf-8", errors="replace")
        for rx, what in patterns:
            if rx.search(text):
                found.append((p.relative_to(root).as_posix(), what))
    return found
