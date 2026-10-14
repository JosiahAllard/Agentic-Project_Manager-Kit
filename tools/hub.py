"""hub.py: the hub server of this machine (kitlib.hubsite) and the verbs that ask it.

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
      `REFUSED:` or a `machine config:` line of any verb. A console that was closed while the
      server served loses the lines, never the exit code.
      The port is hub.port of the machine config (default 8790); --port 0 takes a free one,
      named on the start line. The store is hub.store, else hub/ in the PM data worktree.
      --store PATH serves that folder instead, as a scratch copy: git sync is off for that run,
      so nothing in it is committed or pushed, here or at the close. An empty PATH is a usage
      error, never the configured store. A store another server holds is refused.
      With hub.access set in the machine config the server also listens on hub.tunnel_port
      (default 8791, loopback too) for a tunnel program, which this tool never runs: every
      request there needs a Cloudflare Access token (kitlib.hubsite.tunnel). A start-up note
      says that it is on, or why it is off (settings that cannot serve, a port that is taken,
      a listener that could not be made); the hub then serves on its own port alone. A request
      it refuses is a note or an error line, at most one a reason a minute, with the reason and
      never the token. A write made through it is recorded with the source `tunnel`.
  hub.py status [--port N]
      asks the running server for its health and prints the revision, the store, the git sync
      (with git's reason when its last pass failed: the owner's token gets it), each
      repository's backlog fetch age and one line for the tunnel listener: `tunnel: on` once
      the Access keys were fetched (the first request with a token fetches them), `tunnel:
      bound, keys not fetched` until then (with the reason as one fixed word in brackets when
      a fetch failed), or `tunnel: off: ` and why.
  hub.py setup [--autostart [--whatif]]
      makes the two token files where they are absent and prints where they are and how to start
      the server. It writes nothing else: never the machine config, so no `hub` key (it says so
      when no store folder is set). --autostart (opt-in) also registers the scheduled task
      kitlib.hubsite.autostart.TASK_NAME, which runs this hub.py's `serve` at sign-in with no
      console window; refused, before any file is written, from a kit worktree or with no
      pythonw.exe beside python. --whatif prints the task's PowerShell and any refusal and
      changes nothing, not even a token file.
  hub.py setup --check
      reads, never writes (no token file, no machine config, no task): it says whether the
      machine is ready for the `hub` key and the tunnel. One line for hub.access: `access: not
      set (hub.access)`, `access: set`, or `access: cannot serve: ` and the fixed phrase of
      kitlib.hubsite.tunnel.check. With settings that can serve, one fetch of the team's Access
      keys (kitlib.hubsite.access.probe, its `access keys: ` line). Then one line for each kit
      copy on this machine (kitlib.config.copies: the plugin export folders, the pins and each
      account folder's install, kitlib.pluginsource.installs): `knows every key`, `would refuse `
      and the keys, or `cannot tell: ` and why (an install whose folder is gone names the
      refresh command), checked against the machine config's keys plus `hub`; a copy that
      refuses a key refuses the whole file. Last, `READY: ...` and exit 0, or `NOT READY: ...`
      and exit 1: a copy in use would refuse a key or cannot be read, the Access settings
      cannot serve, or the keys were not fetched. Export folders not in use decide nothing; a
      NOTE counts those that would refuse. Refresh the plugin and restart the sessions of
      older copies before setting `hub` (docs/hub-remote-access.md "Before you start").
  hub.py open [--port N] [--print]
      opens the hub page in the browser, signed in as the owner through a one-time pairing link
      the running server gives for the owner's token. --print prints the link instead. A paired
      browser stays paired across a restart of the server; its session (auth.SESSION_TTL_S, 30
      days) is renewed when it is used with less than half of that left
      (kitlib.hubsite.auth, "Sessions").
  hub.py unpair [--port N]
      ends the session of every browser paired with the server on the port, and every pairing
      link not yet used; the owner's token only. With no server running there it empties the
      sessions file that server reads at its start (in the token folder) itself and says so.
      A new owner token also unpairs every browser.
  hub.py migrate --from DIR [--from DIR ...] [--store PATH] [--dry-run]
      copies the documents of each DIR (the mirror folder, or an export of one account's copy)
      into the store (hub.store, else hub/ in the PM data worktree; --store PATH: that folder)
      and prints the migration report (kitlib.hubsite.migrate). It only copies in: no source is
      changed, and a document the store already holds is never replaced, so a second run prints
      `nothing changed`. --dry-run writes nothing and reports what a run would do. Refused
      while a running server holds the store. Exit 1 too when a document that has a place in
      the store was not imported (the CONFLICT and SKIPPED lines): a file that is no document,
      two sources that differ, or a source that differs from a store document no server has
      written (the store folder is the mirror folder by default). A document a server has
      written since is kept (KEPT) and does not fail the run.

The verbs below ask the running server through kitlib.hubsite.client. Each takes --port N
(default: hub.port) and --json, which prints the server's answer as one JSON line in place of
the lines named here. A verb marked (owner) carries the owner's token, the others the agent's;
the server alone decides what a role may do.

  hub.py questions [--status S] [--repo R]          `Q-1 v2 open [repo] title` for each
  hub.py questions add --title T [--repo R] [--context-file F] [--option "label::description"
        ...] [--recommended N] [--refs ID ...] [--urgent] [--story S-n] [--from NAME]
                                                     `ASKED Q-2`, then a line for each warning
  hub.py questions log Q-1 <ruling id> --if-version V        `LOGGED Q-1 <ruling id>`
  hub.py answer Q-1 --choice N|other [--note TEXT] [--if-version V]     (owner)
                                                     `ANSWERED Q-1 <label>`
  hub.py todos [--status S] [--repo R]              `T-1 open [repo] title` for each
  hub.py todos add --title T [--detail D] [--command C ...] [--repo R] [--refs ID ...]
        [--urgent] [--from NAME | --owner]           `ADDED T-2`; --owner: the owner's own to-do
  hub.py todos done T-1 [--undo] [--if-version V]   (owner) `DONE T-1`, `REOPENED T-1`
  hub.py todos ack T-1                              `ACKED T-1`
  hub.py stories [--status S] [--repo R]            `S-1 v2 submitted [repo] title` for each
  hub.py stories add --repo R --title T --story-file F [--urgent]       (owner) `SUBMITTED S-2`
  hub.py stories set S-1 --status review|questions|added|declined [--reviewer NAME]
        [--review-note TEXT] [--backlog-id ID] [--closed-note TEXT] --if-version V
                                                     `SET S-1 <status>`
  hub.py stories withdraw S-1                       (owner) `WITHDRAWN S-1`
  hub.py to-pm [--status S] [--repo R]              `P-1 queued [repo] text` for each
  hub.py to-pm add --text T [--kind K] [--repo R] [--urgent]            (owner) `QUEUED P-2`
  hub.py to-pm send                                 (owner) `SENT P-1 P-2`, or `SENT nothing`
  hub.py to-pm reply P-1 TEXT [<ruling id>] [--if-version V]            `REPLIED P-1`
  hub.py inbox                                      `M-1 unread [repo] title` for each
  hub.py inbox add --text T [--title T] [--repo R] [--ref ID] [--asked TEXT] [--refs ID ...]
        [--from NAME]                                `POSTED M-2`
  hub.py inbox read M-1 [--undo] [--if-version V]   (owner) `READ M-1`, `UNREAD M-1`
  hub.py pending                                    `<kind> <id> [repo] title` for each item of
                                                     owner input the PM has not handled
  hub.py wait [--since REV] [--timeout S] [--pending]
      waits until the store changes after revision REV (default: the revision at the call), or,
      with --pending, until an item is pending (one that is pending already ends it at once).
      Prints `HUB <rev> <kind> <id> <title>` for each pending item and `HUB <rev> <collection>
      <id> <op> <status>` for each change, exit 0. With --timeout S: exit 4 and no line when S
      seconds pass with neither. Without it the wait has no end of its own.
  hub.py validate [--store PATH]
      reads the store folder itself (hub.store, else hub/ in the PM data worktree), with no
      server and no lock, so it may run beside one; it writes nothing. Prints each finding as
      `VALIDATE <LEVEL> <document> <field>: <message> [<code>]` and a summary line. Exit 1 when
      a finding is an error.

`--choice N` and `--recommended N` are the option's place in the question's list, counted from
0 as the api counts it. `questions log` and `stories set` need --if-version V: the version of the
document the caller read and decided on, which the `questions` and `stories` lines show as `v2`
and --json as `version`. Without it the verb is a usage error and asks nothing; with a version
the document no longer has, the server refuses the write (`REFUSED conflict`).

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.hubsite, prints and exits.
Exit codes: 0 ok; 1 refused (`REFUSED: ...`, or `REFUSED <code>: <message>` when the server
refused the call), a store with an error finding, a server that did not close cleanly, or a
Ctrl+C other than the one that closes a running server; 2 usage error or a machine config that
cannot be read; 3 the server is not running (the start command is printed); 4 `wait --timeout`
ran out with nothing to say.
"""
import argparse
import json
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import hubvalidate                                     # noqa: E402
from kitlib.config import copies, user                             # noqa: E402
from kitlib.config.types import ConfigError                        # noqa: E402
from kitlib.hubsite import access, api, auth, client, httpd, serve, settings, tunnel   # noqa: E402
from kitlib.hubsite import autostart as hub_autostart              # noqa: E402
from kitlib.hubsite import migrate as hub_migrate                  # noqa: E402

START = 'python "%s" serve' % os.path.abspath(__file__)
OPEN = 'python "%s" open' % os.path.abspath(__file__)
HEALTH = api.BASE + '/health'
# every verb, in the order the usage error names them
VERB_NAMES = ('serve', 'status', 'setup', 'open', 'unpair', 'migrate', 'questions', 'answer',
              'todos', 'stories', 'to-pm', 'inbox', 'pending', 'wait', 'validate')
NO_VERB = 'give a verb: %s or %s' % (', '.join(VERB_NAMES[:-1]), VERB_NAMES[-1])


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


def _source(text):
    """migrate's --from value: a folder, never an empty value."""
    if not text.strip():
        raise argparse.ArgumentTypeError('--from needs a folder, not an empty value')
    return text


def _count(text):
    try:
        n = int(text)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError('a whole number from 0, not %r' % text)
    return n


def _seconds(text):
    try:
        n = float(text)
    except ValueError:
        n = -1.0
    if not 0 <= n < float('inf'):
        raise argparse.ArgumentTypeError('a number of seconds from 0, not %r' % text)
    return n


def _choice(text):
    return text if text == 'other' else _count(text)


def _option(text):
    """questions add's --option value: "label::description"."""
    label, sep, description = text.partition('::')
    if not sep or not label.strip() or not description.strip():
        raise argparse.ArgumentTypeError('an option is "label::description", not %r' % text)
    return {'label': label.strip(), 'description': description.strip()}


def _asks(p):
    """The two arguments of every parser whose verb asks the server. No default is set: a
    sub-verb's parser would put its own over what the verb before it was given."""
    p.add_argument('--port', type=_port(1), default=argparse.SUPPRESS,
                   help='the port (default: hub.port)')
    p.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                   help="print the server's answer as one JSON line")
    return p


def _pins(p, required=False):
    p.add_argument('--if-version', dest='if_version', type=_count, default=None, metavar='V',
                   required=required, help='refuse unless the document has this version')


def _repo(p):
    """--repo of an `add` whose verb lists by --repo too. No default is set, as in _asks: the
    sub-verb's own would put None over a --repo given before `add`. Read it with getattr."""
    p.add_argument('--repo', default=argparse.SUPPRESS)


def _listed(sub, name, what, statuses=None, repo=True):
    """The parser of a verb that lists its collection when no sub-verb follows; its
    sub-parsers."""
    p = _asks(sub.add_parser(name, help=what))
    if statuses:
        p.add_argument('--status', choices=statuses + ('all',), default=argparse.SUPPRESS)
    if repo:
        p.add_argument('--repo', default=argparse.SUPPRESS)
    return p.add_subparsers(dest='sub')


def _leaf(subs, name, what):
    return _asks(subs.add_parser(name, help=what))


def _client_verbs(sub):
    S = hubvalidate.STATUSES
    q = _listed(sub, 'questions', 'list the questions; add: ask one; log: record its ruling',
                S['questions'])
    x = _leaf(q, 'add', 'ask the owner a question')
    x.add_argument('--title', required=True)
    _repo(x)
    x.add_argument('--context-file', dest='context_file', metavar='F',
                   help='a UTF-8 file with the context of the question')
    x.add_argument('--option', dest='options', action='append', type=_option, default=[],
                   metavar='"label::description"', help='an answer to choose (repeatable)')
    x.add_argument('--recommended', type=_count, metavar='N',
                   help='the place of the recommended option (counts from 0)')
    x.add_argument('--refs', nargs='+', metavar='ID')
    x.add_argument('--urgent', action='store_true')
    x.add_argument('--story', metavar='S-n')
    x.add_argument('--from', dest='who', metavar='NAME')
    x = _leaf(q, 'log', 'record the ruling an answered question was logged as')
    x.add_argument('id')
    x.add_argument('ruling', metavar='ruling-id')
    _pins(x, required=True)

    x = _asks(sub.add_parser('answer', help='answer a question (the owner)'))
    x.add_argument('id')
    x.add_argument('--choice', required=True, type=_choice, metavar='N|other',
                   help='the place of the chosen option (counts from 0), or other (which needs '
                        '--note)')
    x.add_argument('--note')
    _pins(x)

    t = _listed(sub, 'todos', 'list the to-dos; add, done, ack', S['todos'])
    x = _leaf(t, 'add', 'add a to-do')
    x.add_argument('--title', required=True)
    x.add_argument('--detail')
    x.add_argument('--command', dest='commands', action='append', metavar='C',
                   help='a command the to-do names (repeatable)')
    _repo(x)
    x.add_argument('--refs', nargs='+', metavar='ID')
    x.add_argument('--urgent', action='store_true')
    who = x.add_mutually_exclusive_group()
    who.add_argument('--from', dest='who', metavar='NAME')
    who.add_argument('--owner', action='store_true', help="the owner's own to-do")
    x = _leaf(t, 'done', 'mark a to-do done (the owner)')
    x.add_argument('id')
    x.add_argument('--undo', action='store_true')
    _pins(x)
    _leaf(t, 'ack', 'say that the PM has seen a to-do').add_argument('id')

    s = _listed(sub, 'stories', 'list the stories; add, set, withdraw', S['stories'])
    x = _leaf(s, 'add', 'submit a story (the owner)')
    x.add_argument('--repo', required=True)
    x.add_argument('--title', required=True)
    x.add_argument('--story-file', dest='story_file', required=True, metavar='F',
                   help='a UTF-8 file with the story')
    x.add_argument('--urgent', action='store_true')
    x = _leaf(s, 'set', "set a story's status")
    x.add_argument('id')
    x.add_argument('--status', required=True, choices=api.AGENT_STORY_STATUSES)
    x.add_argument('--reviewer')
    x.add_argument('--review-note', dest='review_note')
    x.add_argument('--backlog-id', dest='backlog_id')
    x.add_argument('--closed-note', dest='closed_note')
    _pins(x, required=True)
    _leaf(s, 'withdraw', 'withdraw a story (the owner)').add_argument('id')

    m = _listed(sub, 'to-pm', 'list the messages to the PM; add, send, reply', S['to_pm'])
    x = _leaf(m, 'add', 'queue a message to the PM (the owner)')
    x.add_argument('--text', required=True)
    x.add_argument('--kind')
    _repo(x)
    x.add_argument('--urgent', action='store_true')
    _leaf(m, 'send', 'send every queued message (the owner)')
    x = _leaf(m, 'reply', 'answer a message the owner sent')
    x.add_argument('id')
    x.add_argument('reply', metavar='TEXT')
    x.add_argument('ruling', nargs='?', metavar='ruling-id')
    _pins(x)

    i = _listed(sub, 'inbox', 'list the inbox; add, read', repo=False)
    x = _leaf(i, 'add', 'post a message for the owner')
    x.add_argument('--text', required=True)
    x.add_argument('--title')
    x.add_argument('--repo')
    x.add_argument('--ref', metavar='ID')
    x.add_argument('--asked')
    x.add_argument('--refs', nargs='+', metavar='ID')
    x.add_argument('--from', dest='who', metavar='NAME')
    x = _leaf(i, 'read', 'mark an inbox message read (the owner)')
    x.add_argument('id')
    x.add_argument('--undo', action='store_true')
    _pins(x)

    _asks(sub.add_parser('pending', help='the owner input the PM has not handled'))
    w = _asks(sub.add_parser('wait', help='wait for a change of the store or a pending item'))
    w.add_argument('--since', type=_count, metavar='REV',
                   help='changes after this revision (default: the revision at the call)')
    w.add_argument('--timeout', type=_seconds, metavar='S',
                   help='exit 4 after this many seconds with nothing (default: no limit)')
    w.add_argument('--pending', action='store_true', help='pending items end the wait too')
    v = sub.add_parser('validate', help='check the store folder; reads it, no server needed')
    v.add_argument('--store', type=_folder, help='the store folder (default: the configured one)')


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
    u = sub.add_parser('setup', help='make the token files where they are absent; the machine '
                                     'config is not written')
    u.add_argument('--autostart', action='store_true',
                   help='also register the scheduled task that starts the server at sign-in')
    u.add_argument('--whatif', action='store_true',
                   help='with --autostart: print the task, change nothing (no token file either)')
    u.add_argument('--check', action='store_true',
                   help='write nothing: say whether the Access settings serve and which kit '
                        'copies would refuse the hub key')
    o = sub.add_parser('open', help='open the hub page, signed in as the owner')
    o.add_argument('--port', type=_port(1), help='the port (default: hub.port)')
    o.add_argument('--print', dest='show', action='store_true',
                   help='print the one-time link instead of opening it')
    n = sub.add_parser('unpair', help='end the session of every paired browser (the owner)')
    n.add_argument('--port', type=_port(1), help='the port (default: hub.port)')
    m = sub.add_parser('migrate', help="copy the earlier hub's documents into the store")
    m.add_argument('--from', dest='sources', action='append', type=_source, required=True,
                   metavar='DIR', help='a folder to copy from: the mirror folder or an export '
                                       '(repeatable); it is only read')
    m.add_argument('--store', type=_folder, help='the store folder (default: the configured one)')
    m.add_argument('--dry-run', dest='dry_run', action='store_true',
                   help='write nothing; report what a run would do')
    _client_verbs(sub)
    return p


LEVELS = {'error': 'ERROR', 'note': 'NOTE'}     # the log levels that are printed, and as what
CLOSED = 'hub closed: the server ended cleanly and the store lock is released'
# Ctrl+C left the close itself (kitlib.hubsite.serve, "The clean close": a second one with no
# git sync to wait for, or one in the close's last two steps). The lock: step 5 released it, or
# the system does when the process ends.
CLOSE_CUT = ('interrupted during the close: the hub server did not close cleanly. The store '
             'lock is released, at the latest when this process ends')
UNCLEAN = 'the hub server did not close cleanly: see the lines above'
INTERRUPTED = 'interrupted'             # Ctrl+C anywhere else (a verb, or serve before it served)


def _say(text, stream=None):
    """One line of `serve` to the console (default stdout). A console that was closed while
    the server served must not change how the server ends: the line is lost, nothing else."""
    try:
        print(text, file=stream or sys.stdout, flush=True)
    except (OSError, ValueError):
        pass


def _log(level, text):
    """The server's error and note lines, to stderr; a request line is not printed. Every line
    goes through serve.scrub: a listener's error line may quote a remote URL as git gave it."""
    word = LEVELS.get(level)
    if word is None:
        return
    _say('%s: %s' % (word, serve.scrub(text)), sys.stderr)   # closed console: a handler goes on


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
        _say('hub serving %s on %s (Ctrl+C stops it); to pair a browser run: %s'
             % (h.store.root, h.url, OPEN))

    try:
        clean = serve.run(hub, started, stop)
    except KeyboardInterrupt:           # run() lets one through only from the close
        _say(CLOSE_CUT, sys.stderr)
        return 1
    if not clean:
        _say(UNCLEAN, sys.stderr)
        return 1
    _say(CLOSED)                        # the owner sees that the server ended on purpose
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


def _check():
    """setup --check: reads only. 0 when ready, 1 when not."""
    from kitlib import pluginsource
    s = settings.load()
    why = []
    try:
        tunnel.check(s.access)
    except tunnel.TunnelConfigError as e:
        if e.unset:
            print('access: not set (hub.access): the tunnel listener stays off')
        else:
            print('access: cannot serve: %s' % e)
            why.append('the Access settings cannot serve')
    else:
        print('access: set')
        ok, line = access.probe(s.access.team)
        print(line)
        if not ok:
            why.append('the Access keys were not fetched')
    keys = copies.config_keys(user.readable_file(), (settings.KEY,))
    try:
        root = pluginsource.load().export_dir
    except pluginsource.RefreshError as e:
        print('kit copies: cannot look: %s' % e)
        why.append('the kit copies could not be looked at')
    else:
        report = copies.check(copies.find(root, pluginsource.current_folder(root),
                                          pluginsource.installs()), keys,
                              pluginsource.refresh_cmd())
        print('kit copies, checked against the keys %s:' % ', '.join(keys))
        for line in report.lines:
            print('  %s' % line)
        if not report.ready:
            why.append('a kit copy in use would refuse a key or cannot be read')
        if report.stale:
            print('NOTE: %d export folder(s) not in use would refuse a key or cannot be read: '
                  'a session started from one before the last refresh still runs it; restart '
                  'such sessions before setting %s' % (report.stale, settings.KEY))
    if why:
        print('NOT READY: %s' % '; '.join(why))
        return client.REFUSED
    print('READY: every kit copy in use knows these keys, and the Access settings serve or are '
          'not set')
    return 0


def _setup(a, stop):
    if a.check:
        return _check()
    if (a.autostart or a.whatif) and not settings.load().store:
        raise serve.ServeError('no store folder is set, so a server started at sign-in would '
                               'fail: set hub.store or pm_data in the machine config; '
                               'nothing registered or written')
    if a.whatif:
        for line in hub_autostart.install(os.path.abspath(__file__), whatif=True):
            print(line)
        return 0
    why = hub_autostart.refusals(os.path.abspath(__file__)) if a.autostart else []
    if why:     # before any token file is written
        raise hub_autostart.AutostartError('; '.join(why) + '; nothing registered or written')
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
    if a.autostart:
        for line in hub_autostart.install(os.path.abspath(__file__)):
            print(line)
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


def _unpair(a, stop):
    port = a.port or settings.load().port
    folder = auth.folder()
    token = auth.read_token(folder, auth.OWNER)
    if not token:
        raise serve.ServeError('no owner token in %s: run hub.py setup' % folder)
    try:
        status, obj = serve.ask(port, 'POST', httpd.UNPAIR_ROUTE, token)
    except serve.NotRunning as e:
        # no server holds the sessions in memory: the file the next one reads is emptied here
        failed = []
        creds = auth.Credentials(folder, port=port, persist=True,
                                 log=lambda level, text: failed.append(text))
        creds.unpair_all()
        if failed:
            raise serve.ServeError(failed[0])
        print('%s: unpaired every browser in %s, the sessions file the server on that port '
              'reads at its start' % (e, creds.path))
        # a server may have bound the port and read the file between the first ask and the write
        try:
            status, obj = serve.ask(port, 'POST', httpd.UNPAIR_ROUTE, token)
        except serve.NotRunning:
            return 0
    if status != 200 or obj.get('ok') is not True:
        return _refused(port, status, obj)
    print('unpaired every browser of the hub server on port %d' % port)
    return 0


def _store_root(a):
    root = os.path.abspath(a.store) if a.store is not None else settings.load().store
    if not root:
        raise serve.ServeError('no store folder is set: set hub.store or pm_data in the machine '
                               'config, or give --store PATH')
    return root


def _migrate(a, stop):
    try:
        result = hub_migrate.migrate(_store_root(a), a.sources, dry_run=a.dry_run)
    except hub_migrate.MigrateError as e:
        raise serve.ServeError(str(e))
    for line in hub_migrate.report_lines(result):
        print(serve.scrub(line))
    return 0 if result.complete else 1


def _validate(a, stop):
    try:
        findings = hub_migrate.validate(_store_root(a))
    except hub_migrate.MigrateError as e:
        raise serve.ServeError(str(e))
    for line in hub_migrate.validate_lines(findings):
        _put(line)
    return 1 if any(f.level == hubvalidate.ERROR for f in findings) else 0


# ------------------------------------------------------------------ the verbs that ask the server

def _put(text):
    """One line to stdout: `text` on one line, with no user or password of a URL, and with a
    character the console cannot show written as its escape."""
    line = serve.scrub(' '.join(str(text).split()))
    try:
        print(line)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
        print(line.encode(enc, 'backslashreplace').decode(enc))


def _show(a, obj, lines):
    """The answer `obj` of the server: as one JSON line with --json, else `lines`."""
    if getattr(a, 'json', False):
        print(serve.scrub(json.dumps(obj, sort_keys=True)))     # ASCII; a `"` ends a URL's user
    else:
        for line in lines:
            _put(line)
    return client.OK


def _hub(a):
    return client.Client(getattr(a, 'port', None) or settings.load().port)


def _item(d, state=None, version=False):
    title = d.get('title') or d.get('text') or ''
    name = '%s v%s' % (d.get('id'), d.get('version')) if version else d.get('id')
    return '%s %s [%s] %s' % (name, state or d.get('status') or '-', d.get('repo') or '-', title)


def _list(a, collection, version=False):
    """The lines of a collection. `version`: each line shows its document's version, for the
    collections that have a write which needs it named (--if-version)."""
    got = _hub(a).get('/' + collection, query={'status': getattr(a, 'status', None),
                                              'repo': getattr(a, 'repo', None)})
    return _show(a, got, [_item(d, version=version) for d in got.get('items') or []])


def _wrote(a, got, line):
    """The answer of a write: its line, then one for each warning the store gave."""
    lines = [line]
    for w in got.get('warnings') or []:
        if isinstance(w, dict):
            lines.append(hubvalidate.format_line(hubvalidate.Finding(
                *[str(w.get(k) or '') for k in hubvalidate.Finding._fields])))
    return _show(a, got, lines)


def _text_of(path, what):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except (OSError, ValueError) as e:
        raise serve.ServeError('cannot read the %s file %s: %s' % (what, path, type(e).__name__))


def _body(**fields):
    """The fields that were given: None, an empty list and a flag that is off are left out."""
    return {k: v for k, v in fields.items()            # 0 is a value: a place, a version
            if not (v is None or v is False or (isinstance(v, list) and not v))}


def _one(collection, a):
    return '/%s/%s' % (collection, client.part(a.id))


def _questions(a, stop):
    hub = _hub(a)
    if a.sub == 'add':
        context = _text_of(a.context_file, 'context') if a.context_file else None
        got = hub.post('/questions', client.AGENT, _body(
            title=a.title, repo=getattr(a, 'repo', None), context=context, options=a.options,
            recommended=a.recommended, refs=a.refs, urgent=a.urgent, story=a.story,
            **{'from': a.who}))
        return _wrote(a, got, 'ASKED %s' % got.get('id'))
    if a.sub == 'log':
        got = hub.post(_one('questions', a) + '/log', client.AGENT,
                       {'ruling': a.ruling, 'if_version': a.if_version})
        return _wrote(a, got, 'LOGGED %s %s' % (got.get('id'), a.ruling))
    return _list(a, 'questions', version=True)


def _answer(a, stop):
    got = _hub(a).post(_one('questions', a) + '/answer', client.OWNER, _body(
        choice=a.choice, note=a.note, if_version=a.if_version))
    answer = got.get('answer') if isinstance(got.get('answer'), dict) else {}
    return _wrote(a, got, 'ANSWERED %s %s' % (got.get('id'), answer.get('label') or a.choice))


def _todos(a, stop):
    hub = _hub(a)
    if a.sub == 'add':
        body = _body(title=a.title, detail=a.detail, commands=a.commands,
                     repo=getattr(a, 'repo', None), refs=a.refs, urgent=a.urgent)
        if a.who:
            body['from'] = a.who
        got = hub.post('/todos', client.OWNER if a.owner else client.AGENT, body)
        return _wrote(a, got, 'ADDED %s' % got.get('id'))
    if a.sub == 'done':
        body = _body(if_version=a.if_version)
        body['done'] = not a.undo
        got = hub.post(_one('todos', a) + '/done', client.OWNER, body)
        return _wrote(a, got, '%s %s' % ('REOPENED' if a.undo else 'DONE', got.get('id')))
    if a.sub == 'ack':
        got = hub.post(_one('todos', a) + '/ack', client.AGENT)
        return _wrote(a, got, 'ACKED %s' % got.get('id'))
    return _list(a, 'todos')


def _stories(a, stop):
    hub = _hub(a)
    if a.sub == 'add':
        got = hub.post('/stories', client.OWNER, _body(
            repo=a.repo, title=a.title, story=_text_of(a.story_file, 'story'), urgent=a.urgent))
        return _wrote(a, got, 'SUBMITTED %s' % got.get('id'))
    if a.sub == 'set':
        got = hub.post(_one('stories', a), client.AGENT, _body(
            status=a.status, reviewer=a.reviewer, review_note=a.review_note,
            backlog_id=a.backlog_id, closed_note=a.closed_note,
            if_version=a.if_version))
        return _wrote(a, got, 'SET %s %s' % (got.get('id'), a.status))
    if a.sub == 'withdraw':
        got = hub.post(_one('stories', a) + '/withdraw', client.OWNER)
        return _wrote(a, got, 'WITHDRAWN %s' % got.get('id'))
    return _list(a, 'stories', version=True)


def _to_pm(a, stop):
    hub = _hub(a)
    if a.sub == 'add':
        got = hub.post('/to_pm', client.OWNER, _body(
            text=a.text, kind=a.kind, repo=getattr(a, 'repo', None), urgent=a.urgent))
        return _wrote(a, got, 'QUEUED %s' % got.get('id'))
    if a.sub == 'send':
        got = hub.post('/to_pm/send', client.OWNER)
        sent = [str(x) for x in got.get('sent') or []]
        return _show(a, got, ['SENT %s' % (' '.join(sent) or 'nothing')])
    if a.sub == 'reply':
        got = hub.post(_one('to_pm', a) + '/reply', client.AGENT, _body(
            reply=a.reply, ruling=a.ruling, if_version=a.if_version))
        return _wrote(a, got, 'REPLIED %s' % got.get('id'))
    return _list(a, 'to_pm')


def _inbox(a, stop):
    hub = _hub(a)
    if a.sub == 'add':
        got = hub.post('/inbox', client.AGENT, _body(
            text=a.text, title=a.title, repo=a.repo, ref=a.ref, asked=a.asked, refs=a.refs,
            **{'from': a.who}))
        return _wrote(a, got, 'POSTED %s' % got.get('id'))
    if a.sub == 'read':
        body = _body(if_version=a.if_version)
        body['read'] = not a.undo
        got = hub.post(_one('inbox', a) + '/read', client.OWNER, body)
        return _wrote(a, got, '%s %s' % ('UNREAD' if a.undo else 'READ', got.get('id')))
    items = hub.inbox()
    return _show(a, {'items': items},
                 [_item(d, 'read' if d.get('read') else 'unread') for d in items])


def _pending(a, stop):
    got = _hub(a).get('/pending')
    return _show(a, got, ['%s %s [%s] %s' % (p.get('kind'), p.get('id'), p.get('repo') or '-',
                                              p.get('title') or '')
                          for p in got.get('items') or []])


def _wait(a, stop):
    got = _hub(a).wait(a.since, a.timeout, a.pending)
    if got.get('timed_out'):
        if getattr(a, 'json', False):
            _show(a, got, [])
        return client.TIMED_OUT
    rev = got.get('revision')
    lines = ['HUB %s %s %s %s' % (rev, p.get('kind'), p.get('id'), p.get('title') or '')
             for p in got.get('pending') or []]
    lines += ['HUB %s %s %s %s %s' % (rev, c.get('collection'), c.get('id'), c.get('op'),
                                      c.get('status') or '-')
              for c in got.get('changes') or []]
    if got.get('reset'):
        lines.append('HUB %s reset: the store is behind --since; read it again' % rev)
    return _show(a, got, lines)


VERBS = {'serve': _serve, 'status': _status, 'setup': _setup, 'open': _open,
         'unpair': _unpair, 'migrate': _migrate, 'questions': _questions, 'answer': _answer, 'todos': _todos,
         'stories': _stories, 'to-pm': _to_pm, 'inbox': _inbox, 'pending': _pending,
         'wait': _wait, 'validate': _validate}


def main(argv=None, stop=None):
    """`stop`: a threading.Event that ends `serve` when set (default: Ctrl+C only). The
    `machine config:` and `REFUSED` lines go through serve.scrub, as the server's log lines
    do: a reason may quote a remote URL."""
    p = parser()
    a = p.parse_args(argv)
    if not a.verb:
        p.error(NO_VERB)
    if a.verb == 'setup' and a.check and (a.autostart or a.whatif):
        p.error('--check takes no other option: it writes nothing')
    if a.verb == 'setup' and a.whatif and not a.autostart:
        p.error('--whatif needs --autostart')
    try:
        return VERBS[a.verb](a, stop)
    except ConfigError as e:
        print(serve.scrub('machine config: %s' % e), file=sys.stderr)
        return client.USAGE
    except serve.NotRunning as e:
        print('%s; start it with: %s' % (e, START), file=sys.stderr)
        return client.exit_code(e)
    except client.Refused as e:         # the server's own refusal: its code, then its words
        if getattr(a, 'json', False):
            print(serve.scrub(json.dumps(e.body, sort_keys=True)))
        print(serve.scrub('REFUSED %s: %s' % (e.code, ' '.join(e.message.split()))),
              file=sys.stderr)
        return client.exit_code(e)
    except (serve.ServeError, auth.TokenFileError) as e:
        print(serve.scrub('REFUSED: %s' % e), file=sys.stderr)
        return client.REFUSED
    except KeyboardInterrupt:           # not in a server's close: _serve answers that one
        print(INTERRUPTED, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
