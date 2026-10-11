"""kitlib.hubsite.serve: the hub server, wired from its parts (the hub site design, sections 2
and 9).

  build(settings, sources, log=None) -> Hub     open the store, make the views, the Api and the
                                                listeners; nothing is started. ServeError.
  Hub   .settings .store .api .views .creds .listeners .sync .backlog .notes .port .url
        .start()                                start the git sync, the backlog timer, the listeners
        .close(timeout=CLOSE_TIMEOUT_S)         the clean close (below); safe to call twice
  run(hub, started=None, stop=None)             start, serve until `stop` is set or Ctrl+C, close
  Sources(...)                                  where each part comes from
  machine_sources(home=None, sync=True)         this machine's Sources, from its machine config
  SiteViews                                     the Api's `views` object over those sources
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
  2. the token files (auth.ensure_tokens): a token file that cannot serve is refused, named.
  3. the backlog views and the git sync, from the Sources' factories. A sync that is refused
     (gitsync.Refused: the folder is no work tree, the wrong branch, ...) does not stop the
     server: the reason goes to `Hub.notes` for the console, the server serves with sync off,
     and GET health says so in a fixed phrase (the reason itself names folders).
  4. the Api over the store and a SiteViews, then the local listener on settings.port with
     httpd.local_hosts, httpd.local_gate and httpd.pair_routes over an auth.Credentials made for
     the port that was really bound.
`Hub.listeners` is a list. It holds the local listener; a second listener (the tunnel's, on
settings.tunnel_port) is appended there by the code that builds it, and start() and close()
then carry it with no change here.

The clean close, in this order (Hub.close; run() ends with it, also on Ctrl+C):
  1. every listener's door: no new connection, and no write begins (httpd refuses it);
  2. the backlog timer;
  3. the requests in flight, except the long waits: after this no write is in flight;
  4. the git sync's thread, then one last pass (sync_once) while the store is still open and
     locked, so what was written since the last pass is committed whole. Only when the server
     was started; a pass that fails is an error line in the log and is tried again at the next
     start;
  5. the store: every long wait ends and the store lock is released;
  6. the handler threads.
Each step is given `timeout` seconds; one that fails or runs out is logged and the next still
runs, and the store is closed whatever happened before it. close() answers True when every
step ended in time. The last pass is the exception: it runs to its end, however long git's own
time limits let it (kitlib.hubsite.gitsync).
A process that is killed closes nothing: the system drops the store lock with it, and the next
start takes it over.

Views (SiteViews). backlog, completed, repos and agents exist only where their source does, so
the Api answers a missing one `unavailable` by itself. What a view says never holds a path,
git's words or an exception's text: the backlog views carry kitlib.hubsite.backlog's fixed
phrases, and the reasons of the derived views are replaced by fixed phrases here.
  backlog(repo, state, fresh), completed(repo, days)
      [{repo, tip, fetched_at, items, error, stale, fetch_error}]; completed adds `days`.
      `tip` is always null (backlog.Views does not give the commit it read). `days` narrows the
      window backlog.Views was built with and cannot widen it.
  notices() repos() usage() agents()      kitlib.hubsite.derived's answers
  health()    {git, backlog, backlog_at, backlog_error, backlog_timer}. `git` is GitSync.status()
      without `detail`, or {"running": false, "off": phrase}. `backlog` is null with no backlog
      source, else [{repo, fetched_at, error}] as the last backlog view read gave it
      (`backlog_at`: when; null and an empty list before the first read): health starts no git.

ask() is the small client `hub status` and `hub open` need: one request to the loopback
listener through http.client, which uses no proxy, so a token never leaves this machine.
"""
import datetime
import http.client
import json
import os
import threading

from kitlib import hubbacklog, pmdata, pmstart, pmwatch
from kitlib.config import user
from kitlib.config.types import ConfigError

from . import api as _api
from . import auth, backlog, derived, gitsync, httpd
from .store import HubError, Store, StoreBusy

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
CLOSE_TIMEOUT_S = 10            # each step of the clean close
POLL_S = 0.5                    # run()'s wait between two looks at `stop` (and at Ctrl+C)
ASK_TIMEOUT_S = 10
MAX_ANSWER = 16 << 20           # bytes of one answer ask() reads
VIA_CLI = 'cli'
SYNC_OFF = 'git sync is off on this server'
SYNC_REFUSED = 'git sync was refused at start-up: the server console says why'
NO_USAGE_SOURCE = 'no launcher usage source configured'     # derived.usage's own phrase
USAGE_FAILED = 'the usage source could not be read'
AGENTS_FAILED = 'the agent definitions could not be read'


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


class Sources(object):
    """Where the server's parts come from. A part that is None is a view the server does not
    have (or, for `sync`, a server that commits nothing).

      tokens       the credential folder (auth.folder)
      static_dir   the page's folder; a missing one answers 404
      pm_data      kitlib.pmdata's {path, branch, remote}, or None
      notices      the machine lock's notice folder
      repos        callable(the store's repos documents) -> [{name, aliases}]
      usage        callable() -> the lines kitlib.launcher.usage prints
      agents       the folder of agent definitions
      backlog      callable(refresh_s) -> a kitlib.hubsite.backlog.Views (or its like: get, start,
                   stop, days, clock, timer_error)
      sync         callable(store_dir, commit_every_s, remote=, branch=, push=) -> a
                   kitlib.hubsite.gitsync.GitSync (or its like: start, stop, sync_once, status)
    """

    def __init__(self, tokens=None, static_dir=STATIC_DIR, pm_data=None, notices=None, repos=None,
                 usage=None, agents=None, backlog=None, sync=None):
        self.tokens = tokens
        self.static_dir = static_dir
        self.pm_data = pm_data
        self.notices = notices
        self.repos = repos
        self.usage = usage
        self.agents = agents
        self.backlog = backlog
        self.sync = sync


def _repo_pairs(cfg):
    """A callable giving backlog.Views its (name, root) pairs from the pm_start config `cfg`,
    as claude/hub/hub_backlog.py finds them. The PM repository's root is asked of git once."""
    found = []

    def pairs():
        if not found:
            top = hubbacklog.git(os.path.dirname(os.path.abspath(cfg['sessions_file'])),
                                 'rev-parse', '--show-toplevel')
            found.append(top.decode('utf-8').strip())
        return backlog.repo_list(pmstart.known_repos(cfg), found[0], cfg.get('repo_paths'))
    return pairs


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
                  agents=os.path.join(pmstart.KIT_ROOT, 'claude', 'agents'),
                  sync=gitsync.GitSync if sync else None)
    if pmwatch.launcher_configured(home):
        def report():
            from kitlib.launcher import settings as launcher_settings   # here: only this needs it
            from kitlib.launcher import usage as launcher_usage
            return launcher_usage.report(launcher_settings.load(home))
        src.usage = report
    if cfg and cfg.get('sessions_file'):
        src.repos = lambda stored: derived.repos(cfg, stored)
        src.backlog = lambda refresh_s: backlog.Views(_repo_pairs(cfg), refresh_s)
    return src


def _named(entries, repo):
    """The entries of the repository `repo`, compared without regard to case ('' : all)."""
    if not repo:
        return list(entries)
    want = repo.lower()
    return [e for e in entries if str(e.get('repo') or '').lower() == want]


def _entry(e, items):
    return {'repo': e.get('repo'), 'tip': None, 'fetched_at': e.get('fetched'), 'items': items,
            'error': e.get('error'), 'stale': bool(e.get('stale')),
            'fetch_error': e.get('fetch_error')}


class SiteViews(object):
    """The Api's `views` (module docstring, "Views"). A view is bound only where its source
    exists: `backlog_views` for backlog and completed, sources.repos and .agents for theirs.
    notices, usage and health are always there (kitlib.hubsite.derived answers a missing notice
    folder with no notices and a missing usage source with its own phrase). `sync` is the git
    sync or None, `sync_off` the fixed phrase health gives when there is none."""

    def __init__(self, store, sources, backlog_views=None, sync=None, sync_off=SYNC_OFF):
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
        if sources.repos is not None:
            self.repos = self._repos
        if sources.agents:
            self.agents = self._agents

    def _read(self, fresh=False):
        got = self._backlog.get(fresh=bool(fresh))
        entries = got.get('repos') or []
        # one assignment: health reads it from another thread and takes no lock
        self._last = {'at': got.get('at'), 'error': got.get('error'),
                      'repos': [{'repo': e.get('repo'), 'fetched_at': e.get('fetched'),
                                 'error': e.get('fetch_error') or e.get('error')}
                                for e in entries]}
        if got.get('error') and not entries:
            raise NoRepoList()
        return entries

    def _open(self, repo='', state='', fresh=False):
        out = []
        for e in _named(self._read(fresh), repo):
            items = e.get('open') or []
            if state:
                items = [i for i in items if i.get('s') == state]
            out.append(_entry(e, items))
        return out

    def _completed(self, repo='', days=None):
        entries = self._read()
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
        notes.append('git sync is off: %s' % e)
        return None, SYNC_REFUSED
    return sync, ''


def build(settings, sources, log=None):
    """The Hub for `settings` (kitlib.hubsite.settings.Settings) from `sources` (a Sources):
    module docstring, "Start-up". Nothing is started; close() the Hub to release the store.
    `log(level, text)` gets the listeners' lines (httpd) and the close's errors. ServeError."""
    if not settings.store:
        raise ServeError('no store folder: set hub.store or pm_data in the machine config, '
                         'or name one')
    if not sources.tokens:
        raise ServeError('no credential folder')
    notes = []
    store = _open_store(settings.store)
    listeners = []
    try:
        if store.took_over:
            notes.append('the store lock was left by a server that did not close: taken over')
        try:
            auth.ensure_tokens(sources.tokens)
        except auth.TokenFileError as e:
            raise ServeError(str(e))
        except OSError as e:
            raise ServeError('cannot write the token files in %s: %s' % (sources.tokens, e))
        views_b = None
        if sources.backlog is not None:
            views_b = sources.backlog(settings.backlog_refresh_s)
        sync, sync_off = _make_sync(settings, sources, notes)
        views = SiteViews(store, sources, views_b, sync, sync_off)
        api = _api.Api(store, views)
        try:
            local = httpd.Listener((httpd.LOOPBACK, settings.port), api, None,
                                   static_dir=sources.static_dir, host_rule=httpd.local_hosts,
                                   log=log)
        except httpd.BindRefused as e:
            raise ServeError('%s: is a hub server running already?' % e)
        listeners.append(local)
        creds = auth.Credentials(sources.tokens, port=local.port)
        local.gate = httpd.local_gate(creds)
        local.routes.update(httpd.pair_routes(creds))
        return Hub(settings, store, api, views, creds, listeners, sync, views_b, notes, log)
    except BaseException:
        for lis in listeners:
            lis.close_door()
        store.close()
        raise


class Hub(object):
    """One built server (build): its parts, start() and close(). `notes` are start-up lines for
    the console; `port` and `url` are the local listener's."""

    def __init__(self, settings, store, api, views, creds, listeners, sync, backlog_views, notes,
                 log=None):
        self.settings = settings
        self.store = store
        self.api = api
        self.views = views
        self.creds = creds
        self.listeners = listeners
        self.sync = sync
        self.backlog = backlog_views
        self.notes = notes
        self._log = log
        self._life = threading.Lock()       # start() and close()
        self._started = False
        self._closed = False
        self._clean = None

    @property
    def port(self):
        return self.listeners[0].port

    @property
    def url(self):
        return self.listeners[0].url

    def _say(self, text):
        if self._log is None:
            return
        try:
            self._log('error', text)
        except Exception:           # noqa: BLE001 - a log that fails must not stop the close
            pass

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

    def _step(self, what, fn, *args):
        """fn(*args), or None after logging what it raised."""
        try:
            return fn(*args)
        except Exception as e:      # noqa: BLE001 - the next step of the close still runs
            self._say('closing: %s failed: %s: %s' % (what, type(e).__name__, e))
            return None

    def close(self, timeout=CLOSE_TIMEOUT_S):
        """The clean close (module docstring). True when every step ended within `timeout`
        seconds (None: wait); a second call answers what the first did."""
        with self._life:
            if self._closed:
                return self._clean
            self._closed = True
            clean = []
            try:
                for lis in self.listeners:
                    self._step('the listener door', lis.close_door)
                if self.backlog is not None:
                    clean.append(self._step('the backlog timer', self.backlog.stop, timeout))
                for lis in self.listeners:
                    clean.append(self._step('the requests in flight', lis.drain, timeout))
                if self.sync is not None and self._started:
                    clean.append(self._step('the git sync', self.sync.stop, timeout))
                    last = self._step('the last git sync pass', self.sync.sync_once)
                    if isinstance(last, dict) and last.get('failed'):
                        self._say('the last git sync pass failed at %s: %s; the next start '
                                  'tries again' % (last.get('failed'), last.get('detail') or '-'))
            finally:
                try:
                    self.store.close()
                    clean.append(True)
                except Exception as e:      # noqa: BLE001 - the threads are still joined
                    clean.append(False)
                    self._say('closing: the store failed: %s: %s' % (type(e).__name__, e))
                for lis in self.listeners:
                    clean.append(self._step('the handler threads', lis.join, timeout))
            self._clean = all(x is True for x in clean)
            return self._clean


def run(hub, started=None, stop=None):
    """Start `hub`, call started(hub), serve until the event `stop` is set or Ctrl+C, then close
    it: what Hub.close() answered. The hub is closed however this ends. ServeError when the hub
    was started or closed already.

    The wait is a timed one in a loop: on Windows a wait with no time limit is not interrupted
    by Ctrl+C. No signal handler is set, so only Ctrl+C (KeyboardInterrupt) and `stop` are a
    clean close."""
    stop = stop if stop is not None else threading.Event()
    try:
        if not hub.start():
            raise ServeError('this server was started or closed already')
        if started is not None:
            started(hub)
        while not stop.wait(POLL_S):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        clean = hub.close()
    return clean


# ------------------------------------------------------------------ the local client

def ask(port, method, path, token=None, body=None, timeout=ASK_TIMEOUT_S):
    """One request to the hub server on this machine's `port`: (status, the JSON object it
    answered). `token` is sent as a Bearer token; a POST carries `body` as JSON with the Origin
    and Content-Type the listener requires and names itself VIA_CLI. NotRunning when nothing
    listens there, ServeError when what answers is not the hub."""
    headers = {'Accept': httpd.JSON}
    if token:
        headers['Authorization'] = 'Bearer %s' % token
    data = None
    if method == 'POST':
        data = json.dumps({} if body is None else body).encode('utf-8')
        headers.update({'Content-Type': httpd.JSON, httpd.VIA_HEADER: VIA_CLI,
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
        parts.append('the last pass failed at %s' % git['failed'])
    return ', '.join(parts)


def status_lines(health, now=None):
    """The lines `hub status` prints for a GET health answer: the revision, the store (the
    owner's answer has it), the git sync, each repository's backlog fetch age, and what is
    unavailable. `now`: an aware datetime (default the UTC clock)."""
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
    missing = health.get('unavailable')
    if isinstance(missing, dict) and missing:
        lines.append('unavailable: %s' % ', '.join('%s (%s)' % (k, missing[k])
                                                   for k in sorted(missing)))
    return lines
