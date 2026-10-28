"""The load-node registry: the owner's list of nodes, `<PM repo>/.pm-kit/load-nodes.json`.

    {"version": 1,
     "nodes": [{"name": "box1", "address": "192.168.1.50", "user": "pmnode",
                "host_key": "ssh-ed25519 AAAA...", "slots": 1, "memory_mb": 12288,
                "port": 22, "identity_file": "~/.ssh/pmkit_node_ed25519",
                "repos": ["my-repo"]}]}

`port`, `identity_file` and `repos` may be left out (22; ssh's own default key; no repository).
`repos` is the opt-in: the repositories (their machine lock repo names) whose runs may go to the
node, by `node.py run` named or chosen (docs/load-nodes.md). Unknown keys are refused;
notes (`_x`, `comment`) are allowed. Every value that reaches ssh's command line is checked here
against a narrow pattern, so none can be read by ssh as an option or a token.

The file names machines and accounts on the owner's network, so it must never be committed: load()
is refused unless git says the PM repository ignores it (ignored()). Nothing here prints or
starts anything but that one git call.
"""
import base64
import collections
import hashlib
import ipaddress
import os
import re
import struct

from kitlib.config import types as T
from kitlib.config import user
from kitlib.git import cmd as gitcmd

FILE_NAME = 'load-nodes.json'
REL = '.pm-kit/' + FILE_NAME           # the registry inside the PM repository
VERSION = 1
KEY_TYPE = 'ssh-ed25519'

NAME_RE = r'[a-z0-9][a-z0-9-]{0,31}'
USER_RE = r'[a-z_][a-z0-9_-]{0,31}'
REPO_RE = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}'     # no '/'; never '..' (repo_ok)
_LABEL = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?')
_IDENT_BAD = re.compile(r'[\x00-\x1f\x7f"%]')

Node = collections.namedtuple('Node', 'name address user host_key slots memory_mb port identity_file '
                                      'repos', defaults=((),))


class RegistryError(Exception):
    """The registry is missing, cannot be read, is not valid, or is not ignored by git. The
    message names every problem found."""


class Address(T.T):
    """An IPv4 or IPv6 address, or a DNS host name (labels of letters, digits and inner
    hyphens): never a leading '-', an '@', a '%' or white space."""

    def check(self, v, where, errs):
        if not isinstance(v, str) or not address_ok(v):
            errs.append('%s must be an IP address or a host name' % where)

    def describe(self):
        return 'IP address or host name'


class RepoName(T.T):
    """A machine lock repo name that may name a folder: letters, digits, '_', '.', '-', never
    '..' and never a leading '.' or '-'."""

    def check(self, v, where, errs):
        if not isinstance(v, str) or not repo_ok(v):
            errs.append('%s must be a repo name of letters, digits, _ . and - (no ..)' % where)

    def describe(self):
        return 'repo name'


class HostKey(T.T):
    """`ssh-ed25519 <base64>`: the node's host public key, as its
    /etc/ssh/ssh_host_ed25519_key.pub holds it (the trailing comment left off)."""

    def check(self, v, where, errs):
        if not isinstance(v, str) or key_blob(v) is None:
            errs.append('%s must be "ssh-ed25519 <base64 key>", the node\'s '
                        '/etc/ssh/ssh_host_ed25519_key.pub without its comment' % where)

    def describe(self):
        return 'ssh-ed25519 public key'


class IdentityFile(T.T):
    """An absolute path or one under the home folder (`~/...`), with no control character, '"'
    or '%' (ssh expands % tokens)."""

    def check(self, v, where, errs):
        if not (isinstance(v, str) and v.strip() == v and v and not _IDENT_BAD.search(v)
                and (v.startswith('~/') or os.path.isabs(v))):
            errs.append('%s must be an absolute path or ~/path, with no " or %%' % where)

    def describe(self):
        return 'absolute path or ~/path'


NODE = T.Obj({
    'name': T.Field(T.Str(pattern=NAME_RE, what='a name of lowercase letters, digits and -'), None,
                    'the node\'s short name'),
    'address': T.Field(Address(), None, 'IP address or host name on the LAN'),
    'user': T.Field(T.Str(pattern=USER_RE, what='a Linux user name'), None,
                    'the account the main machine\'s key logs in as'),
    'host_key': T.Field(HostKey(), None, 'the node\'s ssh host key, pinned'),
    'slots': T.Field(T.Int(lo=1, hi=1024), None, 'how many jobs it may run at once'),
    'memory_mb': T.Field(T.Int(lo=256), None, 'memory jobs may use, in MB'),
    'port': T.Field(T.Int(lo=1, hi=65535), 22, 'ssh port'),
    'identity_file': T.Field(IdentityFile(), None, 'the main machine\'s private key for it'),
    'repos': T.Field(T.ListOf(RepoName()), [], 'the repositories whose runs it may take'),
}, required=('name', 'address', 'user', 'host_key', 'slots', 'memory_mb'))

SCHEMA = T.Obj({
    'version': T.Field(T.Int(lo=VERSION, hi=VERSION), None, 'always 1'),
    'nodes': T.Field(T.ListOf(NODE), None, 'the nodes'),
}, required=('version', 'nodes'))


def repo_ok(v):
    """True for a repo name a node may use as a folder name (REPO_RE, no '..')."""
    return isinstance(v, str) and bool(re.fullmatch(REPO_RE, v)) and '..' not in v


def address_ok(v):
    if not v or len(v) > 253 or v.startswith('-'):
        return False
    if ':' in v:
        if '%' in v or not re.fullmatch(r'[0-9A-Fa-f:.]+', v):
            return False
        try:
            return ipaddress.ip_address(v).version == 6
        except ValueError:
            return False
    return all(_LABEL.fullmatch(p) for p in v.split('.'))


def key_blob(text):
    """The key bytes of an `ssh-ed25519 <base64>` line, or None when it is not one: the base64
    must decode and the key inside must say it is ssh-ed25519 and hold 32 bytes."""
    parts = text.split(' ')
    if len(parts) != 2 or parts[0] != KEY_TYPE or not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', parts[1]):
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        return None
    name = KEY_TYPE.encode()
    want = struct.pack('>I', len(name)) + name + struct.pack('>I', 32)
    if len(blob) != len(want) + 32 or not blob.startswith(want):
        return None
    return blob


def fingerprint(host_key):
    """`SHA256:<base64>`, as `ssh-keygen -lf` prints the key's fingerprint."""
    digest = hashlib.sha256(key_blob(host_key)).digest()
    return 'SHA256:' + base64.b64encode(digest).decode('ascii').rstrip('=')


def list_lines(nodes, identity_exists=os.path.isfile, resolve=None):
    """One line per node for `node.py list`: where it is, what it may take, the pinned key's
    fingerprint, and whether its identity file is there. `resolve(node)` gives the identity
    file's real path (kitlib.loadnode.remote.identity_path); nothing goes on the network."""
    out = []
    for n in nodes:
        ident = 'ssh default key'
        if n.identity_file:
            real = resolve(n) if resolve else n.identity_file
            ident = '%s (%s)' % (n.identity_file, 'present' if identity_exists(real) else 'MISSING')
        out.append('%s  %s@%s:%d  %d slot%s, %d MB  %s  key %s  repos: %s' % (
            n.name, n.user, n.address, n.port, n.slots, '' if n.slots == 1 else 's', n.memory_mb,
            fingerprint(n.host_key), ident, ', '.join(n.repos) or 'none'))
    return out


def path(home=None):
    """The registry file in the PM repository's `.pm-kit` folder, or None when the machine
    config is not in a PM repository (kitlib.config.user.pm_config_dir)."""
    folder = user.pm_config_dir(home)
    return os.path.join(folder, FILE_NAME) if folder else None


def parse(text, where=FILE_NAME):
    """The Node list `text` describes. RegistryError naming every problem."""
    try:
        data = T.parse_json(text, where)
    except T.ConfigError as e:
        raise RegistryError(str(e))
    errs = []
    SCHEMA.check(data, where, errs)
    if not errs:
        seen = set()
        for i, n in enumerate(data['nodes']):
            if n['name'] in seen:
                errs.append('%s.nodes[%d].name: "%s" is used twice' % (where, i, n['name']))
            seen.add(n['name'])
    if errs:
        raise RegistryError('; '.join(errs))
    return [Node(n['name'], n['address'], n['user'], n['host_key'], n['slots'], n['memory_mb'],
                 n.get('port', 22), n.get('identity_file'), tuple(n.get('repos') or ()))
            for n in data['nodes']]


def ignored(pm_repo, call=gitcmd.call):
    """None when git says the PM repository `pm_repo` ignores the registry, else why not.
    `call` is kitlib.git.cmd.call (a test's stand-in)."""
    r = call(['check-ignore', '-q', '--', REL], cwd=pm_repo)
    if r.rc == 0:
        return None
    if r.rc == 1:
        return ('%s is not ignored by git in %s: add the line "%s" to %s/.pm-kit/.gitignore '
                'first, so the list of your machines is never committed' % (REL, pm_repo, FILE_NAME, pm_repo))
    # call() gives err as text already (decoded, or its own words on a timeout or a git that
    # cannot start), so it is used as it is.
    return 'cannot ask git whether %s is ignored in %s: %s' % (REL, pm_repo, (r.err or '').strip())


def load(file_path, call=gitcmd.call):
    """The Node list in `file_path` (`<PM repo>/.pm-kit/load-nodes.json`). RegistryError when it
    is missing, unreadable, invalid, or not ignored by git."""
    if not file_path:
        raise RegistryError('no PM repository: the machine config is not in one, so there is no '
                            '%s (docs/load-nodes.md)' % REL)
    pm_repo = os.path.dirname(os.path.dirname(file_path))
    try:
        with open(file_path, encoding='utf-8-sig') as f:
            text = f.read()
    except FileNotFoundError:
        raise RegistryError('no load nodes: %s does not exist (docs/load-nodes.md)' % file_path)
    except (OSError, UnicodeDecodeError) as e:
        raise RegistryError('cannot read %s: %s' % (file_path, e))
    why = ignored(pm_repo, call)
    if why:
        raise RegistryError(why)
    return parse(text, file_path)
