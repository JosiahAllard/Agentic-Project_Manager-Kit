"""What the shipped-text scrub tests share: the forbidden patterns (no ruling ids or user-profile
paths in BASE_FORBIDDEN; then the install's own: the machine config's `scrub_patterns` as install
names and its `machine_path_folders` as machine paths) and the frontmatter parse, as in
tests/agents/test_agents.py."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


NO_INSTALL_NAMES = "no install names to scrub for: %s"


def machine_path_pattern(folders):
    """The compiled pattern for a drive-rooted path into one of `folders` (the machine config's
    `machine_path_folders`), case-insensitive, separators as kitlib.machinepaths reads them; None
    when there are none."""
    if not folders:
        return None
    from kitlib.machinepaths import SEP
    names = '|'.join(re.escape(f) for f in folders)
    return re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:" + SEP + r"(?:" + names + r")(?![A-Za-z0-9_])",
                      re.I)


def install_patterns(home=None):
    """The install's own: its names (repositories, owner, accounts) from the machine config's
    `scrub_patterns`, case-insensitive, as "install name"; then one "machine path" pattern for
    its `machine_path_folders` (machine_path_pattern). Under `home` when given (none when it sets
    neither); with no home, the real machine config's through the home fence's one exception
    (tests.real_install_config), and when that sets neither key the calling test is skipped
    (unittest.SkipTest, NO_INSTALL_NAMES) rather than passing without them. Read at call time,
    never at import, so a malformed machine config fails the test that reads it (ConfigError)
    instead of breaking test collection."""
    if home is None:
        from tests import real_install_config
        cfg, why = real_install_config()
        if cfg is not None and not cfg['scrub_patterns'] and not cfg['machine_path_folders']:
            why = 'the machine config sets no scrub_patterns or machine_path_folders'
        if cfg is None or why:
            raise unittest.SkipTest(NO_INSTALL_NAMES % why)
    else:
        from kitlib.config.user import user_config
        cfg = user_config(home)
    out = [(re.compile(p, re.I), "install name") for p in cfg["scrub_patterns"] or []]
    rx = machine_path_pattern(cfg["machine_path_folders"])
    return out + ([(rx, "machine path")] if rx else [])


# The one coded machine folder is a user profile: the kit codes no other folder name. The machine
# config's `machine_path_folders` are the install's own and run with its names (install_patterns).
BASE_FORBIDDEN = [(re.compile(r"\bR-\d+"), "ruling id"),
                  (re.compile(r"[A-Za-z]:[\\/]+Users[\\/]", re.I), "Windows user path")]


def forbidden(home=None):
    """BASE_FORBIDDEN plus the install's own names (install_patterns). With no install names this
    skips as install_patterns does, before any pattern runs: a test that scans text uses scan or
    violations, which run BASE_FORBIDDEN first."""
    return BASE_FORBIDDEN + install_patterns(home)


def scan(named_texts, home=None, base=BASE_FORBIDDEN):
    """[(name, what)] for every forbidden pattern found in each (name, text) of named_texts.
    The `base` patterns (BASE_FORBIDDEN unless a test keeps its own) always run. The install's
    own names (install_patterns) run after them; when there are none to read, a base finding is
    returned so the test fails on it, and only a clean base scan raises install_patterns' named
    skip (unittest.SkipTest, NO_INSTALL_NAMES)."""
    named_texts = list(named_texts)
    found = [(name, what) for name, text in named_texts
             for rx, what in base if rx.search(text)]
    try:
        names = install_patterns(home)
    except unittest.SkipTest:
        if found:
            return found
        raise
    return found + [(name, what) for name, text in named_texts
                   for rx, what in names if rx.search(text)]


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
    """[(file, what)] for every forbidden pattern found in a text file under folder (scan: the
    base patterns always, the install names when there are any); each file is named relative to
    root."""
    return scan(((p.relative_to(root).as_posix(), p.read_text(encoding="utf-8", errors="replace"))
                 for p in shipped_text_files(folder)), home)
