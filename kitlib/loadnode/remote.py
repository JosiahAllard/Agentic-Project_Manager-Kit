"""Talking to a load node over ssh: its status, and the run verbs of `node.py run`.

One ssh start per node, an argument list (never a shell), through kitlib.proc.spawn:

    ssh -T -F none -o BatchMode=yes -o StrictHostKeyChecking=yes
        -o HostKeyAlias=pmkit-node-<name> -o UserKnownHostsFile="<temp file>"
        -o GlobalKnownHostsFile=none -o CheckHostIP=no -o UpdateHostKeys=no
        -o ConnectTimeout=<s> [-i <key> -o IdentitiesOnly=yes] -l <user> -p <port> -- <address> status

`-F none` keeps the user's ssh config out. The host key is pinned: the only known_hosts ssh reads
is a temp file holding the registry's key under the alias `pmkit-node-<name>`, written for this
call and removed after it, so a node whose key differs is refused (BatchMode: no prompt) and ssh
learns nothing into ~/.ssh. The command word is fixed (`status`); on the node the key's forced
command (optional/loadnode/loadnode.py) runs whatever it is sent as nothing but that word.

A reply is untrusted: kitlib.loadnode.reply.parse checks it. Nothing here prints.

Link holds one node's pinned known_hosts file for the length of a `node.py run` job and makes
the same argument list for each run verb (`start`, `follow`, `fetch`, `clean`, `stop`): the verb is
the one word sent, its parameters one JSON line on ssh's stdin. A streamed verb (`follow`,
`fetch`) adds ssh's keep-alive, so a node that drops off is noticed. git's push to the node's
mirror uses the same options through GIT_SSH_COMMAND (git_env), never the user's ssh config.
"""
import collections
import concurrent.futures
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import kitlib.platform
from kitlib.config import user
from kitlib.loadnode import reply as rp
from kitlib.proc import spawn

ALIAS = 'pmkit-node-%s'
COMMAND = 'status'                       # the one word sent; the node serves nothing else
TIMEOUT_S = 20                           # the whole call
CONNECT_S = 8                            # ssh's ConnectTimeout
OUT_CAP = 64 * 1024                      # a reply longer than this is not a status reply
ERR_CAP = 200                            # characters of ssh's last error line kept
ALIVE_S = 10                             # a streamed verb's ServerAliveInterval
ALIVE_COUNT = 3                          # ... and ServerAliveCountMax
MIRROR = 'pmkit-node/mirrors/%s.git'     # the mirror's path in a push URL (the node maps it)

OK = 'ok'
TIMED_OUT = 'timed out'
HOST_KEY = 'host key mismatch'
UNREACHABLE = 'unreachable'
BAD_REPLY = 'bad reply'
NODE_ERROR = 'node error'                # the node answered ok false
NO_SSH = 'no ssh'

Result = collections.namedtuple('Result', 'name state reply detail')
# `state` one of OK, TIMED_OUT, HOST_KEY, UNREACHABLE, BAD_REPLY, NODE_ERROR, NO_SSH; `reply` the
# checked reply (reply.parse) for OK, else None; `detail` a short sanitized reason, else ''.

_KEY_FAILS = ('Host key verification failed', 'REMOTE HOST IDENTIFICATION HAS CHANGED',
              'No ED25519 host key is known')


def ssh_exe():
    """Windows' own OpenSSH client when it is there, else the first ssh on PATH, else None."""
    root = os.environ.get('SystemRoot')
    if kitlib.platform.NAME == 'windows' and root:
        own = os.path.join(root, 'System32', 'OpenSSH', 'ssh.exe')
        if os.path.isfile(own):
            return own
    return shutil.which('ssh')


def known_hosts_line(node):
    """The one line of the temp known_hosts file: the registry's key under the node's alias."""
    return '%s %s\n' % (ALIAS % node.name, node.host_key)


def identity_path(node, home=None):
    """The node's identity file with a leading ~/ made the home folder, or None."""
    f = node.identity_file
    if not f:
        return None
    if f.startswith('~/'):
        return os.path.join(home or user.home_dir(), *f[2:].split('/'))
    return f


def ssh_options(node, known_hosts, connect_s=CONNECT_S, home=None, keepalive=False):
    """ssh's options for `node`, from -T to -p: everything but the program, the address and the
    command word (the module docstring). `known_hosts` is the temp file's path; ssh reads an -o
    value as words, so it is quoted, and a path holding '"' or '%' (a token to ssh) is refused
    with ValueError. keepalive: ServerAliveInterval and CountMax for a streamed verb."""
    if '"' in known_hosts or '%' in known_hosts:
        raise ValueError('the temp known_hosts path cannot be given to ssh: %s' % known_hosts)
    argv = ['-T', '-F', 'none',
            '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', 'HostKeyAlias=' + ALIAS % node.name,
            '-o', 'UserKnownHostsFile="%s"' % known_hosts,
            '-o', 'GlobalKnownHostsFile=none',
            '-o', 'CheckHostIP=no',
            '-o', 'UpdateHostKeys=no',
            '-o', 'ConnectTimeout=%d' % connect_s]
    if keepalive:
        argv += ['-o', 'ServerAliveInterval=%d' % ALIVE_S, '-o', 'ServerAliveCountMax=%d' % ALIVE_COUNT]
    ident = identity_path(node, home)
    if ident:
        argv += ['-i', ident, '-o', 'IdentitiesOnly=yes']
    return argv + ['-l', node.user, '-p', str(node.port)]


def ssh_argv(node, ssh, known_hosts, connect_s=CONNECT_S, home=None, command=COMMAND,
             keepalive=False):
    """The ssh argument list for one call of the word `command` (default `status`)."""
    return ([ssh] + ssh_options(node, known_hosts, connect_s, home, keepalive)
            + ['--', node.address, command])


def _drain(stream, cap, into):
    """Read `stream` to its end, keeping its first cap + 1 bytes in the bytearray `into`: a node
    that sends without end costs no memory past that."""
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            break
        room = cap + 1 - len(into)
        if room > 0:
            into += chunk[:room]


def default_runner(argv, timeout_s):
    """Run `argv` with no stdin: (rc, stdout bytes, stderr bytes), each cut at its cap plus one
    byte (so an over-long reply still shows as one), or raise subprocess.TimeoutExpired once
    `timeout_s` is past, ssh ended first."""
    p = spawn.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = bytearray(), bytearray()
    readers = [threading.Thread(target=_drain, args=(p.stdout, OUT_CAP, out), daemon=True),
               threading.Thread(target=_drain, args=(p.stderr, OUT_CAP, err), daemon=True)]
    for t in readers:
        t.start()
    try:
        p.wait(timeout_s)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        raise
    finally:
        for t, s in zip(readers, (p.stdout, p.stderr)):
            t.join(5)
            if not t.is_alive():             # a reader still blocked keeps its pipe (daemon)
                s.close()
    return p.returncode, bytes(out), bytes(err)


def _last_line(err):
    lines = [x for x in err.decode('utf-8', 'replace').splitlines() if x.strip()]
    return rp.clean(lines[-1], ERR_CAP) if lines else ''


def classify(name, rc, out, err):
    """The Result of one finished ssh call."""
    text = err.decode('utf-8', 'replace')
    if any(k in text for k in _KEY_FAILS):
        return Result(name, HOST_KEY, None, 'the node\'s host key is not the one in the registry')
    if rc == 255:
        return Result(name, UNREACHABLE, None, _last_line(err) or 'ssh exited 255')
    if len(out) > OUT_CAP:
        return Result(name, BAD_REPLY, None, 'the reply is longer than %d bytes' % OUT_CAP)
    try:
        got = rp.parse(out.decode('utf-8'))
    except (UnicodeDecodeError, rp.ReplyError) as e:
        why = 'not UTF-8' if isinstance(e, UnicodeDecodeError) else str(e)
        return Result(name, BAD_REPLY, None, 'exit %d: %s' % (rc, why))
    if not got['ok']:
        return Result(name, NODE_ERROR, None, got['error'])
    if rc != 0:
        return Result(name, BAD_REPLY, None, 'an ok reply with exit %d' % rc)
    return Result(name, OK, got, '')


def query(node, runner=default_runner, ssh=None, timeout_s=TIMEOUT_S, home=None):
    """Ask `node` for its status: a Result. `runner(argv, timeout_s)` gives (rc, out, err) or
    raises subprocess.TimeoutExpired (a test's stand-in; default_runner starts ssh). Never
    raises for a node that fails."""
    ssh = ssh or ssh_exe()
    if not ssh:
        return Result(node.name, NO_SSH, None, 'no ssh client: install Windows\' OpenSSH client')
    fd, kh = tempfile.mkstemp(prefix='pmkit-known-hosts-', suffix='.txt')
    try:
        with os.fdopen(fd, 'w', encoding='ascii', newline='\n') as f:
            f.write(known_hosts_line(node))
        try:
            argv = ssh_argv(node, ssh, kh, home=home)
        except ValueError as e:
            return Result(node.name, NO_SSH, None, str(e))
        try:
            rc, out, err = runner(argv, timeout_s)
        except subprocess.TimeoutExpired:
            return Result(node.name, TIMED_OUT, None, 'no answer in %d s' % timeout_s)
        except OSError as e:
            return Result(node.name, NO_SSH, None, 'cannot start ssh: %s' % (e.strerror or e))
        return classify(node.name, rc, out, err)
    finally:
        try:
            os.remove(kh)
        except OSError:
            pass


def query_all(nodes, runner=default_runner, ssh=None, timeout_s=TIMEOUT_S, home=None):
    """query() for each node at once: the Results in the order of `nodes`."""
    if not nodes:
        return []
    ssh = ssh or ssh_exe()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
        futs = [pool.submit(query, n, runner, ssh, timeout_s, home) for n in nodes]
        return [f.result() for f in futs]


# -- the run verbs -------------------------------------------------------------------------------

def sh_quote(word):
    """`word` as one sh word (git runs GIT_SSH_COMMAND through sh)."""
    return "'" + word.replace("'", "'\\''") + "'"


def push_url(node, repo):
    """git's scp-style URL of the node's mirror of `repo` (the user and port are in git_env's
    command): `<address>:pmkit-node/mirrors/<repo>.git`, an IPv6 address in brackets."""
    addr = '[%s]' % node.address if ':' in node.address else node.address
    return '%s:%s' % (addr, MIRROR % repo)


class Link(object):
    """One node's pinned ssh for a `node.py run` job: a context that writes the temp known_hosts
    file on entry and removes it on exit. `popen` is spawn.popen (a test's stand-in)."""

    def __init__(self, node, ssh=None, home=None, popen=None):
        self.node, self.home = node, home
        self.ssh = ssh or ssh_exe()
        self.popen = popen or spawn.popen
        self.known_hosts = None

    def __enter__(self):
        if not self.ssh:
            raise OSError('no ssh client: install Windows\' OpenSSH client')
        fd, self.known_hosts = tempfile.mkstemp(prefix='pmkit-known-hosts-', suffix='.txt')
        with os.fdopen(fd, 'w', encoding='ascii', newline='\n') as f:
            f.write(known_hosts_line(self.node))
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.known_hosts)
        except (OSError, TypeError):
            pass
        self.known_hosts = None

    def argv(self, verb, keepalive=False):
        return ssh_argv(self.node, self.ssh, self.known_hosts, home=self.home, command=verb,
                        keepalive=keepalive)

    def git_env(self, base=None):
        """The environment for `git push` to push_url: git's ssh is this ssh with these options."""
        env = dict(os.environ if base is None else base)
        opts = [self.ssh] + ssh_options(self.node, self.known_hosts, home=self.home)
        env['GIT_SSH_COMMAND'] = ' '.join(sh_quote(w) for w in opts)
        env['GIT_SSH_VARIANT'] = 'ssh'
        for k in ('GIT_SSH', 'SSH_ASKPASS', 'GIT_ASKPASS'):
            env.pop(k, None)
        return env

    def _start(self, verb, payload, keepalive):
        p = self.popen(self.argv(verb, keepalive), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
        try:
            p.stdin.write(json.dumps(payload, separators=(',', ':')).encode('utf-8') + b'\n')
            p.stdin.close()
        except OSError:                      # ssh ended already; its exit says why
            pass
        return p

    def call(self, verb, payload, timeout_s=TIMEOUT_S):
        """(rc, stdout bytes, stderr bytes) of one verb whose reply is one JSON line; raises
        subprocess.TimeoutExpired past `timeout_s` (ssh killed)."""
        p = self._start(verb, payload, False)
        out, err = bytearray(), bytearray()
        readers = [threading.Thread(target=_drain, args=(p.stdout, OUT_CAP, out), daemon=True),
                   threading.Thread(target=_drain, args=(p.stderr, OUT_CAP, err), daemon=True)]
        for t in readers:
            t.start()
        try:
            p.wait(timeout_s)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise
        finally:
            for t in readers:
                t.join(5)
            _close(p)
        return p.returncode, bytes(out), bytes(err)

    def stream(self, verb, payload, on_data, idle_s=ALIVE_S * (ALIVE_COUNT + 3),
               clock=time.monotonic):
        """(rc, stderr bytes, idle) of one streamed verb: on_data(bytes) for each piece of its
        stdout as it comes. ssh is killed when nothing at all came for `idle_s` seconds (the node
        sends a keep-alive frame well inside that), and idle is then True. on_data returning
        False ends the call early (ssh killed, idle False)."""
        p = self._start(verb, payload, True)
        err = bytearray()
        et = threading.Thread(target=_drain, args=(p.stderr, OUT_CAP, err), daemon=True)
        et.start()
        last = [clock()]
        idle = [False]
        done = threading.Event()

        def watch():
            while not done.wait(1.0):
                if clock() - last[0] > idle_s:
                    idle[0] = True
                    p.kill()
                    return
        wt = threading.Thread(target=watch, daemon=True)
        wt.start()
        try:
            while True:
                chunk = p.stdout.read1(65536)
                if not chunk:
                    break
                last[0] = clock()
                if on_data(chunk) is False:
                    p.kill()
                    break
        finally:
            done.set()
            p.wait()
            et.join(5)
            _close(p)
        return p.returncode, bytes(err), idle[0]


def _close(p):
    for f in (p.stdout, p.stderr):
        try:
            f.close()
        except (OSError, AttributeError):
            pass
