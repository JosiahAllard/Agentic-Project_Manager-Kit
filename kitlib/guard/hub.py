"""What the guard's hub rules know of the Action Hub (docs/guard.md "Hub secrets"): pure helpers
for kitlib.guard.rules' hub-secrets, hub-owner-verb, hub-mcp and hub-browser.

  secret_folder(config_path)   the hub's token folder beside the machine config file
  stores(hub, pm_data_path)    the folders the hub store may be in
  glob_reaches(pattern, folder) True when a wildcard path can name the folder or a path in it
  ports(hub)                   the hub's listener ports: the defaults and the configured ones
  owner_verb(words)            the owner verb a tools/hub.py command line runs, or None
  split_mcp(tool, sep)         (server, tool) of a host's MCP tool name
  owner_mcp(tail, server)      True for a hub MCP tool only the owner calls
  hub_origins(values, ports)   the loopback host:port texts in an MCP call's input on a hub port

The names are kitlib.hubsite's (auth, settings), copied rather than imported: the guard runs on
every tool call and stays free of the hub site's imports. tests/guard/test_rules_hub.py checks
they are the same. The owner-only MCP tool names are kitlib.hubmcp's own OWNER_ONLY, imported:
that package's __init__ imports nothing. Nothing here reads a file or the environment.
"""
import fnmatch
import ipaddress
import ntpath
import os
import re
import urllib.parse

from kitlib.hubmcp import OWNER_ONLY

# kitlib.hubsite.auth: the token folder beside the machine config, and its files
FOLDER_NAME = 'hub'
OWNER_FILE = 'owner.token'
AGENT_FILE = 'agent.token'
# the key the owner's approval path will sign with, guarded before that path exists
APPROVAL_FILE = 'approval.key'
SECRET_FILES = (OWNER_FILE, AGENT_FILE, APPROVAL_FILE)
# kitlib.hubsite.settings: the store's folder in the PM data worktree, and the two listeners
STORE_DIR = 'hub'
PORT = 8790
TUNNEL_PORT = 8791

# The hub MCP server's owner-only tools: the server's own list (kitlib.hubmcp, which imports
# nothing, so the server is not loaded)
OWNER_MCP_TOOLS = OWNER_ONLY
# words in a hub tool's name that make it the owner's too, whatever a later server calls it
OWNER_MCP_WORDS = ('decide', 'approve')
HUB_SERVER = 'hub'

# the token folder named in a command's text: `.pm-kit` then `hub` with only separators and
# quotes between (`.pm-kit/hub`, `'.pm-kit', 'hub'`); hub.json beside it is not it
FOLDER_RE = re.compile(r'(?i)\.pm-kit\W{1,8}hub(?![\w.-])')
# code that reads a token: a file name, kitlib.hubsite.auth's names, the hub site or the hub tool
INLINE_RE = re.compile(r'(?i)owner\.token|agent\.token|approval\.key|hubsite|(?<![\w-])hub\.py\b|'
                       r'\.pm-kit\W{1,8}hub(?![\w.-])|'
                       r'\b(?:OWNER_FILE|AGENT_FILE|TOKEN_FILES|APPROVAL_FILE|read_token|ensure_tokens)\b')
# programs whose literal words often name the files without touching them: a search pattern, a
# commit message. Their words that are not literal are still read.
NAME_ONLY_PROGRAMS = frozenset(('grep', 'egrep', 'fgrep', 'rg', 'findstr', 'select-string', 'sls', 'git'))

# the browser and computer-use MCP servers: a page they open or a key they type reaches the hub
BROWSER_SERVER_RE = re.compile(r'(?i)chrome|browser|playwright|puppeteer|computer[-_]?use')
HOST_PORT_RE = re.compile(r'(?<![\w.%-])(\[[^\]\s/]+\]|[\w.%-]+):(\d{1,6})(?!\d)')
SCAN_DEPTH = 8
SCAN_ITEMS = 4000


def secret_folder(config_path):
    """The hub's token folder: `hub` beside the machine config file `config_path` (the
    kitlib.config.user DIR_NAME file, as kitlib.hubsite.auth.folder finds it)."""
    return os.path.join(os.path.dirname(config_path), FOLDER_NAME)


def stores(hub, pm_data_path):
    """The folders the hub store may be in: hub.store when it is an absolute path, and
    <pm_data_path>/hub (the default store; held either way, since the hub may move to it)."""
    out = []
    store = hub.get('store') if isinstance(hub, dict) else None
    if isinstance(store, str) and store.strip() and os.path.isabs(store):
        out.append(store)                       # folded by the caller (jail.real), never here
    if pm_data_path:
        out.append(os.path.join(pm_data_path, STORE_DIR))
    return out


GLOB_CHARS = frozenset('*?[')


def has_glob(text):
    return any(c in GLOB_CHARS for c in text)


def _parts(path):
    drive, rest = ntpath.splitdrive(ntpath.normpath(path.replace('/', '\\')))
    return [drive.lower()] + [p for p in rest.lower().split('\\') if p]


def glob_reaches(pattern, folder):
    """True when the absolute wildcard path `pattern` can name `folder` or a path inside it:
    each of the folder's parts is matched by the pattern's part in its place (fnmatch, case
    ignored; `**` matches the rest). A pattern shorter than the folder names a parent, which
    this does not count (docs/guard.md "Hub secrets": a recursive read of a parent)."""
    pat, fol = _parts(pattern), _parts(folder)
    for k, f in enumerate(fol):
        if k >= len(pat):
            return False
        if pat[k] == '**':
            return True
        if not fnmatch.fnmatchcase(f, pat[k]):
            return False
    return True


def ports(hub):
    """The hub's listener ports: the defaults, and hub.port and hub.tunnel_port when they are
    ports (both kept: a config that changes the port leaves the default one an old server's)."""
    out = {PORT, TUNNEL_PORT}
    if isinstance(hub, dict):
        for key in ('port', 'tunnel_port'):
            v = hub.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535:
                out.add(v)
    return out


# ---------------------------------------------------------------- tools/hub.py owner verbs

# tools/hub.py's parser (argparse: subcommand names are exact; a long option may be cut to a
# unique prefix). Verbs with no sub-verb that are the owner's, and the owner sub-verbs.
OWNER_VERBS = ('answer', 'open', 'pair')
OWNER_SUBS = {'todos': ('done',), 'stories': ('add', 'withdraw'), 'to-pm': ('add', 'send'),
              'inbox': ('read',), 'approvals': ('approve', 'deny')}
# options a listed verb takes before its sub-verb, and those that take a value
VERB_OPTS = ('--port', '--json', '--status', '--repo', '--help')
VERB_VALUE_OPTS = ('--port', '--status', '--repo')
# `todos add`: its options, the ones that take one value, and --owner (sends as the owner)
TODO_ADD_OPTS = ('--title', '--detail', '--command', '--repo', '--refs', '--urgent', '--from', '--owner',
                 '--port', '--json', '--help')
TODO_ADD_SINGLE = ('--title', '--detail', '--command', '--repo', '--from', '--port')


class Word(object):
    """One argument after the script: `text`, `dyn` (the text is not what the program gets: a
    variable, a substitution) and `safe` (dyn, but one argument whatever it holds: quoted, and
    passed by a shell that does not split it again)."""
    __slots__ = ('text', 'dyn', 'safe')

    def __init__(self, text, dyn=False, safe=False):
        self.text, self.dyn, self.safe = text, dyn, safe


def long_opt(text, names):
    """The option of `names` the word `text` selects as argparse does (exact, or the one name
    it is a prefix of; `--x=v` by its `--x`), or None (not one, or a prefix of several)."""
    t = text.split('=', 1)[0]
    if not t.startswith('--') or t == '--':
        return None
    if t in names:
        return t
    hits = [n for n in names if n.startswith(t)]
    return hits[0] if len(hits) == 1 else None


def owner_verb(words):
    """What the tools/hub.py arguments `words` (Word, after the script) run that is the owner's,
    as text, or None for an agent verb. A word the guard cannot read where it could be the verb,
    the sub-verb or --owner counts as the owner's."""
    k, n = 0, len(words)
    verb = None
    while k < n:
        w = words[k]
        if w.dyn:
            return 'a verb the guard cannot read (%s)' % w.text
        if w.text == '--':
            k += 1
            if k < n and words[k].dyn:
                return 'a verb the guard cannot read (%s)' % words[k].text
            verb = words[k].text if k < n else None
            break
        if not w.text.startswith('-'):
            verb = w.text
            break
        k += 1
    if verb is None:
        return None
    if verb in OWNER_VERBS:
        return verb
    if verb not in OWNER_SUBS:
        return None
    k += 1
    sub = None
    while k < n:
        w = words[k]
        if w.text == '--' and not w.dyn:
            k += 1
            if k < n and words[k].dyn:
                return '%s with a sub-verb the guard cannot read (%s)' % (verb, words[k].text)
            sub = words[k].text if k < n else None
            break
        if w.dyn and not w.text.startswith('-'):
            return '%s with a sub-verb the guard cannot read (%s)' % (verb, w.text)
        if w.text.startswith('-'):
            name = long_opt(w.text, VERB_OPTS)
            if w.dyn and not (name in VERB_VALUE_OPTS and '=' in w.text and w.safe):
                return '%s with an option the guard cannot read (%s)' % (verb, w.text)
            if name in VERB_VALUE_OPTS and '=' not in w.text:
                k += 1                          # its value
                if k < n and words[k].dyn and not words[k].safe:
                    return '%s with a value the guard cannot read (%s)' % (verb, words[k].text)
            k += 1
            continue
        sub = w.text
        break
    if sub is None:
        return None
    if sub in OWNER_SUBS[verb]:
        return '%s %s' % (verb, sub)
    if (verb, sub) == ('todos', 'add'):
        return todo_add_owner(words[k + 1:])
    return None


def todo_add_owner(words):
    """`todos add --owner` (it files the todo as the owner's own), or a word the guard cannot
    read that could become it; None otherwise."""
    k, n = 0, len(words)
    while k < n:
        w = words[k]
        if w.text == '--' and not w.dyn:
            return None                         # the rest are not options
        name = long_opt(w.text, TODO_ADD_OPTS)
        if name == '--owner':
            return 'todos add --owner'
        if w.dyn and not (name in TODO_ADD_SINGLE and '=' in w.text and w.safe):
            return 'todos add with a word the guard cannot read (%s)' % w.text
        if name in TODO_ADD_SINGLE and '=' not in w.text:
            k += 1                              # its one value: argparse never reads it as an option
            if k < n and words[k].dyn and not words[k].safe:
                return 'todos add with a value the guard cannot read (%s)' % words[k].text
        k += 1
    return None


# ---------------------------------------------------------------- hub MCP tools

def owner_mcp(tail, server):
    """True for a hub MCP tool only the owner calls: OWNER_MCP_TOOLS under any server name, and
    a hub tool (named hub_*, or served by a server named hub) whose name holds decide or approve."""
    if tail in OWNER_MCP_TOOLS:
        return True
    low, srv = tail.lower(), (server or '').lower()
    hub_tool = low.startswith('hub_') or srv == HUB_SERVER or srv.endswith('_' + HUB_SERVER)
    return hub_tool and any(w in low for w in OWNER_MCP_WORDS)


def split_mcp(tool, sep):
    """(server, tail) of an MCP tool name `mcp<sep><server><sep><tool>`; ('', tool) when it has
    no separator."""
    parts = tool.split(sep) if sep else [tool]
    if len(parts) < 3:
        return '', parts[-1]
    return sep.join(parts[1:-1]), parts[-1]


# ---------------------------------------------------------------- the hub origin

def browser_server(server):
    return bool(BROWSER_SERVER_RE.search(server or ''))


def strings(value, depth=0, out=None):
    """Every string in `value` (dicts, lists), at most SCAN_ITEMS, SCAN_DEPTH deep."""
    out = [] if out is None else out
    if len(out) >= SCAN_ITEMS or depth > SCAN_DEPTH:
        return out
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            strings(v, depth + 1, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            strings(v, depth + 1, out)
    return out


def _ipv4_number(host):
    """The host as a browser reads an IPv4 address (WHATWG: one to four parts, each decimal,
    0x hex or 0 octal, the last filling the remaining bytes), as an int, or None."""
    parts = host.split('.')
    if parts and parts[-1] == '':
        parts = parts[:-1]
    if not 1 <= len(parts) <= 4:
        return None
    nums = []
    for p in parts:
        if p == '':
            return None
        try:
            if p[:2].lower() == '0x':
                nums.append(int(p[2:], 16) if p[2:] else 0)
            elif len(p) > 1 and p[0] == '0':
                nums.append(int(p[1:], 8))
            else:
                nums.append(int(p, 10))
        except ValueError:
            return None
    last = nums.pop()
    if any(x > 255 for x in nums) or last >= 256 ** (4 - len(nums)):
        return None
    n = last
    for i, x in enumerate(nums):
        n += x << (8 * (3 - i))
    return n


def loopback(host):
    """True for a host a browser on this machine reaches the machine itself at: localhost and
    *.localhost, a loopback or unspecified IP (IPv4-mapped IPv6 too), and an IPv4 address in a
    browser's other spellings (127.1, 2130706433, 0x7f.0.0.1)."""
    h = urllib.parse.unquote(host).strip().lower().rstrip('.')
    if not h:
        return False
    if h == 'localhost' or h.endswith('.localhost'):
        return True
    if h.startswith('[') and h.endswith(']'):
        h = h[1:-1].split('%', 1)[0]
        try:
            ip = ipaddress.IPv6Address(h)
        except ValueError:
            return False
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return mapped.is_loopback or mapped.is_unspecified
        return ip.is_loopback or ip.is_unspecified
    n = _ipv4_number(h)
    if n is None:
        return False
    return (n >> 24) == 127 or n == 0


def hub_origins(values, hub_ports):
    """The `host:port` texts in the strings `values` that are the hub's own origin: a loopback
    host (loopback) on one of `hub_ports`."""
    found = []
    for s in values:
        for m in HOST_PORT_RE.finditer(s):
            host, port = m.group(1), int(m.group(2))
            if port in hub_ports and loopback(host):
                found.append(m.group(0))
    return found
