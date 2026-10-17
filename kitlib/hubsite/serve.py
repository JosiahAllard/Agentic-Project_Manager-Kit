"""kitlib.hubsite.serve: the hub server, wired from its parts (the hub site design, sections 2
and 9).

  build(settings, sources, log=None) -> Hub     open the store, make the views, the Api and the
                                                listeners; nothing is started. ServeError.
  Hub   .settings .store .api .views .creds .listeners .sync .backlog .notes .tunnel .port .url
        .stopping                               the event the owner's stop route sets
        .start()                               start the git sync, the backlog timer, the listeners
        .watch()                                log a git sync failure that is new (below)
        .close(timeout=CLOSE_TIMEOUT_S)         the clean close (below); safe to call twice
  run(hub, started=None, stop=None)             start, serve until `stop` is set, the owner's
                                                stop route is called or Ctrl+C, close
  Sources(...)                                  where each part comes from
  machine_sources(home=None, sync=True)         this machine's Sources, from its machine config
  SiteViews                                     the Api's `views` object over those sources
  health_route(api, views, tunnel='', fixed=False)
                                                a listener's route for GET health (below)
  scrub(text)                                   `text` without the `user:password@` of any URL
  ask(port, method, path, token=None, body=None, timeout=ASK_TIMEOUT_S) -> (status, object)
  status_lines(health, now=None)                what `hub status` prints for a GET health answer
  ServeError, NotRunning

build() takes its parts from a Sources, so a test serves a temp store on port 0 with stand-ins
and never reads the machine config, a real repository or the network. Nothing here reads a
setting that could name an address: every listener is httpd.Listener on httpd.LOOPBACK.

Start-up, in this order; a failure releases everything taken before it and is a ServeError whose
text is for the owner's console (it may name a folder):
  1. the store (Store.open). A store another server holds is refused at once, never waited for.
     A lock left by a server that died is taken over, with a note.
  2. the token files (auth.ensure_tokens) and the approval key beside them
     (auth.ensure_approval_key): a file that cannot serve is refused, named.
  3. the backlog views and the git sync, from the Sources' factories. A sync that is refused
     (gitsync.Refused: the folder is no work tree, the wrong branch, ...) does not stop the
     server: the reason goes to `Hub.notes` for the console, the server serves with sync off,
     and GET health says so in a fixed phrase (the reason itself names folders).
  4. the Api over the store and a SiteViews, then the local listener on settings.port with
     httpd.local_hosts, httpd.local_gate, httpd.pair_routes over an auth.Credentials made for
     the port that was really bound, httpd.stop_route (it sets Hub.stopping and answers this
     process's pid and creation time; the tunnel listener never has it) and health_route. The
     Credentials keep the browser
     sessions in the token folder's sessions file for that port (auth, persist=True), so a
     paired browser stays paired when the server starts again; a file it cannot read whole is a
     line in `Hub.notes`. With settings.port 0 (a new port each run) no sessions file is kept.
  5. the tunnel listener on settings.tunnel_port (kitlib.hubsite.tunnel.start), only when
     settings.access can serve it. It never stops the server: with no Access settings at all
     there is no second listener and no line about it; with settings that cannot serve
     (tunnel.TunnelConfigError) or a port that cannot be bound, a line in `Hub.notes` says why
     and the local listener serves alone. So it does when tunnel.start raises anything else
     (the maker of the key source, say): the note then names the exception's class and none
     of its text, and the reason is TUNNEL_FAILED. `Hub.tunnel` is then TUNNEL_OFF with the
     reason in fixed words. With a listener bound, a note says which port needs an Access
     token and `Hub.tunnel` is what "GET health and the sync's reason" gives below.
`Hub.listeners` is a list: the local listener first, then the tunnel's when there is one.
start() and close() carry every listener in it, and a start-up that fails after one was bound
closes its door. Each gets a health_route of its own: the local listener's answers as it did
with no tunnel, the tunnel's (fixed=True) answers fixed words only, so the tunnel's owner is
told that a sync failed and not why. The tunnel listener has no local pairing routes, only
httpd.strong_pair_route (an Access login asks there for a code that pairs a local browser as
the strong owner; the code is redeemed on the local listener), and no Host rule, and its error answers are the agent's wording for the owner too (tunnel.start makes it
with httpd's agent_words), so none names the store folder; the local listener's rules are what
they were with no tunnel, and it never reads the Access header.

The log. `log(level, text)` gets the listeners' lines (httpd: 'access', 'error'), the tunnel
gate's ('note' or 'error' for a request it refused, at most one a reason a minute:
kitlib.hubsite.tunnel), the tunnel listener's 'error' tunnel.HOST_LINE for a write refused
because its Origin host is not its Host (httpd's origin_refused, tunnel.host_note: at most one
a minute) and the hub's own: 'error' for a close step that failed, ran out of
time or was cut short and for a git sync pass that failed, 'note' for what is no error (the
wait for git begins at the close; the sync works again). Every line of the hub's own goes
through scrub(), so none holds the user or password of a remote URL; the listeners' and the
gate's lines do not, so what prints them applies scrub().

The git sync's failures. GitSync has no log of its own: it keeps the step its last pass failed
at and git's reason (`detail`) in status(). Hub.watch() reads that status (it never waits for a
pass) and logs an 'error' line with the step and the reason when a failure is new or its reason
changed, and a 'note' when the sync works again; run() calls it on every turn of its wait
(POLL_S). A Hub served without run() logs a failing sync only when its owner calls watch().
A sync with push on and no remote to push to is failing from the start (gitsync.NO_REMOTE), so
the first look logs that.

The clean close, in this order (Hub.close; run() ends with it, also on Ctrl+C and on the
owner's stop route):
  1. every listener's gate that has a stop() (the tunnel's: tunnel.Gate.stop), then every
     listener's door. The gate first: a tunnel request held on a fetch of the Access keys
     (up to access.FETCH_DEADLINE_S, longer than a step's `timeout`) is refused at once with
     the tunnel's one answer, so step 3 does not wait for it; a stop admits nobody. Then no
     new connection, and no write begins (httpd refuses it);
  2. the backlog timer;
  3. the requests in flight, except the long waits: after this no write is in flight;
  4. the git sync, only when the server was started: a 'note' line says that the wait for git
     begins, the sync's thread is stopped, then one last pass (sync_once) runs while the store
     is still open and locked, so what was written since the last pass is committed whole. A
     pass that fails is an error line in the log and is tried again at the next start;
  5. the store: every long wait ends and the store lock is released;
  6. the handler threads.
Each step is given `timeout` seconds. One that raises, or that has not ended when its time is
up, is an 'error' line in the log that names the step; the next step still runs, and the store
is closed whatever happened before it. close() answers True only when every step ended in time
and none raised. Three things about step 4:
  - The wait for git has no limit of this module's. The last pass is not given `timeout`: it
    waits for a pass still in flight, then runs to its end, each as long as git's own limits
    let it (gitsync.NET_TIMEOUT for a fetch and again for a push, in each pass). A pass must
    end before the store closes, and a git command stopped half way may leave an `index.lock`
    behind, after which every pass ends at `busy` until a person removes it
    (kitlib.hubsite.gitsync). The 'note' line (LAST_PASS) is logged before the wait begins
    and says all of this. When the sync's thread has not ended in `timeout`, the wait for
    its pass in flight is made in the sync's stop(None), which begins no pass, and the last
    pass is called after it: no call of sync_once is left queued behind a pass in flight.
  - The last pass is skipped when step 3 ran out of time or raised. A request is then still in
    flight and may write while git adds the store's files (GitSync takes no store lock), so the
    commit could hold half of that write with no later pass in this process to bring the rest.
    An 'error' line says so (LAST_PASS_SKIPPED); the first pass of the next start commits what
    is left. The sync's thread is still stopped, after a 'note' (IN_FLIGHT). When it has not
    ended in `timeout`, a pass is in flight: close() says so (a 'note', STILL_WAITING) and
    waits for it with no limit, so the store is not closed beside a running git. Only when
    that wait itself fails is the store closed with a pass that may still run, and an 'error'
    line (LEFT_RUNNING) says what to check.
  - A second Ctrl+C ends the wait. Every wait of step 4 is made on a thread of its own
    (GIT_WAIT_THREAD) while close() waits for that thread in turns of POLL_S, so a
    KeyboardInterrupt reaches a close() that runs on the main thread. For that one, and for
    one in steps 1 to 3 of a server whose sync was started, an 'error' line (CUT_SHORT) says
    that a git command may have been left half way and what to check; the sync is told to
    begin no further pass, steps 5 and 6 still run, and close() answers False. No pass
    begins after that: the wait that was left is one in the sync's stop(), or a last pass
    that had begun already, and that one, like a pass in flight, is left to itself. (A sync
    whose stop(None) raises or does not answer True gets no last pass either: the raise is
    an 'error' line, then LEFT_RUNNING. GitSync's stop(None) answers True.)
    With no sync started, a KeyboardInterrupt leaves close() as it came, after steps 5 and
    6. So does one that arrives in step 5 or 6 itself, sync or none: step 4 has ended, so
    there is no CUT_SHORT line, and what is left of those two steps is not run (the system
    drops the store lock with the process when step 5 was the one cut).
A process that is killed closes nothing: the system drops the store lock with it, and the next
start takes it over.

Views (SiteViews). backlog, completed, repos and agents exist only where their source does, so
the Api answers a missing one `unavailable` by itself. What a view says never holds a path,
git's words or an exception's text: the backlog views carry kitlib.hubsite.backlog's fixed
phrases, and the reasons of the derived views are replaced by fixed phrases here.
  backlog(repo, state, fresh), completed(repo, days)
      [{repo, fetched_at, items, error, stale, fetch_error}]; completed adds `days`. No entry
      names the commit it was read at (backlog.Views does not give it). `days` narrows the
      window backlog.Views was built with and cannot widen it.
      Called with no argument (GET snapshot's call: api.snapshot; the backlog and completed
      routes pass every argument) while the backlog timer runs (backlog.Views.timer_running),
      both answer from what the timer's reads left (backlog.Views.held), however old, and start
      no git: the page's one load never waits on a repository's read. A repository the first
      round has not come to yet is an entry with "error" backlog.NOT_READ; with none read yet
      both raise NotReadYet, which the snapshot shows in `unavailable`. With no timer running
      (backlog_refresh_s unset, or the timer not started or ended) they read as the routes do.
  notices() repos() usage() agents()      kitlib.hubsite.derived's answers
  health()    {git, backlog, backlog_at, backlog_error, backlog_timer}. `git` is GitSync.status()
      without `detail`, or {"running": false, "off": phrase}. `backlog` is null with no backlog
      source, else [{repo, fetched_at, error}] as the last backlog read gave it (`backlog_at`:
      when; null and an empty list before the first read): health starts no git. A read is a
      client's GET of the backlog or completed view (a snapshot's held answer is not one), or the
      end of a round of the backlog
      timer: build() wraps the `wait` of a backlog.Views (its pause between two rounds) so
      SiteViews.after_round() runs on the timer's own thread after each round. That reads the
      view as a client would, mostly from what the round just read; with no timer
      (backlog_refresh_s unset) health knows nothing of the backlog until a client reads it.
      That read is given the timer's stop event (backlog.Views.get(stop=)): once the timer is
      told to stop it reads no further repository, and health says what it said before. A
      read of after_round() that raises (a backlog source whose `get` takes no `stop`, say) is
      one 'error' line in the log (ROUND_FAILED), once for each failure that is new.

GET health and the sync's reason (health_route). The Api's health carries no `detail` for
anyone. The route adds `git.detail`, the reason of a failing sync with scrub() applied, for the
owner role only: an agent and a caller with no role get the Api's answer as it is. `detail` may
name a remote URL or a folder of this machine, which is why it is the owner's alone.
The route also adds `tunnel` to every 200 answer, so for the owner and the agent alike and on
both listeners; `hub status` prints it (status_lines) and `Hub.tunnel` is the local listener's:
  TUNNEL_OFF with why the tunnel listener did not start, in kitlib.hubsite.tunnel's fixed
      phrases, TUNNEL_BIND or TUNNEL_FAILED;
  TUNNEL_ON when the listener is bound and its gate has the team's keys to check a token with
      (tunnel.Gate.keys: a good fetch no older than access.STALE_LIMIT_S);
  TUNNEL_BOUND when it is bound and has no such keys. On the local listener, and there only,
      TUNNEL_BOUND_WHY names the reason the last fetch failed, one word of access.REASONS
      (`keys-fetch`, ...), never an exception's text.
It never holds a value of the settings (no team name, audience tag or email) and never the
system's words for a failed bind: those are in the console note. Health asks the gate what it
holds: it starts no key fetch and waits for none, and the gate fetches keys only when a request
brings a token. So a server just started says TUNNEL_BOUND until the first request with a token
made a good fetch, and `on` does not say that a tunnel program reaches the listener. A key
source with no ready() never says it has keys (tunnel.Gate.keys), so it is TUNNEL_BOUND too.
After Hub.close() was called, `Hub.tunnel` of a hub that had a tunnel listener is TUNNEL_CLOSED,
not TUNNEL_BOUND: the gate is stopped and its keys are no longer asked for. Only `Hub.tunnel`
says that word; no GET health answer does (a request the close finds in flight on the local
listener is answered from the stopped gate, so with TUNNEL_BOUND).

GET health through the tunnel (health_route with fixed=True, _fixed_health). The tunnel
listener's answer holds fixed words and no other value: {ok: true, version, store: WORD_OK,
git: WORD_OK | WORD_FAILED | WORD_OFF | WORD_UNKNOWN, tunnel: TUNNEL_ON | TUNNEL_BOUND}. No
store folder, no git branch, step or reason, no backlog, no revision. `store` is WORD_OK
whenever the Api answered at all; `git` is WORD_OFF with no sync, WORD_FAILED when its last
pass failed, WORD_UNKNOWN when the Api gave no git status. An answer of the Api that is no 200
leaves as its status and error code with the message HEALTH_REFUSED, never the Api's own
message.

ask() is the one request every local client makes (`hub status`, `hub open`, and
kitlib.hubsite.client for the other verbs): to the loopback listener through http.client, which
uses no proxy, so a token never leaves this machine. It names itself USER_AGENT.
"""
import datetime
import functools
import http.client
import json
import os
import re
import threading

from kitlib import hubbacklog, pmdata, pmstart, pmwatch
from kitlib.config import user
from kitlib.config.types import ConfigError
from kitlib.proc import process

from . import api as _api
from . import access, auth, backlog, derived, gitsync, httpd, recordwrite, tunnel
from .store import HubError, Store, StoreBusy

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
CLOSE_TIMEOUT_S = 10            # each step of the clean close
POLL_S = 0.5                    # run()'s wait between two looks at `stop` (and at Ctrl+C)
ASK_TIMEOUT_S = 10
MAX_ANSWER = 16 << 20           # bytes of one answer ask() reads
VIA_CLI = 'cli'
VIA_MCP = 'mcp'                 # the hub MCP server's writes (kitlib.hubmcp, docs/hub-mcp.md)
USER_AGENT = 'hub-client'       # what ask() calls itself: it names no person and no machine
SYNC_OFF = 'git sync is off on this server'
SYNC_REFUSED = 'git sync was refused at start-up: the server console says why'
TUNNEL_ON = 'on'                # GET health's `tunnel`, and `Hub.tunnel`
TUNNEL_BOUND = 'bound, keys not fetched'
TUNNEL_BOUND_WHY = 'bound, keys not fetched (%s)'   # the local listener's: one of access.REASONS
TUNNEL_OFF = 'off: %s'
TUNNEL_CLOSED = 'closed'        # `Hub.tunnel` once close() was called; no GET health says it
TUNNEL_BIND = 'the tunnel port could not be bound: the server console says why'
TUNNEL_FAILED = 'the tunnel listener could not be made: the server console says why'
TUNNEL_OFF_NOTE = 'the tunnel listener is off: %s'
TUNNEL_FAILED_NOTE = 'it could not be made (%s)'    # the exception's class name, never its text
# GET health through the tunnel listener: these words and no other value
WORD_OK, WORD_FAILED, WORD_OFF, WORD_UNKNOWN = 'ok', 'failed', 'off', 'unknown'
HEALTH_REFUSED = 'health could not be answered'
TUNNEL_ON_NOTE = ('the tunnel listener is on %s: every request there needs a Cloudflare Access '
                  'token')
NO_USAGE_SOURCE = 'no launcher usage source configured'     # derived.usage's own phrase
USAGE_FAILED = 'the usage source could not be read'
AGENTS_FAILED = 'the agent definitions could not be read'
ROUND_FAILED = ('the backlog view could not be read after a round of the backlog timer, so '
                'health does not follow the timer: %s: %s')
STOP_ASKED = 'closing: the owner asked the server to stop (hub.py stop)'
GIT_WAIT_THREAD = 'hub-close-git'       # the thread a wait for git is made on at the close
_GIT_LIMITS = ('git may take %d s for a fetch and again for a push' % gitsync.NET_TIMEOUT)
_AGAIN = 'Ctrl+C again ends the wait'
LAST_PASS = ('closing: the last git sync pass: finishing a sync pass, waiting for git. A pass in '
             'flight ends first, then the last one runs; each runs to its end, and in each %s. %s.'
             % (_GIT_LIMITS, _AGAIN))
LAST_PASS_SKIPPED = ('closing: the last git sync pass is skipped: a request was still in flight '
                     'and could write during it; the next start commits what is left')
IN_FLIGHT = ('closing: finishing a sync pass that may be in flight, waiting for git: it has to '
             'end before the store closes (%s). %s.' % (_GIT_LIMITS, _AGAIN))
STILL_WAITING = ('closing: still waiting for git: the sync pass in flight has to end before the '
                 'store closes. %s.' % _AGAIN)
_CHECK = ('Look for a leftover index.lock in the git directory of the store\'s work tree (`git '
          'rev-parse --git-dir` in the store folder names it): while it is there every sync '
          'pass ends at `busy`. Remove it once no git runs there; the next start commits what '
          'is left.')
CUT_SHORT = ('closing: interrupted before the git sync had ended: a git command may have been '
             'left half-way. ' + _CHECK)
LEFT_RUNNING = ('closing: the store is closed while a git sync pass may still be running: a git '
                'command may be left half-way. ' + _CHECK)
# the user and password part of a URL: everything between `://` and the last `@` of its host
# part. A quote does not end it (a password may hold `'`); a space, `"`, `<` and `>` do. So in
# `'https://h','x@y'` (a URL with no user part, then other text with an `@` and no space or
# `/` before it) what stands between is lost: too much is taken out, never a password left
# in. No rule here tells that text from a password that holds `','`.
_USERINFO = re.compile(r'(?<=://)[^/?#\s"<>]*@')


def scrub(text):
    """`text` without the `user:password@` part of any URL in it. git's own words may quote a
    remote URL, and a remote URL may carry a token: nothing shown or logged here keeps one."""
    return _USERINFO.sub('', str(text))


def _failure(sync):
    """(the step the last pass of the git sync `sync` failed at, git's reason with scrub()
    applied); ('', '') when it is not failing. Never waits for a pass."""
    status = sync.status()
    failed = status.get('failed') or ''
    if not failed:
        return '', ''
    return str(failed), scrub(status.get('detail') or '')


class ServeError(Exception):
    """The server cannot start, or a local request to it failed. The text is for the owner's
    console and may name a folder."""


class NotRunning(ServeError):
    """Nothing listens on the port: the hub server is not running there."""

    def __init__(self, port):
        ServeError.__init__(self, 'no hub server is listening on port %d' % port)
        self.port = port


class NoRepoList(Exception):
    """The repository list could not be read and no earlier answer is held. The Api shows the
    class name only."""


class NotReadYet(Exception):
    """GET snapshot's backlog or completed view while the backlog timer runs and no read has
    answered yet (backlog.NOT_READ): its first round is still in git. The Api shows the class
    name only."""

    def __init__(self):
        Exception.__init__(self, backlog.NOT_READ)


class Sources(object):
    """Where the server's parts come from. A part that is None is a view the server does not
    have (or, for `sync`, a server that commits nothing).

      tokens       the credential folder (auth.folder)
      static_dir   the page's folder; a missing one answers 404
      pm_data      kitlib.pmdata's {path, branch, remote}, or None
      notices      the machine lock's notice folder
      repos        callable(the store's repos documents) -> [{name, aliases}]
      known_repos  callable() -> the PM's configured repo names, which the store's repo-alias
                   check accepts without a repos/ document; None: none
      usage        callable() -> the lines kitlib.launcher.usage prints
      agents       the folder of agent definitions
      backlog      callable(refresh_s) -> a kitlib.hubsite.backlog.Views (or its like: get, start,
                   stop, days, clock, timer_error; a callable `wait`, when it has one, is
                   wrapped by build(), and its get() is then also called with `stop=`, the
                   timer's event: module docstring, "Views")
      sync         callable(store_dir, commit_every_s, remote=, branch=, push=) -> a
                   kitlib.hubsite.gitsync.GitSync (or its like: start, stop, sync_once, status)
      access_keys  callable(team) -> the tunnel gate's key source (get(kid)); None:
                   kitlib.hubsite.access.KeyCache, which asks Cloudflare for the team's keys
                   when a token first needs one. Called only when the tunnel listener starts.
                   What it raises turns the tunnel off (TUNNEL_FAILED). A key source may also
                   have ready() (are there keys now: health's `tunnel`; one without it never
                   counts as having them, so health says TUNNEL_BOUND while its tokens are
                   checked all the same), `error` and stop() (called first at the close)
      access_now   callable() -> the Unix time the tunnel gate checks a token's times against;
                   None: time.time
    """

    def __init__(self, tokens=None, static_dir=STATIC_DIR, pm_data=None, notices=None, repos=None,
                 usage=None, agents=None, backlog=None, sync=None, access_keys=None,
                 access_now=None, known_repos=None):
        self.known_repos = known_repos
        self.tokens = tokens
        self.static_dir = static_dir
        self.pm_data = pm_data
        self.notices = notices
        self.repos = repos
        self.usage = usage
        self.agents = agents
        self.backlog = backlog
        self.sync = sync
        self.access_keys = access_keys
        self.access_now = access_now


def _repo_pairs(cfg):
    """A callable giving backlog.Views its (name, root) pairs from the pm_start config `cfg`,
    each root as kitlib.hubbacklog.repo_root finds it. The PM repository's root is asked of git once."""
    found = []

    def pairs():
        if not found:
            top = hubbacklog.git(os.path.dirname(os.path.abspath(cfg['sessions_file'])),
                                 'rev-parse', '--show-toplevel')
            found.append(top.decode('utf-8').strip())
        return backlog.repo_list(pmstart.known_repos(cfg), found[0], cfg.get('repo_paths'))
    return pairs


def agents_dir(environ=None):
    """The rendered agents folder of the host this server runs for (PM_KIT_HOST, else the
    registry's default: kitlib.host.registry.selected): <kit>/<its shell>/agents. None when
    hosts.json cannot name one, so the agents view is simply not there."""
    from kitlib.host import registry
    try:
        shell = registry.host(registry.selected(environ)).shell
    except registry.RegistryError:
        return None
    return os.path.join(pmstart.KIT_ROOT, os.path.normpath(shell), 'agents')


def _configured_repos(cfg):
    """A callable giving pmstart.known_repos of the pm_start config `cfg`, the store's repo-alias
    check's names. As in _repo_pairs, git is asked once: each folder's git top level (the PM's
    repository and the kit's, two git runs) is kept, while sessions_file is read on each call, so
    a new coordinator's repository counts at once, as on the repo menu (derived.repos). () when
    the names cannot be read, since a store write must never fail on them: KeyError no
    sessions_file, ValueError bad "repos" or bad JSON, OSError an unreadable sessions file,
    AttributeError a sessions file whose JSON is not an object, TypeError a sessions_file that is
    not a path. No git is not an error: pmstart._toplevel gives None and the name is left out.
    Anything else is a bug in this code and is left to raise."""
    tops = {}

    def toplevel(folder):
        if folder not in tops:
            tops[folder] = pmstart._toplevel(folder)
        return tops[folder]

    def names():
        try:
            return pmstart.known_repos(cfg, toplevel=toplevel)
        except (KeyError, ValueError, OSError, AttributeError, TypeError):
            return ()
    return names


def machine_sources(home=None, sync=True):
    """This machine's Sources, read from the machine config under `home` (default the user's).
    It writes nothing and starts no git. sync=False: no git sync (a scratch store). ConfigError
    when the machine config cannot be used."""
    path = user.readable_file(home)
    cfg = pmstart.load_config(path)
    try:
        pm = pmdata.machine_settings(home)
    except pmdata.PmDataError as e:
        raise ConfigError(str(e))
    src = Sources(tokens=auth.folder(home), pm_data=pm,
                  notices=pmwatch.configured_notice_dir(path),
                  agents=agents_dir(),
                  sync=gitsync.GitSync if sync else None)
    if pmwatch.launcher_configured(home):
        def report():
            from kitlib.launcher import settings as launcher_settings   # here: only this needs it
            from kitlib.launcher import usage as launcher_usage
            return launcher_usage.report(launcher_settings.load(home))
        src.usage = report
    if cfg and cfg.get('sessions_file'):
        src.repos = lambda stored: derived.repos(cfg, stored)
        src.known_repos = _configured_repos(cfg)
        src.backlog = lambda refresh_s: backlog.Views(_repo_pairs(cfg), refresh_s)
    return src


def _named(entries, repo):
    """The entries of the repository `repo`, compared without regard to case ('' : all)."""
    if not repo:
        return list(entries)
    want = repo.lower()
    return [e for e in entries if str(e.get('repo') or '').lower() == want]


def _entry(e, items):
    return {'repo': e.get('repo'), 'fetched_at': e.get('fetched'), 'items': items,
            'error': e.get('error'), 'stale': bool(e.get('stale')),
            'fetch_error': e.get('fetch_error')}


def _record_call(fn, *args):
    """fn(*args), a record view of kitlib.hubsite.backlog, with what it raises put as the Api
    answers it: a RecordError as its own code (not_found, invalid) and fixed phrase, a ReadError
    as `unavailable` with its fixed phrase (backlog.say). Neither names a path or what was asked."""
    try:
        return fn(*args)
    except backlog.RecordError as e:
        raise HubError(e.code, e.said, e.field)
    except backlog.ReadError as e:
        raise HubError('unavailable', backlog.say(e), 'views')


class SiteViews(object):
    """The Api's `views` (module docstring, "Views"). A view is bound only where its source
    exists: `backlog_views` for backlog, completed, record, record_template and the record
    writes (record_add, record_edit, record_delete), sources.repos and .agents for theirs.
    notices, usage and health are always there (kitlib.hubsite.derived answers a missing notice
    folder with no notices and a missing usage source with its own phrase). `sync` is the git
    sync or None, `sync_off` the fixed phrase health gives when there is none. `log(level,
    text)`: where after_round says that a read of its own failed."""

    def __init__(self, store, sources, backlog_views=None, sync=None, sync_off=SYNC_OFF,
                 log=None):
        self._log = log
        self._round_said = ''       # the failure after_round last logged
        self._store = store
        self._sources = sources
        self._backlog = backlog_views
        self._sync = sync
        self._sync_off = sync_off
        self._last = None           # what the last backlog answer said, for health
        self.notices = self._notices
        self.usage = self._usage
        if backlog_views is not None:
            self.backlog = self._open
            self.completed = self._completed
            self.record = self._record
            self.record_template = self._record_template
            self.record_add = self._record_add
            self.record_edit = self._record_edit
            self.record_delete = self._record_delete
        if sources.repos is not None:
            self.repos = self._repos
        if sources.agents:
            self.agents = self._agents

    def _held(self):
        """The backlog source's held answer (backlog.Views.held) while its timer runs, or None
        to read as before: no timer running (none configured, not started, or ended) or a
        source with no held answer (a stand-in). Raises NotReadYet when the timer runs and no
        read has answered yet."""
        running = getattr(self._backlog, 'timer_running', None)
        held = getattr(self._backlog, 'held', None)
        if not callable(running) or not callable(held) or not running():
            return None
        got = held()
        if got is None:
            raise NotReadYet()
        return got

    def _read(self, fresh=False, stop=None, held=False):
        """The backlog source's entries, kept for health. With `stop` (an Event; the timer's
        own read gives its) the source may answer None, a read it cut short: None then, and
        health keeps what it had. `held` (GET snapshot's): while the timer runs, what its reads
        left, with no git started and health left as it was (_held); with no timer running, a
        read as without it."""
        got = self._held() if held else None
        if got is not None:
            return got.get('repos') or []
        if stop is None:
            got = self._backlog.get(fresh=bool(fresh))
        else:
            got = self._backlog.get(fresh=bool(fresh), stop=stop)
            if got is None:
                return None
        entries = got.get('repos') or []
        # one assignment: health reads it from another thread and takes no lock
        self._last = {'at': got.get('at'), 'error': got.get('error'),
                      'repos': [{'repo': e.get('repo'), 'fetched_at': e.get('fetched'),
                                 'error': e.get('fetch_error') or e.get('error')}
                                for e in entries]}
        if got.get('error') and not entries:
            raise NoRepoList()
        return entries

    def _open(self, repo=None, state=None, fresh=None):
        # no argument at all is GET snapshot's call (api.snapshot); every route passes all three
        held = repo is None and state is None and fresh is None
        out = []
        for e in _named(self._read(fresh, held=held), repo or ''):
            items = e.get('open') or []
            if state:
                items = [i for i in items if i.get('s') == state]
            out.append(_entry(e, items))
        return out

    def _completed(self, repo=None, days=None):
        # no `repo` is GET snapshot's call (api.snapshot); GET completed always passes one
        entries = self._read(held=repo is None)
        limit = self._backlog.days
        since = None
        if days is not None and days < limit:
            since = backlog.window(self._backlog.clock(), days)
        out = []
        for e in _named(entries, repo):
            items = e.get('completed') or []
            if since is not None:
                items = [i for i in items if str(i.get('d') or '')[:10] >= since]
            one = _entry(e, items)
            one['days'] = limit if since is None else days
            out.append(one)
        return out

    def _record(self, repo, id):
        """One record, read now (kitlib.hubsite.backlog Views.record), its failures as HubErrors."""
        return _record_call(self._backlog.record, repo, id)

    def _record_template(self, repo):
        return _record_call(self._backlog.record_template, repo)

    # The owner's record writes (kitlib.hubsite.recordwrite) to the store of the configured
    # repository `repo` names; the root never comes from the caller. After a write the backlog
    # source reads that repository again (Views.reread), so the snapshot the page reads shows
    # it; add and edit also answer the record as read after the write.
    def _record_root(self, repo, rid):
        _record_call(backlog.check_id, rid)
        # Views._named, the record reads' own lookup: its one caller outside backlog.Views
        return _record_call(self._backlog._named, repo)

    def _record_add(self, repo, id, summary, scopes, effort, ref):
        name, root = self._record_root(repo, id)
        out = recordwrite.add(root, id, summary, scopes, effort, ref)
        self._backlog.reread(name, root)
        return dict(out, record=self._record(name, id))

    def _record_edit(self, repo, id, version, changes, unset, body, ruling):
        name, root = self._record_root(repo, id)
        out = recordwrite.edit(root, id, version, changes, unset, body, ruling)
        self._backlog.reread(name, root)
        return dict(out, record=self._record(name, id))

    def _record_delete(self, repo, id, why, ruling):
        name, root = self._record_root(repo, id)
        out = recordwrite.delete(root, id, why, ruling)
        self._backlog.reread(name, root)
        return out

    def _notices(self):
        return derived.notices(self._sources.notices)

    def _repos(self):
        return self._sources.repos(list(self._store.docs('repos').values()))

    def _usage(self):
        out = derived.usage(self._sources.usage, self._store.clock)
        if not out.get('available') and out.get('reason') != NO_USAGE_SOURCE:
            out['reason'] = USAGE_FAILED
        return out

    def _agents(self):
        out = derived.agents(self._sources.agents)
        if not out.get('available'):
            out['reason'] = AGENTS_FAILED
        return out

    def _git(self):
        if self._sync is None:
            return {'running': False, 'off': self._sync_off}
        out = dict(self._sync.status())
        out.pop('detail', None)     # git's own words: they may name a remote URL or a path
        return out

    def git_failure(self):
        """(the step the git sync's last pass failed at, git's reason with scrub() applied), or
        ('', '') with no sync or one that is not failing. The reason may name a remote or a
        folder: it is for the owner and the server log, never for an agent."""
        return ('', '') if self._sync is None else _failure(self._sync)

    def after_round(self, stop=None):
        """Read the backlog view as a client's read does, so health carries what the timer's
        round just read. `stop`: the timer's event; once it is set no further repository is
        read (backlog.Views.get) and health keeps what it had. Never raises: it runs on the
        timer's thread (build). A read that raised leaves health saying what it said before,
        and is one 'error' line in the log (ROUND_FAILED): one for a failure that is new, not
        one for every round. A repository list that cannot be read is no such failure: health
        carries it (backlog_error)."""
        if self._backlog is None:
            return
        try:
            self._read(stop=stop)
            said = ''
        except NoRepoList:
            said = ''
        except Exception as e:      # noqa: BLE001 - the timer's thread goes on
            said = scrub(ROUND_FAILED % (type(e).__name__, e))
        was, self._round_said = self._round_said, said
        if said and said != was and self._log is not None:
            try:
                self._log('error', said)
            except Exception:       # noqa: BLE001 - a log that fails must not end the timer
                pass

    def health(self):
        out = {'git': self._git(), 'backlog': None, 'backlog_at': None, 'backlog_error': None,
               'backlog_timer': None}
        if self._backlog is not None:
            last = self._last
            out['backlog'] = [dict(r) for r in last['repos']] if last else []
            out['backlog_at'] = last['at'] if last else None
            out['backlog_error'] = last['error'] if last else None
            out['backlog_timer'] = getattr(self._backlog, 'timer_error', None)
        return out


def _under(path, folder):
    """True when `path` is `folder` or below it."""
    a, b = (os.path.normcase(os.path.abspath(p)) for p in (path, folder))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:              # two drives
        return False


def _open_store(root):
    try:
        return Store.open(root)
    except StoreBusy as e:
        raise ServeError('%s: another hub server has this store open; stop that one first' % e)
    except HubError as e:
        raise ServeError(str(e))
    except OSError as e:
        raise ServeError('cannot open the hub store %s: %s' % (root, e))


def _make_sync(settings, sources, notes):
    """(the git sync or None, the phrase health gives when there is none)."""
    if sources.sync is None:
        return None, SYNC_OFF
    pm = sources.pm_data
    if not (pm and pm.get('path') and _under(settings.store, pm['path'])):
        pm = None                   # a store outside the data worktree: its own branch, no remote
    try:
        sync = sources.sync(settings.store, settings.commit_every_s,
                            remote=pm['remote'] if pm else None,
                            branch=pm['branch'] if pm else None, push=settings.push)
    except (gitsync.Refused, ValueError, OSError) as e:
        notes.append(scrub('git sync is off: %s' % e))
        return None, SYNC_REFUSED
    return sync, ''


def _make_tunnel(settings, sources, api, notes, log):
    """(the tunnel listener, None) or (None, what health says of there being none: TUNNEL_OFF
    with a fixed reason): module docstring, "Start-up", step 5. The listener is bound and not
    started, with no route yet. The note of a port that could not be bound carries the system's
    reason; health carries TUNNEL_BIND instead. Whatever else tunnel.start raises is no reason
    to refuse the local listener: the note names the exception's class and nothing of its text
    (a key source's maker may put a setting in it), and health carries TUNNEL_FAILED."""
    try:
        # through the module, never a name bound at import: a test replaces tunnel.start
        lis = tunnel.start(settings, api, static_dir=sources.static_dir, log=log,
                           keys=sources.access_keys, now=sources.access_now)
    except tunnel.TunnelConfigError as e:
        if not e.unset:
            notes.append(TUNNEL_OFF_NOTE % e)
        return None, TUNNEL_OFF % e
    except httpd.BindRefused as e:
        notes.append(scrub(TUNNEL_OFF_NOTE % e))
        return None, TUNNEL_OFF % TUNNEL_BIND
    except Exception as e:          # noqa: BLE001 - the local listener serves alone
        notes.append(TUNNEL_OFF_NOTE % (TUNNEL_FAILED_NOTE % type(e).__name__))
        return None, TUNNEL_OFF % TUNNEL_FAILED
    notes.append(TUNNEL_ON_NOTE % lis.url)
    return lis, None


def _tunnel_state(listener, why=False):
    """What health says of the bound tunnel `listener`: TUNNEL_ON when its gate has the keys to
    check a token with (tunnel.Gate.keys: it asks the key source and starts no fetch, waits for
    none), else TUNNEL_BOUND. With `why`, and a reason that is one of access.REASONS, it is
    TUNNEL_BOUND_WHY with that word. A gate that cannot say, or that raises, has no keys."""
    try:
        fetched, reason = listener.gate.keys()
    except Exception:               # noqa: BLE001 - health still answers
        fetched, reason = False, ''
    if fetched is True:
        return TUNNEL_ON
    if why and isinstance(reason, str) and reason in access.REASONS:
        return TUNNEL_BOUND_WHY % reason
    return TUNNEL_BOUND


def _fixed_health(status, obj, tunnel):
    """GET health as the tunnel listener answers it, from the Api's (status, obj): fixed words
    and nothing else (module docstring, "GET health through the tunnel")."""
    if status != 200 or not isinstance(obj, dict):
        error = obj.get('error') if isinstance(obj, dict) else None
        code = error.get('code') if isinstance(error, dict) else None
        if status == 200:           # a 200 that is no object is no answer at all
            status, code = 500, 'internal'
        elif not (isinstance(code, str) and code in _api.STATUS):
            code = 'internal'
        return httpd.refusal(status, code, HEALTH_REFUSED)
    git = obj.get('git')
    if not isinstance(git, dict):
        word = WORD_UNKNOWN
    elif git.get('off'):
        word = WORD_OFF
    else:
        word = WORD_FAILED if git.get('failed') else WORD_OK
    said = {'ok': True, 'version': _api.API_VERSION, 'store': WORD_OK, 'git': word}
    if tunnel:
        said['tunnel'] = tunnel
    return httpd.json_response(200, said)


def health_route(api, views, tunnel='', fixed=False):
    """The route a listener answers GET health with (module docstring, "GET health and the
    sync's reason"): the answer of `api` (an Api), and for the owner role `git.detail` from
    `views` (a SiteViews) when the sync is failing at the step the answer names. `tunnel` is
    added to every 200 answer when given: a text (TUNNEL_OFF with a fixed reason), or a callable
    giving one, asked at each request (_tunnel_state). With `fixed`, the tunnel listener's
    route, the answer is _fixed_health's instead: the Api is asked all the same, and nothing of
    its answer but a fixed word leaves."""
    def route(request):
        # raise_unexpected: as the listener's own Api call, so an error is logged there; and
        # strong, as that call, only for the request its gate marked (httpd.strong_words)
        status, obj = api.call(request.method, request.path, request.role, request.body,
                               request.query, request.via, raise_unexpected=True,
                               **httpd.strong_words(request))
        said = tunnel() if callable(tunnel) else tunnel
        if fixed:
            return _fixed_health(status, obj, said)
        git = obj.get('git') if status == 200 and request.role == _api.OWNER else None
        if isinstance(git, dict):
            failed, detail = views.git_failure()
            if detail and git.get('failed') == failed:
                git['detail'] = detail
        if status == 200 and said and isinstance(obj, dict):
            obj['tunnel'] = said
        return httpd.json_response(status, obj)
    return route


def _follow_rounds(backlog_views, views):
    """Make each round of the backlog timer reach health: wrap `backlog_views.wait`, the pause
    the timer takes after a round on its own thread, so views.after_round(stop) runs first. Not
    when the timer is stopping (and a timer told to stop during that read ends it after the
    repository it is on), and not for a stand-in with no `wait`."""
    wait = getattr(backlog_views, 'wait', None)
    if not callable(wait):
        return

    def pause(stop, seconds):
        if not stop.is_set():
            views.after_round(stop)
        return wait(stop, seconds)

    backlog_views.wait = pause


def build(settings, sources, log=None):
    """The Hub for `settings` (kitlib.hubsite.settings.Settings) from `sources` (a Sources):
    module docstring, "Start-up". Nothing is started; close() the Hub to release the store.
    `log(level, text)` gets the listeners' lines (httpd) and the hub's own (module docstring,
    "The log"). ServeError."""
    if not settings.store:
        raise ServeError('no store folder: set hub.store or pm_data in the machine config, '
                         'or name one')
    if not sources.tokens:
        raise ServeError('no credential folder')
    notes = []
    store = _open_store(settings.store)
    if sources.known_repos is not None:
        store.known_repos = sources.known_repos
    listeners = []
    try:
        if store.took_over:
            notes.append('the store lock was left by a server that did not close: taken over')
        try:
            auth.ensure_tokens(sources.tokens)
            auth.ensure_approval_key(sources.tokens)
        except auth.TokenFileError as e:
            raise ServeError(str(e))
        except OSError as e:
            raise ServeError('cannot write the token files in %s: %s' % (sources.tokens, e))
        views_b = None
        if sources.backlog is not None:
            views_b = sources.backlog(settings.backlog_refresh_s)
        sync, sync_off = _make_sync(settings, sources, notes)
        views = SiteViews(store, sources, views_b, sync, sync_off, log=log)
        if views_b is not None:
            _follow_rounds(views_b, views)
        api = _api.Api(store, views)
        try:
            local = httpd.Listener((httpd.LOOPBACK, settings.port), api, None,
                                   static_dir=sources.static_dir, host_rule=httpd.local_hosts,
                                   log=log)
        except httpd.BindRefused as e:
            raise ServeError('%s: is a hub server running already?' % e)
        listeners.append(local)
        # port 0 binds a new port each run: a sessions file per run would never be read again
        creds = auth.Credentials(sources.tokens, port=local.port, persist=settings.port != 0,
                                 log=log)
        notes.extend(creds.notes)
        local.gate = httpd.local_gate(creds)
        local.routes.update(httpd.pair_routes(creds))
        # the local listener's alone: the tunnel listener is never given a stop route
        stopping = threading.Event()
        local.routes.update(httpd.stop_route(creds, stopping.set,
                                             {'pid': os.getpid(), 'ctime': process.own_ctime()}))
        tunnelled, said = _make_tunnel(settings, sources, api, notes, log)
        health = ('GET', _api.BASE + '/health')
        if tunnelled is not None:
            listeners.append(tunnelled)     # at once: a failure below closes its door too
            # the tunnel listener's alone: only an Access login may ask for a strong code
            tunnelled.routes.update(httpd.strong_pair_route(creds))
            # the local listener's health and Hub.tunnel say why there are no keys; the
            # tunnel's own health never does
            said = functools.partial(_tunnel_state, tunnelled, True)
            tunnelled.routes[health] = health_route(
                api, views, functools.partial(_tunnel_state, tunnelled), fixed=True)
        local.routes[health] = health_route(api, views, said)
        return Hub(settings, store, api, views, creds, listeners, sync, views_b, notes, log,
                   tunnel=said, stopping=stopping)
    except BaseException:
        for lis in listeners:
            lis.close_door()
        store.close()
        raise


class Hub(object):
    """One built server (build): its parts, start() and close(). `notes` are start-up lines for
    the console; `port` and `url` are the local listener's; `tunnel` is what the local listener's
    GET health says of the tunnel listener at this moment (TUNNEL_ON, TUNNEL_BOUND or
    TUNNEL_BOUND_WHY, or TUNNEL_OFF with the reason; '' for a Hub made without build), and
    TUNNEL_CLOSED from the moment close() was called when there was a tunnel listener (with
    none it stays what it was). The `tunnel` given here is that text or a callable giving it.
    `stopping` is the event the local listener's stop route sets (httpd.stop_route); run()
    closes the hub once it is set. A Hub made without one gets an event nothing sets."""

    def __init__(self, settings, store, api, views, creds, listeners, sync, backlog_views, notes,
                 log=None, tunnel='', stopping=None):
        self.settings = settings
        self.store = store
        self.api = api
        self.views = views
        self.creds = creds
        self.listeners = listeners
        self.sync = sync
        self.backlog = backlog_views
        self.notes = notes
        self._tunnel = tunnel
        self.stopping = stopping if stopping is not None else threading.Event()
        self._log = log
        self._life = threading.Lock()       # start() and close()
        self._started = False
        self._closed = False
        self._clean = None
        self._seen = ('', '')               # the sync failure watch() last logged

    @property
    def port(self):
        return self.listeners[0].port

    @property
    def url(self):
        return self.listeners[0].url

    @property
    def tunnel(self):
        if not callable(self._tunnel):
            return self._tunnel         # no tunnel listener: why, and the same after the close
        # read with no lock: one flag that close() sets once, before it stops the gate
        return TUNNEL_CLOSED if self._closed else self._tunnel()

    def _say(self, text, level='error'):
        """One line of the hub's own to the log, with scrub() applied."""
        if self._log is None:
            return
        try:
            self._log(level, scrub(text))
        except Exception:           # noqa: BLE001 - a log that fails must not stop the close
            pass

    def watch(self):
        """Look at the git sync once and log what is new (module docstring, "The git sync's
        failures"): an 'error' line when its last pass failed at another step or for another
        reason than the last look saw, a 'note' when it works again. Never waits for a pass
        and never raises. One caller at a time: run() is it."""
        if self.sync is None:
            return
        try:
            seen = _failure(self.sync)
        except Exception:           # noqa: BLE001 - a look that fails is tried again next turn
            return
        was, self._seen = self._seen, seen
        if seen == was:
            return
        if seen[0]:
            self._say('the git sync failed at %s: %s' % (seen[0], seen[1] or '-'))
        else:
            self._say('the git sync works again', 'note')

    def start(self):
        """Start the git sync, the backlog timer and every listener, the listeners last so the
        first request finds the rest running. False, and nothing started, when it was started
        or closed already."""
        with self._life:
            if self._started or self._closed:
                return False
            self._started = True
            if self.sync is not None:
                self.sync.start()
            if self.backlog is not None:
                self.backlog.start()
            for lis in self.listeners:
                lis.start()
            return True

    def _step(self, what, timeout, fn, *args):
        """One step of the close: True when fn(*args) answered True, which is how a step says
        it ended in time. A step that raised, or that answered anything else, is logged by
        its name `what` and answers False."""
        try:
            done = fn(*args)
        except Exception as e:      # noqa: BLE001 - the next step of the close still runs
            self._say('closing: %s failed: %s: %s' % (what, type(e).__name__, e))
            return False
        if done is True:
            return True
        self._say('closing: %s did not end within %s'
                  % (what, 'its time' if timeout is None else '%g s' % timeout))
        return False

    def _last_pass(self):
        """The last git sync pass of the close: it is given no time limit (module docstring;
        _close_sync said so in the log before this). False when it raised; a pass that failed
        is logged and is no unclean close (the next start tries again)."""
        try:
            last = _apart(self.sync.sync_once)
        except Exception as e:      # noqa: BLE001 - the store is still closed
            self._say('closing: the last git sync pass failed: %s: %s' % (type(e).__name__, e))
            return False
        if isinstance(last, dict) and last.get('failed'):
            self._say('the last git sync pass failed at %s: %s; the next start tries again'
                      % (last.get('failed'), last.get('detail') or '-'))
        return True

    def _close_sync(self, timeout, drained):
        """Step 4 of the close (module docstring): True when the sync's thread ended in
        `timeout` and, when the requests had `drained`, the last pass did not raise. Every wait
        for git here is announced in the log first and made through _apart, so a
        KeyboardInterrupt leaves it (close() answers that one)."""
        if drained:
            self._say(LAST_PASS, 'note')
        else:
            self._say(LAST_PASS_SKIPPED)
            self._say(IN_FLIGHT, 'note')
        stopped = self._step('the git sync', timeout, _apart, self.sync.stop, timeout)
        if stopped:
            return self._last_pass() if drained else True
        if drained:
            # A pass is in flight. Wait for it in stop(), which begins no pass, and only then
            # call the last one: a sync_once left waiting behind that pass by a Ctrl+C would
            # begin its own pass after the store closed. LAST_PASS announced this wait. When
            # the wait itself failed the pass may still run: no last pass is called beside it.
            if self._pass_ended():
                self._last_pass()
            else:
                self._say(LEFT_RUNNING)
            return False
        # no last pass will wait for the pass in flight: wait for it here, before the store closes
        self._say(STILL_WAITING, 'note')
        if not self._pass_ended():
            self._say(LEFT_RUNNING)
        return False

    def _pass_ended(self):
        """Wait, with no time limit, for the sync's thread and so for its pass in flight: True
        when it ended. No pass begins here. False when the wait raised (an 'error' line says
        what) or answered anything else; the caller says what follows, and the store is still
        closed."""
        try:
            return _apart(self.sync.stop, None) is True
        except Exception as e:      # noqa: BLE001 - the store is still closed
            self._say('closing: the wait for the git sync pass in flight failed: %s: %s'
                      % (type(e).__name__, e))
            return False

    def _cut_short(self):
        """A KeyboardInterrupt ended the close before the git sync had: say what may be left
        half way, and tell the sync to begin no further pass (no wait: a pass in flight, or a
        last pass that had begun, is left to itself). No wait _close_sync makes is one that
        begins a pass later: module docstring, "The clean close"."""
        self._say(CUT_SHORT)
        try:
            self.sync.stop(0)
        except Exception:           # noqa: BLE001 - the store is still closed
            pass

    def close(self, timeout=CLOSE_TIMEOUT_S):
        """The clean close (module docstring). True when every step ended within `timeout`
        seconds (None: wait) and none raised; a second call answers what the first did (False
        after one that a KeyboardInterrupt left)."""
        with self._life:
            if self._closed:
                return self._clean
            self._closed = True
            self._clean = False
            clean = []
            synced = self.sync is not None and self._started
            try:
                for lis in self.listeners:
                    # first: a request a gate holds (the tunnel's, on a key fetch) is refused
                    # now, so it is not in flight when the requests are waited for below
                    stop = getattr(getattr(lis, 'gate', None), 'stop', None)
                    if callable(stop):
                        clean.append(self._step('the listener gate', timeout, stop))
                for lis in self.listeners:
                    clean.append(self._step('the listener door', timeout, _shut, lis))
                if self.backlog is not None:
                    clean.append(self._step('the backlog timer', timeout, self.backlog.stop,
                                            timeout))
                drained = True
                for lis in self.listeners:
                    done = self._step('the requests in flight', timeout, lis.drain, timeout)
                    drained = drained and done
                    clean.append(done)
                if synced:
                    clean.append(self._close_sync(timeout, drained))
            except KeyboardInterrupt:
                if not synced:
                    raise
                clean.append(False)
                self._cut_short()
            finally:
                try:
                    self.store.close()
                    clean.append(True)
                except Exception as e:      # noqa: BLE001 - the threads are still joined
                    clean.append(False)
                    self._say('closing: the store failed: %s: %s' % (type(e).__name__, e))
                for lis in self.listeners:
                    clean.append(self._step('the handler threads', timeout, lis.join, timeout))
            self._clean = all(x is True for x in clean)
            return self._clean


def _shut(listener):
    """Close a listener's door: True, as a close step that ended (close_door answers nothing)."""
    listener.close_door()
    return True


def _apart(fn, *args):
    """What fn(*args) answered or raised, with the call made on a thread of its own
    (GIT_WAIT_THREAD, a daemon) while this caller waits for it in turns of POLL_S. A wait with no
    time limit is not interrupted by Ctrl+C on Windows; this one is: a KeyboardInterrupt in the
    caller leaves here within one turn, and the call is then left running on its thread."""
    box = []

    def work():
        try:
            box.append((True, fn(*args)))
        except BaseException as e:     # noqa: BLE001 - raised again in the caller, below
            box.append((False, e))

    thread = threading.Thread(target=work, name=GIT_WAIT_THREAD, daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(POLL_S)
    ok, value = box[0]
    if ok:
        return value
    raise value


def run(hub, started=None, stop=None):
    """Start `hub`, call started(hub), serve until the event `stop` is set, the owner's stop
    route sets hub.stopping (hub.py stop) or Ctrl+C, then close it: what Hub.close() answered.
    The hub is closed however this ends. ServeError when the hub was started or closed already.

    The wait is a timed one in a loop: on Windows a wait with no time limit is not interrupted
    by Ctrl+C. Each turn of it looks at hub.stopping (set: a 'note' line, STOP_ASKED, and the
    close), then calls hub.watch(), which logs a git sync failure that is new. No signal
    handler is set, so only Ctrl+C (KeyboardInterrupt), `stop` and the stop route are a clean
    close, and the stop route's is the same close as Ctrl+C's. A
    second Ctrl+C, during the close, is Hub.close's to answer (module docstring, "The clean
    close"): with a git sync started it ends the wait for git and this answers False; with
    none, or when it arrives in the last two steps of the close, it leaves here as a
    KeyboardInterrupt (the store is closed, unless closing it was the step cut)."""
    stop = stop if stop is not None else threading.Event()
    try:
        if not hub.start():
            raise ServeError('this server was started or closed already')
        if started is not None:
            started(hub)
        while not stop.wait(POLL_S):
            if hub.stopping.is_set():
                hub._say(STOP_ASKED, 'note')
                break
            hub.watch()
    except KeyboardInterrupt:
        pass
    finally:
        clean = hub.close()
    return clean


# ------------------------------------------------------------------ the local client

def ask(port, method, path, token=None, body=None, timeout=ASK_TIMEOUT_S, via=VIA_CLI):
    """One request to the hub server on this machine's `port`: (status, the JSON object it
    answered). `token` is sent as a Bearer token; a POST carries `body` as JSON with the Origin
    and Content-Type the listener requires and names itself `via` (VIA_CLI by default; the hub
    MCP server passes VIA_MCP). NotRunning when nothing listens there, ServeError when what
    answers is not the hub."""
    headers = {'Accept': httpd.JSON, 'User-Agent': USER_AGENT}
    if token:
        headers['Authorization'] = 'Bearer %s' % token
    data = None
    if method == 'POST':
        data = json.dumps({} if body is None else body).encode('utf-8')
        headers.update({'Content-Type': httpd.JSON, httpd.VIA_HEADER: via,
                        'Origin': 'http://%s:%d' % (httpd.LOOPBACK, port)})
    conn = http.client.HTTPConnection(httpd.LOOPBACK, port, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=headers)
        answer = conn.getresponse()
        status, raw = answer.status, answer.read(MAX_ANSWER + 1)
    except ConnectionRefusedError:
        raise NotRunning(port)
    except (OSError, http.client.HTTPException) as e:
        raise ServeError('port %d did not answer as the hub does: %s' % (port, type(e).__name__))
    finally:
        conn.close()
    try:
        obj = json.loads(raw.decode('utf-8')) if len(raw) <= MAX_ANSWER else None
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        raise ServeError('port %d answered %d with something that is not the hub\'s JSON'
                         % (port, status))
    return status, obj


def _age(text, now):
    """`now` less the ISO time `text`, in words, or None when `text` is no time."""
    try:
        at = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.timezone.utc)
    s = max(0, int((now - at).total_seconds()))
    if s < 120:
        return '%d s' % s
    if s < 7200:
        return '%d min' % (s // 60)
    return '%d h' % (s // 3600)


def _git_line(git):
    if not isinstance(git, dict):
        return 'no answer'
    if git.get('off'):
        return str(git['off'])
    parts = ['running' if git.get('running') else 'not running']
    if git.get('branch'):
        parts.append('branch %s' % git['branch'])
    pending = [w for k, w in (('pending_commit', 'a commit'), ('pending_push', 'a push'))
               if git.get(k)]
    parts.append('pending: %s' % ' and '.join(pending) if pending else 'nothing pending')
    if git.get('diverged'):
        parts.append('diverged from the remote: a person joins them')
    if git.get('failed'):
        # `detail` is in the owner's answer only (health_route); scrubbed there, and here again
        why = scrub(git['detail']) if git.get('detail') else ''
        parts.append('the last pass failed at %s%s' % (git['failed'], ': %s' % why if why else ''))
    return ', '.join(parts)


def status_lines(health, now=None):
    """The lines `hub status` prints for a GET health answer: the revision, the store (the
    owner's answer has it), the git sync, each repository's backlog fetch age, the tunnel
    listener in one line (`tunnel: on`, `tunnel: bound, keys not fetched` with the reason in
    brackets when health gave one, or `tunnel: off: ` and why; none when the answer has no such
    text, as an older server's) and what is unavailable. `now`: an aware datetime (default the
    UTC clock)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    lines = ['revision %s' % health.get('revision')]
    if health.get('store'):
        lines.append('store %s' % health['store'])
    lines.append('git: %s' % _git_line(health.get('git')))
    repos = health.get('backlog')
    if not isinstance(repos, list):
        lines.append('backlog: no source on this server')
    elif not health.get('backlog_at'):
        lines.append('backlog: not read yet')
    else:
        for r in repos:
            if not isinstance(r, dict):
                continue
            age = _age(r.get('fetched_at'), now)
            line = 'backlog %s: %s' % (r.get('repo'), 'fetched %s ago' % age if age
                                       else 'never fetched')
            lines.append(line + ('; %s' % r['error'] if r.get('error') else ''))
    for key, what in (('backlog_error', 'backlog'), ('backlog_timer', 'backlog timer')):
        if health.get(key):
            lines.append('%s: %s' % (what, health[key]))
    said = health.get('tunnel')
    if isinstance(said, str) and said:
        lines.append('tunnel: %s' % scrub(said))    # fixed words; scrubbed all the same
    missing = health.get('unavailable')
    if isinstance(missing, dict) and missing:
        lines.append('unavailable: %s' % ', '.join('%s (%s)' % (k, missing[k])
                                                   for k in sorted(missing)))
    return lines
