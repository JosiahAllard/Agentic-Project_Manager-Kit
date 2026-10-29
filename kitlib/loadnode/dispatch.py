"""Where a run under the machine lock goes: here or a load node (docs/load-nodes.md "Lock runs on
a node", docs/lock-api.md "Load nodes").

  Router     the route= of kitlib.lock.run.run_command for `machine_lock.py run`: None runs the
             command here as always; a number is the run's exit code after it ran on a node
  on_node    one job on one node: (exit code, fallback), shared with `node.py run`
  windows_only(cmd)  why cmd cannot run on a Linux node, or ''
  status_lines       the `node:` lines of `machine_lock.py status`

With node 'auto' (the default) a run goes to a node only when one has opted its repository in
(the registry entry's `repos`), nothing about the run ties it to this machine, and route.choose
picks the node; anything else runs it here. Silently, when there is no registry, no node has
opted the repository in, or the run is one a node never takes (a nested run, a detached one, a
test store with no registry named, --cores, --hold, --gpu-share, --within, --resource,
--on-release); with one line naming why once a node has opted the repository in (a Windows
command, the folder is not the checkout's top, tracked changes not committed, a spec the node
would refuse, a lock that cannot be read). Node 'main' runs it here. A node's name runs it on
that node or refuses (EXIT_USAGE) when it cannot.

Once a node has started the job it never runs here: a failure after that is
job.EXIT_AFTER_START (job.EXIT_LOST when the connection did not come back, the job running on).
A failure before the start, or the node's lock refusing it as busy, runs it here under auto.
Nothing here prints: `say(text)` gets every line and `out(bytes)` the job's own output.
"""
import os
import re

from kitlib.loadnode import box, job, registry, remote, reply, route

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NODE_TOOL = 'python "%s"' % os.path.join(KIT_ROOT, 'tools', 'node.py')
NEST_ENV = 'MACHINE_LOCK_RUN'                  # kitlib.lock.run.NEST_ENV: a run inside a run
STATUS_TIMEOUT_S = 10
EXIT_INTERRUPTED = 130

WINDOWS_PROGRAMS = ('powershell', 'pwsh', 'cmd')
WINDOWS_EXTS = ('.bat', '.cmd', '.ps1', '.exe')
DRIVE_RE = re.compile(r'(?:^|[^A-Za-z0-9])[A-Za-z]:(?:[\\/]|$)')
# The run_command arguments a node does not take: a run with any of them set stays here.
HERE_ONLY = (('cores', '--cores'), ('hold', '--hold'), ('gpu_share', '--gpu-share'),
             ('within', '--within'), ('resource', '--resource'), ('on_release_cmd', '--on-release'),
             ('rid', '--detach'), ('detached', '--detach'), ('env', 'an environment of its own'))


def windows_only(cmd):
    """Why `cmd` cannot run on a Linux node, or '': a Windows program (a .bat, .cmd, .ps1 or .exe,
    or powershell, pwsh or cmd) or an argument with a drive letter or a backslash."""
    first = os.path.basename(str(cmd[0]).replace('\\', '/')).lower() if cmd else ''
    if first.endswith(WINDOWS_EXTS) or first in WINDOWS_PROGRAMS:
        return 'the command is a Windows program (%s)' % first
    for arg in cmd:
        if '\\' in arg or DRIVE_RE.search(arg):
            return 'an argument is a Windows path (%s)' % arg[:80]
    return ''


def on_node(node, open_link, make_job, say):
    """Run one job on `node`: (exit code, fallback). open_link(node) is a context manager giving a
    remote.Link, make_job(link) the job.NodeJob. fallback True: nothing ran on the node and the
    main machine may run it (job.JobError's fallback). An OSError once the node has started the
    job is EXIT_AFTER_START and never a fallback. Ctrl+C asks the node to stop a started job."""
    nj = None
    try:
        with open_link(node) as link:
            nj = make_job(link)
            try:
                return nj.run(), False
            except KeyboardInterrupt:
                return _interrupted(nj, say), False
    except job.JobError as e:
        say(e.message)
        return e.code, e.fallback
    except OSError as e:
        if nj is not None and nj.started:
            say('job %s on %s: %s; it is not run again on main' % (nj.job, node.name, e))
            return job.EXIT_AFTER_START, False
        say('%s could not be used: %s' % (node.name, e))
        return job.EXIT_NODE, True


def _interrupted(nj, say):
    if not nj.started:
        say('Ctrl+C: job %s was not seen to start on %s; if it did, stop it: %s stop %s %s'
            % (nj.job, nj.node.name, nj.tool, nj.node.name, nj.job))
        return EXIT_INTERRUPTED
    try:
        d = nj.stop()
        asked = 'had ended already' if d.get('ended') else 'asked to stop' if d.get('ok') \
            else 'not stopped: %s' % str(d.get('error'))[:200]
    except (job.JobError, OSError) as e:
        asked = 'not stopped: %s' % e
    say('Ctrl+C: job %s on %s %s' % (nj.job, nj.node.name, asked))
    say('  follow it: %s follow %s %s' % (nj.tool, nj.node.name, nj.job))
    return EXIT_INTERRUPTED


def notice_dir(kind, store=None):
    """The lock-window notice folder for `kind` (kitlib.lock.site.notice), or None."""
    from kitlib.lock import site
    from kitlib.lock.store import Store
    try:
        n = site.notice(store or Store())
    except Exception:                       # no notice never stops a run
        return None
    if n and (n[1] is None or kind in n[1]):
        return n[0]
    return None


def main_lock(store):
    """This machine's lock as a node reply's lock dict (route.choose's main_lock)."""
    from kitlib.lock import api
    return reply.build(api.snapshot(store, history_days=0), reply.kit_version(KIT_ROOT))['lock']


class Decline(Exception):
    """The run stays here; `quiet` when nothing needs saying (no node would take it anyway)."""

    def __init__(self, why, quiet=False):
        Exception.__init__(self, why)
        self.why, self.quiet = why, quiet


class Router(object):
    """route= for run_command (the module docstring). node: 'auto', 'main' or a node's name;
    artifacts: the globs to copy back; registry_file: the registry (None: registry.path(), and
    on a test store none). The rest are the tests' stand-ins."""

    def __init__(self, node='auto', artifacts=(), registry_file=None, out=None, runner=None,
                 open_link=None, environ=None, cwd=None, checkout=None, need=None, lock=None,
                 reattach_s=job.REATTACH_S, tool=NODE_TOOL, git=None):
        self.node, self.artifacts, self.registry_file = node, list(artifacts), registry_file
        self.out = out
        self.runner = runner or remote.default_runner
        self.open_link = open_link or remote.Link
        self.environ = os.environ if environ is None else environ
        self.cwd = cwd
        self.checkout = checkout or job.checkout
        self.need = need or (lambda kind, store: route.need_of(kind, store=store))
        self.lock = lock or main_lock
        self.reattach_s, self.tool, self.git = reattach_s, tool, git

    def __call__(self, cmd, kind, repo, slug, kw):
        if self.node == 'main':
            return None
        store = kw['store']
        try:
            return self._route(cmd, kind, repo, slug, kw, store)
        except KeyboardInterrupt:              # on_node answers it once a job exists
            store.say('Ctrl+C: nothing was started on a load node')
            return EXIT_INTERRUPTED

    def _route(self, cmd, kind, repo, slug, kw, store):
        say = store.say
        named = self.node != 'auto'
        try:
            nodes, co, need = self._plan(cmd, kind, repo, slug, kw, store, named)
        except Decline as d:
            if named:
                say('usage: --node %s: %s' % (self.node, d.why))
                return job.EXIT_USAGE
            if not d.quiet:
                say('running here, not on a load node: %s' % d.why)
            return None
        if named:
            return self._run(nodes[0], co, cmd, kind, repo, slug, kw, store)[0]
        results = remote.query_all(nodes, runner=self.runner)
        try:
            mine = self.lock(store)
        except Exception as e:
            say('running here, not on a load node: this machine\'s lock cannot be read (%s)'
                % type(e).__name__)
            return None
        choice = route.choose(nodes, results, repo, mine, need)
        say('%s: %s' % ('running here' if choice.node is None else 'running on load node '
                        + choice.where, choice.why))
        if choice.node is None:
            return None
        code, fallback = self._run(choice.node, co, cmd, kind, repo, slug, kw, store)
        if fallback:
            say('running here instead')
            return None
        return code

    def _plan(self, cmd, kind, repo, slug, kw, store, named):
        """(the nodes to ask, the checkout, the kind's need here), or Decline."""
        env = kw.get('env') or self.environ
        if env.get(NEST_ENV):
            raise Decline('it runs inside the machine lock run %s' % env[NEST_ENV].split(':')[0],
                          quiet=True)
        for key, flag in HERE_ONLY:
            if kw.get(key):
                raise Decline('a node does not take %s' % flag, quiet=True)
        path = self.registry_file
        if path is None and not store.testing:
            path = registry.path()
        if not path or not os.path.isfile(path):
            raise Decline('no load node registry', quiet=True)
        try:
            nodes = registry.load(path)
        except registry.RegistryError as e:
            raise Decline('the load node registry cannot be used: %s' % e)
        if named:
            nodes = [n for n in nodes if n.name == self.node]
            if not nodes:
                raise Decline('no node named %s in the registry' % self.node)
            if repo not in nodes[0].repos:
                raise Decline('%s has not opted in %s: add it to the node\'s "repos" in the '
                              'registry' % (self.node, repo))
        else:
            nodes = [n for n in nodes if repo in n.repos]
            if not nodes:
                raise Decline('no node has opted in %s' % repo, quiet=True)
        why = windows_only(cmd)
        if why:
            raise Decline(why)
        for what, v in (('repo', repo), ('slug', slug)):
            if not registry.repo_ok(v):
                raise Decline('the %s %r is not a name a node takes (letters, digits, "_", "." '
                              'and "-")' % (what, v))
        here = os.path.abspath(self.cwd or os.getcwd())
        try:
            co = self.checkout(here)
        except job.JobError as e:
            raise Decline(e.message)
        if os.path.normcase(os.path.normpath(here)) != os.path.normcase(os.path.normpath(co.top)):
            raise Decline('a node runs in the checkout\'s top (%s), not in %s' % (co.top, here))
        if co.dirty:
            raise Decline('%s has tracked changes not committed; a node runs HEAD' % co.top)
        try:
            box.check_spec(self._spec(co, cmd, kind, repo, slug, kw))
        except box.SpecError as e:
            raise Decline('a node would refuse it: %s' % e)
        need = None
        if not named:
            try:
                need = self.need(kind, store)
            except KeyError:                    # an unknown kind: the lock here refuses it
                raise Decline('no kind %s' % kind, quiet=True)
            except Exception as e:
                raise Decline('the kind %s cannot be sized here (%s)' % (kind, e))
        return nodes, co, need

    def _spec(self, co, cmd, kind, repo, slug, kw):
        return {'job': box.new_job_id(), 'repo': repo, 'slug': slug, 'kind': kind, 'sha': co.sha,
                'cmd': list(cmd), 'artifacts': self.artifacts, 'max_jobs': 1,
                'wait_s': kw.get('wait_s'), 'expect_s': kw.get('expect_s'),
                'no_wait': bool(kw.get('no_wait')), 'note': kw.get('note')}

    def _run(self, node, co, cmd, kind, repo, slug, kw, store):
        def make_job(link):
            return job.NodeJob(node, link, co.top, co.sha, repo, slug, kind, cmd, self.artifacts,
                               wait_s=kw.get('wait_s'), expect_s=kw.get('expect_s'),
                               no_wait=bool(kw.get('no_wait')), note=kw.get('note'),
                               say=store.say, out=self.out, git=self.git,
                               notice_dir=None if store.testing else notice_dir(kind, store),
                               reattach_s=self.reattach_s, tool=self.tool)
        return on_node(node, self.open_link, make_job, store.say)


def _gb(mb):
    return '?' if mb is None else '%.1f' % (mb / 1024.0)


def status_lines(registry_file=None, testing=False, runner=None, timeout_s=STATUS_TIMEOUT_S):
    """The `node:` lines of `machine_lock.py status`: one per node in the registry, asked now
    (remote.query_all). No line when there is no registry (none on a test store unless named)."""
    path = registry_file if registry_file is not None or testing else registry.path()
    if not path or not os.path.isfile(path):
        return []
    try:
        nodes = registry.load(path)
    except registry.RegistryError as e:
        return ['node: the load node registry cannot be used: %s' % e]
    results = remote.query_all(nodes, runner=runner or remote.default_runner, timeout_s=timeout_s)
    here = reply.kit_version(KIT_ROOT)
    lines = []
    for r in results:
        if r.reply is None:
            lines.append('node: %s  %s%s' % (r.name, r.state, ': ' + r.detail if r.detail else ''))
            continue
        lk, mem = r.reply['lock'], r.reply['hardware']['memory_mb']
        kit = r.reply['kit_version']
        lines.append('node: %s  %s  cpu %d/%d free  gpu %d/%d free  memory %s/%s GB free  '
                     '%d running, %d waiting%s'
                     % (r.name, r.state, lk['cpu_free'], lk['cpu_slots'], lk['gpu_free'],
                        lk['gpu_slots'], _gb(mem['free']), _gb(mem['total']),
                        lk['running'], lk['waiting'],
                        '  kit %s (here %s)' % (kit, here) if here and kit != here else ''))
    return lines
