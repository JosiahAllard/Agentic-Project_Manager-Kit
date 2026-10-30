"""hub.py: the hub server of this machine (kitlib.hubsite) and the verbs that ask it.

  hub.py serve [--port N] [--store PATH]
      serves the hub on the loopback address until Ctrl+C or hub.py stop, which close it
      cleanly: the last changes are committed and the store lock is released. Prints one start
      line and, after a
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
      so nothing in it is committed or pushed, here or at the close, and no push notification
      is sent. An empty PATH is a usage error, never the configured store. A store another
      server holds is refused.
      Push notifications (docs/action-hub.md "Push notifications") run when the host adapter
      has push() and launcher.state_dir is set; otherwise a start-up note says why they are off.
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
  hub.py stop [--port N] [--timeout S]
      asks the server on the port to close as Ctrl+C closes it (kitlib.hubsite.control), then
      waits until nothing listens on the port and its process has ended: `STOPPED: ...`. The
      owner's token only; a browser session never stops the server. With nothing answering
      there: `nothing to stop: ...`, exit 0; a server stopped a moment ago may still be closing
      then, as it frees its port before its last git pass and the store's close. --timeout S
      (default 300, approximate: one request in flight may run past it): exit 4 with
      `TIMED OUT: ...` when it has not closed by then; nothing is ever killed, the server goes
      on closing.
  hub.py restart [--port N] [--timeout S]
      stops the server on the port (default hub.port) as stop does, then starts the scheduled
      task kitlib.hubsite.autostart.TASK_NAME again and waits until the new server answers on
      hub.port: `RESTARTED: ...`. It reads the task's state first and starts the task only when
      the task ran the server it stopped, or nothing ran and the task is ready: no task, a
      disabled one or a server started by hand starts nothing, says why and prints the start
      command, exit 3. A task whose state cannot be read or that cannot be started after the
      stop: the STOPPED line, why, and the start command, exit 1. --timeout S covers the whole
      restart: exit 4 as for stop (the console advice only when the new server never answered).

The verbs below ask the running server through kitlib.hubsite.client. Each takes --port N
(default: hub.port) and --json, which prints the server's answer as one JSON line in place of
the lines named here. A verb marked (owner) carries the owner's token, the others the agent's;
the server alone decides what a role may do.

  hub.py questions [--status S] [--repo R]          `Q-1 v2 open [repo] title` for each; a
                                                     done one ends ` (done by the hub: WHY)`
                                                     or ` (done: WHY)`
  hub.py questions show Q-1                         one question: title, status, version, options
                                                     (the recommended marked), context, answer
                                                     and note; any status
  hub.py questions add --title T [--repo R] [--context-file F] [--option "label::description"
        ...] [--recommended N] [--refs ID ...] [--urgent] [--story S-n]
        (--record REPO:ID | --class rule|machine|cross-repo|go) [--from KEY]
                                                     `ASKED Q-2`, then a line for each warning;
                                                     --from: the asker's launcher session key
                                                     (default pm), refused when the hub does
                                                     not know it; --record: the open backlog
                                                     record it is about, refused unless the
                                                     hub reads it now in that configured repo;
                                                     else --class: what kind of question it is
  hub.py questions log Q-1 [--unfiled WHY] --if-version V
                                                     `LOGGED Q-1`: an answered question the
                                                     PM logged in the ledger (no id is given)
  hub.py questions done Q-1 --why TEXT [--unfiled WHY] --if-version V
                                                     `DONE Q-1`: an answered question that
                                                     needs no ruling. A bound answer not filed
                                                     is refused until it files; --unfiled says
                                                     why one that never can is handled anyway
  hub.py questions file [Q-1]                       `FILED Q-1`, `NOT FILED Q-1: <why> (<code>)`
                                                     or `nothing to file`: each answer to a
                                                     question bound to a record not filed yet,
                                                     filed into its record again
  hub.py questions bind --dry-run | --apply          (PM) `BIND Q-1 repo:ID` per open question a
                                                     parked record's q or an owner-blocked
                                                     record cites, then `MISS`, `MULTI`, `SKIP`
                                                     lines; --apply binds them (open or
                                                     answered questions), `BOUND` / `NOT BOUND`
                                                     for each, and files an answered one at
                                                     once: `FILED` / `NOT FILED Q-1: <why>`
  hub.py questions edit Q-1 --from KEY --if-version V [--title T] [--context-file F]
        [--option "label::description" ...] [--recommended N] [--refs ID ...]
        [--urgent | --no-urgent] [--record REPO:ID | --no-record] [--class C]
                                                     `EDITED Q-1`; an --option list replaces
                                                     the old one; --no-record unbinds it (with
                                                     --class unless it has one)
  hub.py questions withdraw Q-1 --from KEY --if-version V    `WITHDRAWN Q-1`
                                                     edit and withdraw: an open question whose
                                                     from is KEY (pm when it has none)
  hub.py answer Q-1 --choice N|other [--note TEXT] [--if-version V]     (owner)
                                                     `ANSWERED Q-1 <label>`
  hub.py todos [--status S] [--repo R]              `T-1 open [repo] title` for each
  hub.py todos add --title T [--detail D] [--command C ...] [--steps F] [--repo R]
        [--refs ID ...] [--urgent] [--from NAME | --owner]
                                                     `ADDED T-2`; --owner: the owner's own to-do;
                                                     --steps F: a UTF-8 JSON file holding the
                                                     list of steps the hub makes into commands
                                                     (docs/hub-mcp.md "To-do steps")
  hub.py todos done T-1 [--undo] [--if-version V]   (owner) `DONE T-1`, `REOPENED T-1`
  hub.py todos ack T-1                              `ACKED T-1`
  hub.py todos edit T-1 --from NAME --if-version V [--title T] [--detail D] [--command C ...]
        [--repo R] [--refs ID ...] [--urgent | --no-urgent]     `EDITED T-1`
  hub.py todos withdraw T-1 --from NAME --if-version V      `WITHDRAWN T-1`
                                                     edit and withdraw: an open to-do whose
                                                     from is NAME (pm when it has none)
  hub.py stories [--status S] [--repo R]            `S-1 v2 submitted [repo] title` for each
  hub.py stories add --repo R --title T --story-file F [--urgent]       (owner) `SUBMITTED S-2`
  hub.py stories set S-1 --status review|questions|added|declined [--reviewer NAME]
        [--review-note TEXT] [--backlog-id ID] [--closed-note TEXT] --if-version V
                                                     `SET S-1 <status>`
  hub.py stories withdraw S-1                       (owner) `WITHDRAWN S-1`
  hub.py to-pm [--status S] [--repo R]              `P-1 queued [repo] text` for each
  hub.py to-pm add --text T [--kind K] [--repo R] [--urgent]            (owner) `QUEUED P-2`
  hub.py to-pm send                                 (owner) `SENT P-1 P-2`, or `SENT nothing`
  hub.py to-pm reply P-1 TEXT [--if-version V]                          `REPLIED P-1`
  hub.py inbox                                      `M-1 unread [repo] title` for each
  hub.py inbox add --text T [--title T] [--repo R] [--ref ID] [--asked TEXT] [--refs ID ...]
        [--from NAME]                                `POSTED M-2`
  hub.py inbox read M-1 [--undo] [--if-version V]   (owner) `READ M-1`, `UNREAD M-1`
  hub.py pages [--repo R]                           `ID v2 - [repo] title` for each published page
  hub.py pages publish ID --title T --html-file F [--repo R] [--summary S] [--refs ID ...]
        [--from NAME] [--if-version V]               `PUBLISHED ID`; F is a UTF-8 HTML file the
                                                     hub's page check (kitlib.hubsite.pagecheck)
                                                     passes, checked here before it is sent;
                                                     over a page already published, --if-version
                                                     is its version (docs/action-hub.md
                                                     "pages/<id> and page_html/<id>")
  hub.py pages show ID [--out F]                    the page's HTML, or with --out F written to F
                                                     as UTF-8 and `WROTE F` printed
  hub.py approvals [--status S] [--repo R]          `A-1 pending [repo] title` for each request
                                                     for the owner's approval, its status read
                                                     now (an expired one says expired;
                                                     kitlib.hubsite.approvals status_at)
  hub.py approvals show A-1                         its status, repo, head, title and reason,
                                                     then `<path> <base_blob> -> <blob>` per file
  hub.py approvals add --title T --file PATH CONTENT_FILE [--file ...] [--checkout DIR] [--repo R]
        [--head SHA] [--reason TEXT]                 `REQUESTED A-001`: asks the owner to approve
                                                     writing each PATH (relative to the repo top)
                                                     as CONTENT_FILE's exact UTF-8 bytes; its base
                                                     is PATH's blob at --head (default: HEAD of
                                                     the checkout DIR, default .), none for a new
                                                     file; --repo defaults to DIR's origin. It only
                                                     asks: no delete, no --from (kitlib.hubapprove)
  hub.py approvals withdraw A-1 --from KEY --if-version V    `WITHDRAWN A-1`
                                                     the requester takes back a request still
                                                     pending (KEY is pm: approvals store no from);
                                                     a decided or expired one is refused
  hub.py skill-edit [--status S]                    `E-1 v2 submitted skill NAME` for each of the
                                                     owner's edits of a kit skill (the owner
                                                     writes them on the hub page only;
                                                     docs/action-hub.md "skill_edits/E-NNN")
  hub.py skill-edit show E-1 [--out F]              its status, skill and base blob; --out F
                                                     writes the owner's text to F byte for byte
  hub.py skill-edit done E-1 --branch B [--checkout DIR] --if-version V    `COMMITTED E-1`
                                                     posts B and the blob of the skill's SKILL.md
                                                     on B in DIR; the hub refuses a blob that is
                                                     not the owner's text
  hub.py skill-edit fail E-1 --why W [--at REV] [--checkout DIR] --if-version V    `FAILED E-1`, or
                                                     `SUPERSEDED E-1` when the blob at REV is not
                                                     the one the owner edited
  hub.py pending [--for KEY]                        `<kind> <id> [repo] to=R from=K title` for
                                                     each item of owner input not yet handled: R
                                                     is the session key to handle it now (pm
                                                     when its creator K cannot be reached);
                                                     --for KEY: only the items for session KEY
                                                     (--for pm: the ones routed to the PM and
                                                     every answered question, the classed
                                                     answers last, after an `AUDIT` line);
                                                     class=C on a classed question
  hub.py wait [--since REV] [--timeout S] [--pending [--for KEY] [--except ID...]]
      waits until the store changes after revision REV (default: the revision at the call), or,
      with --pending, until an item is pending (one that is pending already ends it at once);
      with --for KEY as well, until an item for session KEY is pending; with --except, items
      with those ids (ones already acted on, still pending until the PM logs them) are left out.
      The PM waits with --for pm. Prints `HUB <rev> <kind> <id> to=R from=K <title>` for each
      pending item (R and K as for pending) and `HUB <rev> <collection>
      <id> <op> <status>` for each change, exit 0. With --timeout S: exit 4 and no line when S
      seconds pass with neither. Without it the wait has no end of its own. A hub that goes
      down after a poll of the wait was answered (port closed, or `REFUSED store`: a restart) is
      asked again each second until S runs out, or 600 s down in a row, then that reason is the
      exit (3 or 1); one down at the first poll ends the wait at once (client.Client.wait).
  hub.py validate [--store PATH]
      reads the store folder itself (hub.store, else hub/ in the PM data worktree), with no
      server and no lock, so it may run beside one; it writes nothing. Prints each finding as
      `VALIDATE <LEVEL> <document> <field>: <message> [<code>]` and a summary line. Exit 1 when
      a finding is an error.

`--choice N` and `--recommended N` are the option's place in the question's list, counted from
0 as the api counts it. `questions log`, `questions done`, `stories set` and each `edit` and
`withdraw` (`approvals withdraw` too) need --if-version V: the version of the document the caller read and decided on, which the `questions`
and `stories` lines show as `v2` and --json (`todos --json` too) as `version`. Without it the verb is a usage error and asks nothing; with a version
the document no longer has, the server refuses the write (`REFUSED conflict`).

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.hubsite, prints and exits.
Exit codes: 0 ok; 1 refused (`REFUSED: ...`, or `REFUSED <code>: <message>` when the server
refused the call), a store with an error finding, a server that did not close cleanly, or a
Ctrl+C other than the one that closes a running server; 2 usage error or a machine config that
cannot be read; 3 the server is not running (the start command is printed), or `restart`
started nothing; 4 `wait --timeout` ran out with nothing to say, or `stop` or `restart` ran out
of time (nothing was killed).
"""
import argparse
import datetime
import json
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import hubapprove, hubvalidate                         # noqa: E402
from kitlib.config import copies, user                             # noqa: E402
from kitlib.config.types import ConfigError                        # noqa: E402
from kitlib.hubsite import access, api, auth, client, httpd, serve, settings, tunnel   # noqa: E402
from kitlib.hubsite import approvals                               # noqa: E402
from kitlib.hubsite import autostart as hub_autostart              # noqa: E402
from kitlib.hubsite import control                                 # noqa: E402
from kitlib.hubsite import pagecheck                               # noqa: E402
from kitlib.hubsite import skilledits                              # noqa: E402
from kitlib.hubsite import storecheck                              # noqa: E402

START = 'python "%s" serve' % os.path.abspath(__file__)
OPEN = 'python "%s" open' % os.path.abspath(__file__)
HEALTH = api.BASE + '/health'
# every verb, in the order the usage error names them
VERB_NAMES = ('serve', 'status', 'setup', 'open', 'unpair', 'stop', 'restart',
              'questions', 'answer', 'todos', 'stories', 'to-pm', 'inbox', 'pages', 'approvals',
              'skill-edit', 'pending', 'wait', 'validate')
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


def _record(text):
    """--record's value: "REPO:ID", split at the first ':' (a repository name holds none), as
    the {repo, id} the server checks against its configured repositories."""
    repo, sep, rid = text.partition(':')
    if not sep or not repo.strip() or not rid.strip():
        raise argparse.ArgumentTypeError('a record is REPO:ID (a configured repository and one '
                                         'of its record ids), not %r' % text)
    return {'repo': repo.strip(), 'id': rid.strip()}


def _bind_lines(got):
    """The lines `questions bind` prints for the hub's answer."""
    def named(r):
        r = r if isinstance(r, dict) else {}
        return '%s:%s' % (r.get('repo'), r.get('id')) if r.get('id') else str(r.get('repo'))
    lines = []
    for p in got.get('pairs') or []:
        word = 'BOUND' if p.get('bound') else 'BIND'
        lines.append('%s %s %s' % (word, p.get('question'), named(p.get('record'))))
        f = p.get('filing')
        if not isinstance(f, dict):
            continue
        if f.get('filed'):
            lines.append('FILED %s' % p.get('question'))
        elif f.get('moved'):
            lines.append('NOT FILED %s: moved (filed by the answer handler)' % p.get('question'))
        elif f.get('unanswered'):
            lines.append('NOT FILED %s: no longer answered' % p.get('question'))
        else:
            lines.append('NOT FILED %s: %s (%s)' % (p.get('question'), f.get('message'),
                                                    f.get('code')))
    lines += ['NOT BOUND %s: %s (%s)' % (f.get('question'), f.get('message'), f.get('code'))
              for f in got.get('failed') or []]
    lines += ['MISS %s: %s' % (named(m.get('record')), m.get('why'))
              for m in got.get('misses') or []]
    lines += ['MULTI %s cites %s' % (named(m.get('record')), ' '.join(m.get('questions') or []))
              for m in got.get('multi') or []]
    lines += ['SKIP %s: %s' % (s.get('question'), s.get('why')) for s in got.get('skipped') or []]
    return lines or ['nothing to bind']


def _question_class(x, what):
    """--class on questions add and edit (docs/action-hub.md "A question's class")."""
    x.add_argument('--class', dest='question_class', choices=api.QUESTION_CLASSES,
                   help=what + ': rule (a standing rule or a delegation run line), machine, '
                        'cross-repo or go (a one-off landing or archive go)')


def _unfiled(x):
    """--unfiled on questions log and done (docs/action-hub.md "Filing the answer")."""
    x.add_argument('--unfiled', metavar='WHY',
                   help='why an answer whose filing into its record failed for good (not '
                        'found, invalid, no store) is handled without it')


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


def _creator(p, metavar):
    """The id, --from and --if-version of a creator's edit or withdraw: the hub checks --from
    against the item's own (kitlib.hubsite.api _creator)."""
    p.add_argument('id')
    p.add_argument('--from', dest='who', required=True, metavar=metavar,
                   help='the from the item was added with (pm when it was added without one)')
    _pins(p, required=True)


def _repo(p):
    """--repo of an `add` whose verb lists by --repo too. No default is set, as in _asks: the
    sub-verb's own would put None over a --repo given before `add`. Read it with getattr."""
    p.add_argument('--repo', default=argparse.SUPPRESS)


def _listed(sub, name, what, statuses=None, repo=True, since=False):
    """The parser of a verb that lists its collection when no sub-verb follows; its
    sub-parsers. `since`: the list takes --since, an ISO time with its offset (the route keeps
    the documents written at or after it)."""
    p = _asks(sub.add_parser(name, help=what))
    if statuses:
        p.add_argument('--status', choices=statuses + ('all',), default=argparse.SUPPRESS)
    if repo:
        p.add_argument('--repo', default=argparse.SUPPRESS)
    if since:
        p.add_argument('--since', default=argparse.SUPPRESS, metavar='ISO',
                       help='only those written at or after this time (ISO, with its offset)')
    return p.add_subparsers(dest='sub')


def _leaf(subs, name, what):
    return _asks(subs.add_parser(name, help=what))


def _client_verbs(sub):
    S = hubvalidate.STATUSES
    q = _listed(sub, 'questions', 'list the questions; add: ask one; log: mark it logged; '
                'done: handled with no ruling; edit, withdraw: change or remove one you asked; '
                'file: file answers into their records again', S['questions'], since=True)
    x = _leaf(q, 'show', 'one question with its options, context and answer')
    x.add_argument('id')
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
    x.add_argument('--record', type=_record, metavar='REPO:ID',
                   help='the backlog record the question is about (checked by the hub)')
    _question_class(x, 'with no --record: what kind of question it is')
    x.add_argument('--from', dest='who', metavar='KEY',
                   help='your launcher session key: the answer comes back to it (default: pm)')
    x = _leaf(q, 'log', 'mark an answered question logged, its answer being in the ledger')
    x.add_argument('id')
    _unfiled(x)
    _pins(x, required=True)
    x = _leaf(q, 'done', 'mark an answered question handled with no ruling (a one-off go)')
    x.add_argument('id')
    x.add_argument('--why', required=True, help='why the answer needs no ruling')
    _unfiled(x)
    _pins(x, required=True)
    x = _leaf(q, 'file', 'file each answer to a question bound to a record that is not filed '
              'yet into its record (the hub does at once and at start)')
    x.add_argument('id', nargs='?', help='only this question')
    x = _leaf(q, 'bind', 'bind open or answered questions to the records that cite them, '
              'filing an answered one at once (the PM)')
    mode = x.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', dest='dry_run', action='store_true',
                      help='list what would be bound, and what cannot be')
    mode.add_argument('--apply', action='store_true', help='bind them')
    x = _leaf(q, 'edit','change an open question you asked')
    _creator(x, 'KEY')
    x.add_argument('--title')
    x.add_argument('--context-file', dest='context_file', metavar='F',
                   help='a UTF-8 file with the new context')
    x.add_argument('--option', dest='options', action='append', type=_option, default=[],
                   metavar='"label::description"',
                   help='an answer to choose (repeatable; the list given replaces the old one)')
    x.add_argument('--recommended', type=_count, metavar='N',
                   help='the place of the recommended option (counts from 0)')
    x.add_argument('--refs', nargs='+', metavar='ID')
    x.add_argument('--urgent', action=argparse.BooleanOptionalAction, default=None)
    bind = x.add_mutually_exclusive_group()
    bind.add_argument('--record', type=_record, metavar='REPO:ID',
                      help='bind the question to this backlog record (checked by the hub)')
    bind.add_argument('--no-record', dest='no_record', action='store_true',
                      help='bind the question to no record')
    _question_class(x, 'the class of a question bound to no record')
    _creator(_leaf(q, 'withdraw', 'remove an open question you asked'), 'KEY')

    x = _asks(sub.add_parser('answer', help='answer a question (the owner)'))
    x.add_argument('id')
    x.add_argument('--choice', required=True, type=_choice, metavar='N|other',
                   help='the place of the chosen option (counts from 0), or other (which needs '
                        '--note)')
    x.add_argument('--note')
    _pins(x)

    t = _listed(sub, 'todos', 'list the to-dos; add, done, ack, edit, withdraw', S['todos'])
    x = _leaf(t, 'add', 'add a to-do')
    x.add_argument('--title', required=True)
    x.add_argument('--detail')
    x.add_argument('--command', dest='commands', action='append', metavar='C',
                   help='a command the to-do names (repeatable)')
    x.add_argument('--steps', dest='steps_file', metavar='F',
                   help='a UTF-8 JSON file holding a list of steps: {edit, old, new}, '
                        '{open, line} or {run}; the hub makes them commands after --command')
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
    x = _leaf(t, 'edit', 'change an open to-do you added')
    _creator(x, 'NAME')
    x.add_argument('--title')
    x.add_argument('--detail')
    x.add_argument('--command', dest='commands', action='append', metavar='C',
                   help='a command the to-do names (repeatable; the list given replaces the old '
                        'one)')
    x.add_argument('--repo', dest='edit_repo', metavar='R')
    x.add_argument('--refs', nargs='+', metavar='ID')
    x.add_argument('--urgent', action=argparse.BooleanOptionalAction, default=None)
    _creator(_leaf(t, 'withdraw', 'remove an open to-do you added'), 'NAME')

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

    g = _listed(sub, 'pages', 'list the published pages; publish, show')
    x = _leaf(g, 'publish', 'publish an HTML page to the hub')
    x.add_argument('id')
    x.add_argument('--title', required=True)
    x.add_argument('--html-file', dest='html_file', required=True, metavar='F',
                   help='a UTF-8 HTML file: no script, no remote URL, Mermaid rendered to svg')
    _repo(x)
    x.add_argument('--summary')
    x.add_argument('--refs', nargs='+', metavar='ID')
    x.add_argument('--from', dest='who', metavar='NAME')
    _pins(x)
    x = _leaf(g, 'show', "a page's HTML")
    x.add_argument('id')
    x.add_argument('--out', metavar='F', help='write the HTML to this file instead')

    e = _listed(sub, 'skill-edit', "list the owner's edits of kit skills; show; done: the "
                "text is committed on a branch; fail: it could not be", S['skill_edits'],
                repo=False)
    x = _leaf(e, 'show', "one edit; --out writes the owner's text to a file, byte for byte")
    x.add_argument('id')
    x.add_argument('--out', metavar='F', help="write the owner's text to this file")
    x = _leaf(e, 'done', 'mark a submitted edit committed: the hub checks the blob of the '
              "skill's SKILL.md on --branch against the owner's text")
    x.add_argument('id')
    x.add_argument('--branch', required=True, help='the branch the commit is on')
    x.add_argument('--checkout', default='.', metavar='DIR',
                   help='the checkout to read the branch in (default: .)')
    _pins(x, required=True)
    x = _leaf(e, 'fail', 'mark a submitted edit failed; with --at, superseded when the skill '
              'changed since the owner wrote it')
    x.add_argument('id')
    x.add_argument('--why', required=True, help='what stopped the commit')
    x.add_argument('--at', metavar='REV',
                   help="send the blob of the skill's SKILL.md at this revision (main): the "
                        'hub marks the edit superseded when it is not the one the owner edited')
    x.add_argument('--checkout', default='.', metavar='DIR',
                   help='the checkout to read --at in (default: .)')
    _pins(x, required=True)

    x = _listed(sub, 'approvals', "list the requests for the owner's approval; show; add: ask "
                "for one; withdraw: take back a pending one", S['approvals'])
    _leaf(x, 'show', 'one request and its files').add_argument('id')
    y = _leaf(x, 'add', "ask the owner to approve writing these files")
    y.add_argument('--checkout', default='.', metavar='DIR',
                   help='the git checkout the change is for (default: .)')
    y.add_argument('--title', required=True)
    y.add_argument('--file', dest='files', action='append', nargs=2, required=True,
                   metavar=('PATH', 'CONTENT_FILE'),
                   help='a path relative to the repo top and the UTF-8 file holding its new '
                        'text (repeatable)')
    _repo(y)
    y.add_argument('--head', metavar='SHA', help="the base commit (default: the checkout's HEAD)")
    y.add_argument('--reason')
    _creator(_leaf(x, 'withdraw', 'take back a request still pending'), 'KEY')

    x = _asks(sub.add_parser('pending', help='the owner input the PM has not handled'))
    x.add_argument('--for', dest='for_key', metavar='KEY',
                   help="only the items for this session key (default: every item)")
    w = _asks(sub.add_parser('wait', help='wait for a change of the store or a pending item'))
    w.add_argument('--since', type=_count, metavar='REV',
                   help='changes after this revision (default: the revision at the call)')
    w.add_argument('--timeout', type=_seconds, metavar='S',
                   help='exit 4 after this many seconds with nothing (default: no limit)')
    w.add_argument('--pending', action='store_true', help='pending items end the wait too')
    w.add_argument('--for', dest='for_key', metavar='KEY',
                   help="with --pending: only this session key's items end the wait, and no "
                        "other change does")
    w.add_argument('--except', dest='except_ids', nargs='+', metavar='ID',
                   help='with --pending: leave out these ids (ones already acted on); only '
                        'another pending item ends the wait')
    v = sub.add_parser('validate', help='check the store folder; reads it, no server needed')
    v.add_argument('--store', type=_folder, help='the store folder (default: the configured one)')


def parser():
    p = Parser(prog='hub.py', description='The hub server of this machine.')
    sub = p.add_subparsers(dest='verb')
    s = sub.add_parser('serve', help='serve the hub until Ctrl+C or hub.py stop')
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
    for name, what in (('stop', 'ask the running server to close cleanly, and wait (the owner)'),
                       ('restart', 'stop the server and start its scheduled task again (the '
                                   'owner)')):
        r = sub.add_parser(name, help=what)
        r.add_argument('--port', type=_port(1), help='the port (default: hub.port)')
        r.add_argument('--timeout', type=_seconds, default=control.DEFAULT_TIMEOUT_S, metavar='S',
                       help='give up after about this many seconds, ending nothing (default: '
                            '%d); a request in flight may run past it by up to %d s, a read of '
                            "the task's state by up to %d s"
                            % (control.DEFAULT_TIMEOUT_S, serve.ASK_TIMEOUT_S,
                               hub_autostart.TASK_TIMEOUT_S))
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


def _push_delivery():
    """(delivery, folder) for push notifications (docs/action-hub.md "Push notifications"): the
    host adapter's push() and the launcher's state folder, or (None, None) with one note line
    saying why there are none. A scratch store (`serve --store`) never asks."""
    from kitlib.launcher import settings as launcher_settings
    try:
        launch = launcher_settings.load()
        folder = launch.state_dir()
        import _host
        make = getattr(_host.adapter_module().ADAPTER, 'push', None)
        if make is None:
            _log('note', 'push notifications off: this host has no delivery')
            return None, None
        return make(launch), folder
    except Exception as e:              # noqa: BLE001 - the hub serves without push notifications
        _log('note', 'push notifications off: %s: %s' % (type(e).__name__, e))
        return None, None


def _serve(a, stop):
    s = settings.load()
    if a.port is not None:
        s = s._replace(port=a.port)
    scratch = a.store is not None       # never the truth of the text: '' is refused by the parser
    if scratch:
        s = s._replace(store=os.path.abspath(a.store), push=False)
    sources = serve.machine_sources(sync=not scratch)
    if not scratch:
        sources.push, sources.push_dir = _push_delivery()
    hub = serve.build(s, sources, log=_log)

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
    if a.autostart:
        print('start the server detached, once the task is registered: %s' % _START_TASK)
        for line in hub_autostart.install(os.path.abspath(__file__)):
            print(line)
    else:
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


# restart that starts nothing: why, by control's outcome
NOT_STARTED = {
    control.NO_TASK: "there is no scheduled task '%s' (hub.py setup --autostart makes it), so "
                     "nothing was started" % hub_autostart.TASK_NAME,
    control.DISABLED: "the scheduled task '%s' is disabled, so nothing was started"
                      % hub_autostart.TASK_NAME,
    control.HAND_STARTED: "the server was started by hand, not by the scheduled task '%s', so "
                          "nothing was started" % hub_autostart.TASK_NAME,
}
_TASK = "-TaskName '%s'" % hub_autostart.TASK_NAME
_START_TASK = 'Start-ScheduledTask %s' % _TASK


def task_advice(outcome):
    """The last line when a restart started nothing, by control's outcome (None: the task could
    not be started). `serve` is not it: a server started in a console ends with that shell; the
    scheduled task runs it detached and windowless."""
    if outcome == control.NO_TASK:
        return 'register the task, then start it: python "%s" setup --autostart; %s' % (
            os.path.abspath(__file__), _START_TASK)
    if outcome == control.DISABLED:
        return 'enable the task, then start it: Enable-ScheduledTask %s; %s' % (_TASK, _START_TASK)
    return 'start it with: %s' % _START_TASK


# a server the task starts has no console: what it says at start-up goes nowhere
NO_CONSOLE = ('a server the scheduled task starts has no console, so its start-up lines are not '
              'shown: run hub.py serve in a console to see why it does not answer: %s' % START)


def _owner_token():
    folder = auth.folder()
    token = auth.read_token(folder, auth.OWNER)
    if not token:
        raise serve.ServeError('no owner token in %s: run hub.py setup' % folder)
    return token


def _stopped(port, stopped):
    if not stopped.ran:
        print('nothing to stop: nothing answers on port %d; %s' % (port, control.STILL_CLOSING))
    elif stopped.pid is None:
        print('STOPPED: the hub server on port %d has closed' % port)
    else:
        print('STOPPED: the hub server on port %d (pid %d) has closed and its process has ended'
              % (port, stopped.pid))


def _stop(a, stop):
    port = a.port or settings.load().port
    token = _owner_token()
    try:
        got = control.stop(port, token, a.timeout)
    except control.Refused as e:
        return _refused(e.port, e.status, e.answer)
    except control.TimedOut as e:
        print(serve.scrub('TIMED OUT: %s' % e), file=sys.stderr)
        return client.TIMED_OUT
    _stopped(port, got)
    return 0


def _restart(a, stop):
    new_port = settings.load().port
    port = a.port or new_port
    token = _owner_token()
    try:
        got = control.restart(port, token, a.timeout, new_port)
    except control.Refused as e:
        return _refused(e.port, e.status, e.answer)
    except control.StartFailed as e:
        # the stop went through: say so, then why nothing was started and how to start it
        _stopped(port, e.stopped)
        print(serve.scrub('the scheduled task could not be started: %s' % e.reason),
              file=sys.stderr)
        print(task_advice(None), file=sys.stderr)
        return 1
    except control.TimedOut as e:
        print(serve.scrub('TIMED OUT: %s' % e), file=sys.stderr)
        if e.phase == control.ANSWER_PHASE:
            # the task was started and its server never answered: why is in its start-up lines
            print(NO_CONSOLE, file=sys.stderr)
        return client.TIMED_OUT
    _stopped(port, got.stopped)
    if got.outcome != control.RESTARTED:
        print(NOT_STARTED[got.outcome])
        print(task_advice(got.outcome))
        return client.NOT_RUNNING
    print("RESTARTED: the hub server answers on port %d (the scheduled task '%s' started it)"
          % (got.port, hub_autostart.TASK_NAME))
    return 0


def _store_root(a):
    root = os.path.abspath(a.store) if a.store is not None else settings.load().store
    if not root:
        raise serve.ServeError('no store folder is set: set hub.store or pm_data in the machine '
                               'config, or give --store PATH')
    return root


def _validate(a, stop):
    try:
        findings = storecheck.validate(_store_root(a))
    except storecheck.StoreReadError as e:
        raise serve.ServeError(str(e))
    for line in storecheck.validate_lines(findings):
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
    return '%s %s [%s] %s%s' % (name, state or d.get('status') or '-', d.get('repo') or '-', title,
                                _done_note(d))


def _done_note(d):
    """` (done by the hub: <why>)` after a question the hub marked done once its asker had the
    filed answer (done_by, docs/action-hub.md "Done by the hub"), ` (done: <why>)` after one its
    asker or the PM marked done; nothing for any other document. The why on one line."""
    by, why = d.get('done_by'), d.get('done_why')
    why = ' '.join(why.split()) if isinstance(why, str) else ''
    if isinstance(by, str) and by.strip():
        who = 'the hub' if by == 'hub' else by.strip()
        return ' (done by %s%s)' % (who, ': ' + why if why else '')
    return ' (done: %s)' % why if why else ''


def _list(a, collection, version=False):
    """The lines of a collection. `version`: each line shows its document's version, for the
    collections that have a write which needs it named (--if-version)."""
    got = _hub(a).get('/' + collection, query={'status': getattr(a, 'status', None),
                                              'repo': getattr(a, 'repo', None),
                                              'since': getattr(a, 'since', None)})
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


def _json_of(path, what):
    """The JSON value of a UTF-8 file (a BOM is skipped); the server judges its shape."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
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
            record=a.record, **{'from': a.who, 'class': a.question_class}))
        return _wrote(a, got, 'ASKED %s' % got.get('id'))
    if a.sub == 'log':
        body = {'if_version': a.if_version}
        if a.unfiled is not None:
            body['unfiled'] = a.unfiled
        got = hub.post(_one('questions', a) + '/log', client.AGENT, body)
        return _wrote(a, got, 'LOGGED %s' % got.get('id'))
    if a.sub == 'done':
        body = {'why': a.why, 'if_version': a.if_version}
        if a.unfiled is not None:
            body['unfiled'] = a.unfiled     # sent only when asked: an older hub refuses unknown fields
        got = hub.post(_one('questions', a) + '/done', client.AGENT, body)
        return _wrote(a, got, 'DONE %s' % got.get('id'))
    if a.sub == 'file':
        got = hub.post('/questions/file', client.AGENT, _body(id=a.id))
        lines = ['FILED %s' % i for i in got.get('filed') or []]
        lines += ['NOT FILED %s: %s (%s)' % (f.get('id'), f.get('message'), f.get('code'))
                  for f in got.get('failed') or [] if isinstance(f, dict)]
        return _show(a, got, lines or ['nothing to file'])
    if a.sub == 'bind':
        got = hub.post('/questions/bind', client.AGENT, {'from': 'pm', 'apply': bool(a.apply)})
        return _show(a, got, _bind_lines(got))
    if a.sub == 'edit':
        context = _text_of(a.context_file, 'context') if a.context_file else None
        body = _body(title=a.title, context=context, options=a.options,
                     recommended=a.recommended, refs=a.refs, record=a.record,
                     **{'class': a.question_class})
        if a.no_record:
            body['record'] = None
        got = hub.post(_one('questions', a) + '/edit', client.AGENT, _edits(a, body))
        return _wrote(a, got, 'EDITED %s' % got.get('id'))
    if a.sub == 'withdraw':
        got = hub.post(_one('questions', a) + '/delete', client.AGENT, _edits(a, {}))
        return _wrote(a, got, 'WITHDRAWN %s' % got.get('id'))
    if a.sub == 'show':
        return _question(a)
    return _list(a, 'questions', version=True)


def _question(a):
    """questions show: the one document of `a.id` out of the list of every status."""
    got = _hub(a).get('/questions', query={'status': 'all'})
    for d in got.get('items') or []:
        if d.get('id') == a.id:
            break
    else:
        raise serve.ServeError('%s is not a question the hub holds' % a.id)
    if getattr(a, 'json', False):
        return _show(a, d, [])
    lines = [_item(d, version=True)]
    for n, o in enumerate(d.get('options') or []):
        if isinstance(o, dict):
            mark = ' (recommended)' if d.get('recommended') == n else ''
            lines.append('option %d: %s%s - %s' % (n, o.get('label'), mark, o.get('description') or ''))
    if d.get('context'):
        lines.append('context: %s' % d['context'].strip())
    answer = d.get('answer') if isinstance(d.get('answer'), dict) else None
    if answer:
        lines.append('answer: %s' % answer.get('label'))
        if answer.get('note'):
            lines.append('note: %s' % answer['note'])
    return _show(a, d, lines)


def _edits(a, body):
    """The body of a creator's edit or withdraw: its fields, --from and --if-version, and
    --urgent or --no-urgent when one was given (_body leaves a False out)."""
    body.update({'from': a.who, 'if_version': a.if_version})
    if getattr(a, 'urgent', None) is not None:
        body['urgent'] = a.urgent
    return body


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
        if a.steps_file:
            body['steps'] = _json_of(a.steps_file, 'steps')
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
    if a.sub == 'edit':
        got = hub.post(_one('todos', a) + '/edit', client.AGENT, _edits(a, _body(
            title=a.title, detail=a.detail, commands=a.commands, repo=a.edit_repo, refs=a.refs)))
        return _wrote(a, got, 'EDITED %s' % got.get('id'))
    if a.sub == 'withdraw':
        got = hub.post(_one('todos', a) + '/delete', client.AGENT, _edits(a, {}))
        return _wrote(a, got, 'WITHDRAWN %s' % got.get('id'))
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
            reply=a.reply, if_version=a.if_version))
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


def _pages(a, stop):
    """No delete: deleting a page is the owner's, on the hub page only."""
    hub = _hub(a)
    if a.sub == 'publish':
        html = _text_of(a.html_file, 'html')
        why = pagecheck.check(html)
        if why:
            raise serve.ServeError('the page check refuses %s: %s' % (a.html_file, why))
        got = hub.post('/pages', client.AGENT, _body(
            id=a.id, title=a.title, html=html, repo=getattr(a, 'repo', None), summary=a.summary,
            refs=a.refs, if_version=a.if_version, **{'from': a.who}))
        return _wrote(a, got, 'PUBLISHED %s' % got.get('id'))
    if a.sub == 'show':
        got = hub.page(a.id)
        html = got.get('html') if isinstance(got.get('html'), str) else ''
        if a.out:
            try:
                with open(a.out, 'w', encoding='utf-8', newline='') as f:
                    f.write(html)
            except OSError as e:
                raise serve.ServeError('cannot write %s: %s' % (a.out, type(e).__name__))
            return _show(a, {'ok': True, 'id': got.get('id'), 'version': got.get('version'),
                             'out': a.out}, ['WROTE %s' % a.out])
        if getattr(a, 'json', False):
            return _show(a, got, [])
        try:
            sys.stdout.write(html + '\n')
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
            sys.stdout.write(html.encode(enc, 'backslashreplace').decode(enc) + '\n')
        return client.OK
    return _list(a, 'pages', version=True)


def _blob_at(checkout, rev, skill):
    """The blob id of the skill's source at `rev` in `checkout` (git rev-parse, a read)."""
    from kitlib.git import cmd
    path = skilledits.source_path(skill)
    out, why = cmd.run_text(['rev-parse', '--verify', '--quiet', '%s:%s' % (rev, path)],
                            checkout)
    if not skilledits.is_blob(out):
        raise serve.ServeError('cannot read %s at %s in %s: %s'
                               % (path, rev, checkout, why or 'not found'))
    return out


def _skill_edit(a, stop):
    """The owner's edits are the owner's (the hub page): an agent shows, commits and reports."""
    hub = _hub(a)
    if a.sub in ('done', 'fail'):
        d = hub.get(_one('skill_edits', a)).get('item') or {}
        if a.sub == 'done':
            body = {'branch': a.branch, 'if_version': a.if_version,
                    'blob': _blob_at(a.checkout, a.branch, d.get('skill'))}
        else:
            body = _body(why=a.why, if_version=a.if_version)
            if a.at:
                body['current_blob'] = _blob_at(a.checkout, a.at, d.get('skill'))
        got = hub.post(_one('skill_edits', a) + '/' + a.sub, client.AGENT, body)
        return _wrote(a, got, '%s %s' % ('COMMITTED' if a.sub == 'done' else
                                         str(got.get('status') or 'failed').upper(), got.get('id')))
    if a.sub == 'show':
        got = hub.get(_one('skill_edits', a))
        d = got.get('item') or {}
        text = d.get('text') if isinstance(d.get('text'), str) else ''
        if a.out:
            try:
                with open(a.out, 'w', encoding='utf-8', newline='') as f:
                    f.write(text)
            except OSError as e:
                raise serve.ServeError('cannot write %s: %s' % (a.out, type(e).__name__))
            return _show(a, {'ok': True, 'id': d.get('id'), 'version': d.get('version'),
                             'out': a.out}, ['WROTE %s' % a.out])
        return _show(a, got, ['%s v%s %s skill %s base %s' % (
            d.get('id'), d.get('version'), d.get('status'), d.get('skill'), d.get('base_blob'))]
            + ['%s: %s' % (k, d[k]) for k in ('branch', 'blob', 'source_blob', 'why') if d.get(k)])
    got = hub.get('/skill_edits', query={'status': getattr(a, 'status', None)})
    return _show(a, got, ['%s v%s %s skill %s' % (d.get('id'), d.get('version'), d.get('status'),
                                                   d.get('skill'))
                          for d in got.get('items') or []])


def _approvals(a, stop):
    """The requests from GET /snapshot (the api has no route that lists approvals), each with
    its status read now: approvals.status_at applies the expiry, `malformed` for a bad document.
    add posts one request (kitlib.hubapprove): it never decides or consumes one."""
    if a.sub == 'withdraw':
        got = _hub(a).post(_one('approvals', a) + '/delete', client.AGENT, _edits(a, {}))
        return _wrote(a, got, 'WITHDRAWN %s' % got.get('id'))
    if a.sub == 'add':
        try:
            body = hubapprove.build(a.checkout, a.title, [tuple(f) for f in a.files],
                                    repo=getattr(a, 'repo', None), head=a.head, reason=a.reason)
        except hubapprove.RequestError as e:
            raise serve.ServeError(e.message)
        got = hubapprove.request(_hub(a), body)
        return _wrote(a, got, 'REQUESTED %s' % got.get('id'))
    docs = (_hub(a).get('/snapshot').get('collections') or {}).get('approvals') or {}
    now = datetime.datetime.now(datetime.timezone.utc)
    items = [dict(docs[k], id=k, status=approvals.status_at(docs[k], now) or 'malformed')
             if isinstance(docs[k], dict) else {'id': k, 'status': 'malformed'}
             for k in sorted(docs, key=api._natural)]
    if a.sub == 'show':
        d = next((d for d in items if d['id'] == a.id), None)
        if d is None:
            raise serve.ServeError('no approval %s' % a.id)
        lines = [_item(d), 'head %s' % d.get('head')] + (
            ['reason %s' % d['reason']] if d.get('reason') else [])
        lines += ['%s %s -> %s' % (f.get('path'), f.get('base_blob') or 'new',
                                   f.get('blob') or 'deleted')
                  for f in d.get('files') or [] if isinstance(f, dict)]
        return _show(a, d, lines)
    status, repo = getattr(a, 'status', 'all'), getattr(a, 'repo', '').strip().lower()
    items = [d for d in items if status in ('all', d['status'])
             and (not repo or str(d.get('repo') or '').strip().lower() == repo)]
    return _show(a, {'items': items}, [_item(d) for d in items])


def _routed(p):
    """`to=<route> from=<from>` of pending item p: the session key that is to handle it now and
    the key it is for (docs/action-hub.md "Who handles a pending item"); `-` for one not given."""
    return 'to=%s from=%s' % (p.get('route') or '-', p.get('from') or '-')


def _title(p):
    """The title of pending item p, marked `(filed)` when its answer is filed into its record."""
    return (p.get('title') or '') + (' (filed)' if p.get('filed') is True else '')


def _pending(a, stop):
    got = _hub(a).get('/pending', client.ANY, {'for': a.for_key})
    items = got.get('items') or []
    lines = [_pending_line(p) for p in items]
    classed = [p for p in items if p.get('class')]
    if a.for_key == api.AGENT_FROM and classed:
        # the PM's audit: an answer with a class has no record to be filed into
        lines = ([_pending_line(p) for p in items if not p.get('class')]
                 + ['AUDIT classed answers, no record to file into:']
                 + [_pending_line(p) for p in classed])
    return _show(a, got, lines)


def _pending_line(p):
    """`<kind> <id> [repo] to=R from=K [class=C] title` of pending item p."""
    return '%s %s [%s] %s %s%s' % (p.get('kind'), p.get('id'), p.get('repo') or '-', _routed(p),
                                   'class=%s ' % p['class'] if p.get('class') else '', _title(p))


def _wait(a, stop):
    got = _hub(a).wait(a.since, a.timeout, a.pending, a.for_key, a.except_ids)
    if got.get('timed_out'):
        if getattr(a, 'json', False):
            _show(a, got, [])
        return client.TIMED_OUT
    rev = got.get('revision')
    lines = ['HUB %s %s %s %s %s' % (rev, p.get('kind'), p.get('id'), _routed(p),
                                     _title(p))
             for p in got.get('pending') or []]
    lines += ['HUB %s %s %s %s %s' % (rev, c.get('collection'), c.get('id'), c.get('op'),
                                      c.get('status') or '-')
              for c in got.get('changes') or []]
    if got.get('reset'):
        lines.append('HUB %s reset: the store is behind --since; read it again' % rev)
    return _show(a, got, lines)


VERBS = {'serve': _serve, 'status': _status, 'setup': _setup, 'open': _open,
         'unpair': _unpair, 'stop': _stop, 'restart': _restart,
         'questions': _questions, 'answer': _answer, 'todos': _todos,
         'stories': _stories, 'to-pm': _to_pm, 'inbox': _inbox, 'pages': _pages,
         'approvals': _approvals, 'skill-edit': _skill_edit,
         'pending': _pending,
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
    if a.verb == 'wait' and a.for_key and not a.pending:
        p.error('--for picks among the pending items: give --pending with it')
    if a.verb == 'wait' and a.except_ids and not a.pending:
        p.error('--except leaves out pending items: give --pending with it')
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
