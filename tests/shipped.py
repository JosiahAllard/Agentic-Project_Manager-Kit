"""What the shipped-text scrub tests share: the forbidden patterns (no ruling ids, user or machine
paths, and none of the install's own names: the machine config's `scrub_patterns`) and the
frontmatter parse, as in tests/agents/test_agents.py."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def install_patterns(home=None):
    """The install's own names (repositories, owner, accounts) from the machine config's
    `scrub_patterns` (under `home`, default the user's), case-insensitive; none when it sets none.
    Read at call time, never at import, so a malformed machine config fails the test that reads
    it (ConfigError) instead of breaking test collection."""
    from kitlib.config.user import user_config
    return [(re.compile(p, re.I), "install name")
            for p in user_config(home)["scrub_patterns"] or []]


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
