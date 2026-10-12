"""hub.py: the hub server of this machine (kitlib.hubsite).

  hub.py serve [--port N] [--store PATH]
      serves the hub on the loopback address until Ctrl+C, which closes it cleanly: the last
      changes are committed and the store lock is released. Prints one start line and, after a
      clean close, one closing line; notes (NOTE: ...) and errors (ERROR: ...) go to stderr: the
      start-up notes, a git sync pass that failed with git's reason, the wait for git at the
      close before it begins (it is given no time limit) and each step of the close that failed
      or ran out of time. Ctrl+C again ends that wait: an error line then says that a git
      command may have been left half-way and what to check, and the exit code is 1. Where
      the close has no git to wait for (--store, a sync refused at start-up, or its last two
      steps: the store and the handler threads), Ctrl+C again ends the close itself: one line
      says that the close was interrupted and that the store lock is released, and the exit
      code is 1. No line of these holds the user or password of a URL, and neither does a
      `REFUSED:` or a `machine config:` line of any verb.
      The port is hub.port of the machine config (default 8790); --port 0 takes a free one,
      named on the start line. The store is hub.store, else hub/ in the PM data worktree.
      --store PATH serves that folder instead, as a scratch copy: git sync is off for that run,
      so nothing in it is committed or pushed, here or at the close. An empty PATH is a usage
      error, never the configured store. A store another server holds is refused.
  hub.py status [--port N]
      asks the running server for its health and prints the revision, the store, the git sync
      (with git's reason when its last pass failed: the owner's token gets it) and each
      repository's backlog fetch age.
  hub.py setup
      makes the two token files where they are absent and prints where they are and how to start
      the server. It writes nothing else: never the machine config, so no `hub` key (it says so
      when no store folder is set).
  hub.py open [--port N] [--print]
      opens the hub page in the browser, signed in as the owner through a one-time pairing link
      the running server gives for the owner's token. --print prints the link instead.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.hubsite, prints and exits.
Exit codes: 0 ok; 1 refused (REFUSED: ...), a server that did not close cleanly, or a Ctrl+C
other than the one that closes a running server; 2 usage error or a machine config that cannot
be read; 3 the server is not running (the start command is printed).
"""
import argparse
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.config.types import ConfigError                        # noqa: E402
from kitlib.hubsite import api, auth, httpd, serve, settings       # noqa: E402

START = 'python "%s" serve' % os.path.abspath(__file__)
HEALTH = api.BASE + '/health'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def _port(low):
    def port(text):
        try:
            n = int(text)
        except ValueError:
            n = -1
        if not low <= n <= 65535:
            raise argparse.ArgumentTypeError('a port is a number from %d to 65535' % low)
        return n
    return port


def _folder(text):
    """--store's value. An empty one is refused here: it must never mean "no --store", which
    serves the configured store with git sync on."""
    if not text.strip():
        raise argparse.ArgumentTypeError('--store needs a folder, not an empty value')
    return text


def parser():
    p = Parser(prog='hub.py', description='The hub server of this machine.')
    sub = p.add_subparsers(dest='verb')
    s = sub.add_parser('serve', help='serve the hub until Ctrl+C')
    s.add_argument('--port', type=_port(0), help='the port (default: hub.port; 0: a free one)')
    s.add_argument('--store', type=_folder,
                   help='serve this folder as a scratch copy: git sync is off for the run, so '
                        'nothing in it is committed or pushed')
    t = sub.add_parser('status', help="the running server's health")
    t.add_argument('--port', type=_port(1), help='the port (default: hub.port)')
    sub.add_parser('setup', help='make the token files where they are absent; the machine '
                                 'config is not written')
    o = sub.add_parser('open', help='open the hub page, signed in as the owner')
    o.add_argument('--port', type=_port(1), help='the port (default: hub.port)')
    o.add_argument('--print', dest='show', action='store_true',
                   help='print the one-time link instead of opening it')
    return p


LEVELS = {'error': 'ERROR', 'note': 'NOTE'}     # the log levels that are printed, and as what
CLOSED = 'hub closed: the server ended cleanly and the store lock is released'
# Ctrl+C left the close itself (kitlib.hubsite.serve, "The clean close": a second one with no
# git sync to wait for, or one in the close's last two steps). The lock: step 5 released it, or
# the system does when the process ends.
CLOSE_CUT = ('interrupted during the close: the hub server did not close cleanly. The store '
             'lock is released, at the latest when this process ends')
INTERRUPTED = 'interrupted'             # Ctrl+C anywhere else (a verb, or serve before it served)


def _log(level, text):
    """The server's error and note lines, to stderr; a request line is not printed. Every line
    goes through serve.scrub: a listener's error line may quote a remote URL as git gave it."""
    word = LEVELS.get(level)
    if word is None:
        return
    try:
        print('%s: %s' % (word, serve.scrub(text)), file=sys.stderr, flush=True)
    except (OSError, ValueError):       # a closed console must not stop a handler
        pass


def _serve(a, stop):
    s = settings.load()
    if a.port is not None:
        s = s._replace(port=a.port)
    scratch = a.store is not None       # never the truth of the text: '' is refused by the parser
    if scratch:
        s = s._replace(store=os.path.abspath(a.store), push=False)
    hub = serve.build(s, serve.machine_sources(sync=not scratch), log=_log)

    def started(h):
        for note in h.notes:
            _log('note', note)
        print('hub serving %s on %s (Ctrl+C stops it)' % (h.store.root, h.url), flush=True)

    try:
        clean = serve.run(hub, started, stop)
    except KeyboardInterrupt:           # run() lets one through only from the close
        print(CLOSE_CUT, file=sys.stderr)
        return 1
    if not clean:
        print('the hub server did not close cleanly: see the lines above', file=sys.stderr)
        return 1
    try:
        print(CLOSED, flush=True)       # the owner sees that the server ended on purpose
    except (OSError, ValueError):       # a closed console: the server still ended cleanly
        pass
    return 0


def _refused(port, status, obj):
    err = obj.get('error') if isinstance(obj.get('error'), dict) else {}
    print(serve.scrub('REFUSED: the server on port %d answered %d %s: %s' % (
        port, status, err.get('code') or '', err.get('message') or 'not the expected answer')),
        file=sys.stderr)
    return 1


def _status(a, stop):
    port = a.port or settings.load().port
    folder = auth.folder()
    token = auth.read_token(folder, auth.OWNER) or auth.read_token(folder, auth.AGENT)
    status, obj = serve.ask(port, 'GET', HEALTH, token)
    if status != 200 or obj.get('ok') is not True:
        return _refused(port, status, obj)
    print('hub server on http://%s:%d' % (httpd.LOOPBACK, port))
    for line in serve.status_lines(obj):
        print(line)
    return 0


def _setup(a, stop):
    s = settings.load()
    folder = auth.folder()
    try:
        paths = auth.ensure_tokens(folder)
    except OSError as e:
        raise serve.ServeError('cannot write the token files in %s: %s' % (folder, e))
    for role in (auth.OWNER, auth.AGENT):
        print('%s token: %s' % (role, paths[role]))
    if s.store:
        print('store: %s' % s.store)
    else:
        print('NOTE: no store folder is set: set hub.store or pm_data in the machine config '
              '(this tool does not write it), or start with --store PATH')
    print('start the server with: %s' % START)
    return 0


def _open(a, stop):
    port = a.port or settings.load().port
    folder = auth.folder()
    token = auth.read_token(folder, auth.OWNER)
    if not token:
        raise serve.ServeError('no owner token in %s: run hub.py setup' % folder)
    status, obj = serve.ask(port, 'POST', httpd.PAIR_ROUTE, token)
    path = obj.get('path')
    if status != 200 or not isinstance(path, str) or not path.startswith('/'):
        return _refused(port, status, obj)
    url = 'http://%s:%d%s' % (httpd.LOOPBACK, port, path)
    if a.show:
        print(url)
        return 0
    if not webbrowser.open(url):
        raise serve.ServeError('no browser could be opened: use hub.py open --print')
    print('opened the hub page in the browser')
    return 0


VERBS = {'serve': _serve, 'status': _status, 'setup': _setup, 'open': _open}


def main(argv=None, stop=None):
    """`stop`: a threading.Event that ends `serve` when set (default: Ctrl+C only). The
    `machine config:` and `REFUSED:` lines go through serve.scrub, as the server's log lines
    do: a reason may quote a remote URL."""
    p = parser()
    a = p.parse_args(argv)
    if not a.verb:
        p.error('give a verb: serve, status, setup or open')
    try:
        return VERBS[a.verb](a, stop)
    except ConfigError as e:
        print(serve.scrub('machine config: %s' % e), file=sys.stderr)
        return 2
    except serve.NotRunning as e:
        print('%s; start it with: %s' % (e, START), file=sys.stderr)
        return 3
    except (serve.ServeError, auth.TokenFileError) as e:
        print(serve.scrub('REFUSED: %s' % e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:           # not in a server's close: _serve answers that one
        print(INTERRUPTED, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
