"""node.py: the load nodes in <PM repo>/.pm-kit/load-nodes.json (docs/load-nodes.md).

  node.py list
      each node in the registry: where it is, what it may take, its pinned host key's fingerprint
      and whether its identity file is there; nothing goes on the network
  node.py status [NAME ...] [--json] [--timeout S]
      ask each node (or the ones named) for its machine lock and hardware over ssh, the host key
      pinned; --json prints the checked replies as one JSON object
  node.py run --kind K --repo R --slug S [--node auto|NAME] [--artifact GLOB]... [--max-wait D |
              --no-wait] [--expect D] [--note TEXT] [--reattach S] [-C DIR] -- <command>
      run the command on the checkout's HEAD under a machine lock: on the node named, or (auto,
      the default) on whichever of the main machine and the nodes that opted the repository in
      has the kind's slots free now; its output streams here, its artifacts (the files matching
      each GLOB, relative to the checkout's top) are copied back into the checkout
  node.py follow NODE JOB [--artifact GLOB]... [-C DIR] [--reattach S]
      stream a job's output from its start to its end, then fetch its artifacts and clean it
  node.py stop NODE JOB
      ask a node to stop a job

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.loadnode, prints and exits.
Exit codes, list and status: 0 every node asked answered; 1 at least one did not (every node is
still listed); 2 usage, or a registry that is missing, invalid or not ignored by git.
run and follow: the command's own exit code; 2 usage (an unknown node or kind, a repository the
node has not opted in, tracked changes not committed for --node NAME); 6 the node was busy; 9 the
connection was lost (the job runs on: the follow and stop lines are said); 10 the node could not
be used; 13 the job succeeded but its artifacts did not all come. With --node auto a node that
fails before its job starts, or is busy, gives way to the main machine. stop: 0 asked, 1 refused.
"""
import argparse
import json
import os
import re
import sys

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT_ROOT)

from kitlib.loadnode import box, job, registry, remote, reply, route       # noqa: E402

OK, FAILED, USAGE = 0, 1, 2
PREFIX = 'node.py:'
TOOL = 'python "%s"' % os.path.abspath(__file__)


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def _timeout(text):
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a whole number of seconds')
    if not 1 <= v <= 300:
        raise argparse.ArgumentTypeError('must be from 1 to 300 seconds')
    return v


def _seconds(text):
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*', text or '')
    v = float(m.group(1)) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[m.group(2)] if m else None
    if v is None or not 0 < v <= box.MAX_WAIT_S:
        raise argparse.ArgumentTypeError('takes N, Ns, Nm, Nh or Nd (more than 0, at most 7d), '
                                         'not %r' % text)
    return v


def _reattach(text):
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a whole number of seconds')
    if not 0 <= v <= 86400:
        raise argparse.ArgumentTypeError('must be from 0 to 86400 seconds')
    return v


def _glob(text):
    if not box.glob_ok(text):
        raise argparse.ArgumentTypeError('%r is not a relative glob under the checkout (no "..", '
                                         'no ".git", "/" between parts)' % text)
    return text


def _name(what):
    def check(text):
        if not registry.repo_ok(text):
            raise argparse.ArgumentTypeError('%r is not a %s (letters, digits, "_", "." and "-")'
                                             % (text, what))
        return text
    return check


def parser():
    p = Parser(prog='node.py', allow_abbrev=False, description='The load nodes.')
    p.add_argument('--registry', help=argparse.SUPPRESS)     # tests: a registry file of their own
    sub = p.add_subparsers(dest='verb', metavar='{list,status,run,follow,stop}', parser_class=Parser)
    sub.required = True
    sub.add_parser('list', help='the registry, no network', allow_abbrev=False)
    s = sub.add_parser('status', help='ask each node over ssh', allow_abbrev=False)
    s.add_argument('names', nargs='*', metavar='NAME', help='only these nodes')
    s.add_argument('--json', action='store_true', help='print the checked replies as JSON')
    s.add_argument('--timeout', type=_timeout, default=remote.TIMEOUT_S,
                   help='seconds to wait for each node (default %d)' % remote.TIMEOUT_S)
    r = sub.add_parser('run', help='run a command on a node, or wherever has room',
                       allow_abbrev=False,
                       usage='node.py run --kind K --repo R --slug S [options] -- <command>')
    r.add_argument('--node', default='auto', help='a node\'s name, or auto (default)')
    r.add_argument('--kind', required=True)
    r.add_argument('--repo', required=True, type=_name('repo name'))
    r.add_argument('--slug', required=True, type=_name('slug'))
    r.add_argument('--artifact', action='append', default=[], type=_glob, metavar='GLOB',
                   help='files to copy back, relative to the checkout\'s top (repeatable)')
    w = r.add_mutually_exclusive_group()
    w.add_argument('--no-wait', action='store_true', help='the lock\'s exit 6 at once when it must wait')
    w.add_argument('--max-wait', dest='wait_s', type=_seconds, default=None)
    r.add_argument('--expect', dest='expect_s', type=_seconds, metavar='D')
    r.add_argument('--note')
    _follow_args(r)
    f = sub.add_parser('follow', help='stream a job to its end, then fetch and clean it',
                       allow_abbrev=False)
    f.add_argument('node', metavar='NODE')
    f.add_argument('job', metavar='JOB')
    f.add_argument('--artifact', action='append', default=[], type=_glob, metavar='GLOB')
    _follow_args(f)
    t = sub.add_parser('stop', help='ask a node to stop a job', allow_abbrev=False)
    t.add_argument('node', metavar='NODE')
    t.add_argument('job', metavar='JOB')
    return p


def _follow_args(p):
    p.add_argument('-C', dest='cwd', metavar='DIR', help='the checkout (default: here)')
    p.add_argument('--reattach', type=_reattach, default=job.REATTACH_S, metavar='S',
                   help='seconds to keep trying when the connection drops (default %d)'
                        % job.REATTACH_S)


def _err(text):
    sys.stderr.write(text + '\n')
    sys.stderr.flush()


def _say(text):
    _err('%s %s' % (PREFIX, text))


def _out(data):
    buf = getattr(sys.stdout, 'buffer', None)
    if buf is None:
        sys.stdout.write(data.decode('utf-8', 'replace'))
    else:
        buf.write(data)
        buf.flush()


# -- the seams the tests replace ------------------------------------------------------------------

def _link(node):
    return remote.Link(node)


def _main_lock():
    """The main machine's lock as a node reply's lock dict."""
    from kitlib.lock import api
    return reply.build(api.snapshot(history_days=0), reply.kit_version(KIT_ROOT))['lock']


def _need(kind):
    return route.need_of(kind)


def _notice_dir(kind):
    from kitlib.lock import site
    from kitlib.lock.store import Store
    try:
        n = site.notice(Store())
    except Exception:                       # no notice never stops a run
        return None
    if n and (n[1] is None or kind in n[1]):
        return n[0]
    return None


def _run_main(a, cmd, top):
    """Run on this machine under its machine lock, in the checkout's top."""
    from kitlib.lock.run import run_command
    from kitlib.lock.store import LockError, Store
    os.chdir(top)
    try:
        return run_command(cmd, a.kind, a.repo, a.slug, store=Store(say=_say), wait_s=a.wait_s,
                           no_wait=a.no_wait, expect_s=a.expect_s, note=a.note)
    except LockError as e:
        _say(str(e))
        return e.code


# -- the verbs -------------------------------------------------------------------------------------

def _load(a):
    return registry.load(a.registry if a.registry else registry.path())


def _node_job(a, node, link, co, cmd=None, job_id=None):
    return job.NodeJob(node, link, co.top if co else None, co.sha if co else None,
                       getattr(a, 'repo', None), getattr(a, 'slug', None), getattr(a, 'kind', None),
                       cmd or [], a.artifact, wait_s=getattr(a, 'wait_s', None),
                       expect_s=getattr(a, 'expect_s', None), no_wait=getattr(a, 'no_wait', False),
                       note=getattr(a, 'note', None), say=_say, out=_out,
                       notice_dir=_notice_dir(a.kind) if cmd else None, reattach_s=a.reattach,
                       job=job_id, tool=TOOL)


def _on_node(a, node, co, cmd):
    """(exit code, fallback): fallback True when nothing ran on the node and main may run it."""
    try:
        with _link(node) as link:
            return _node_job(a, node, link, co, cmd).run(), False
    except job.JobError as e:
        _say(e.message)
        return e.code, e.fallback
    except OSError as e:
        _say('%s could not be used: %s' % (node.name, e))
        return job.EXIT_NODE, True


def cmd_run(a, cmd, runner=None):
    if not cmd:
        _err('USAGE: node.py run ... -- <command>: no command after --')
        return USAGE
    try:
        co = job.checkout(os.path.abspath(a.cwd or os.getcwd()))
    except job.JobError as e:
        _err('USAGE: %s' % e.message)
        return USAGE
    try:
        need = _need(a.kind)
    except KeyError:
        _err('USAGE: no kind %s in the machine lock\'s kinds table' % a.kind)
        return USAGE
    except Exception as e:
        _say('the kind %s cannot be sized here (%s); running on main' % (a.kind, e))
        return _run_main(a, cmd, co.top)
    try:
        nodes = _load(a)
    except registry.RegistryError as e:
        if a.node != 'auto':
            _err('REFUSED: %s' % e)
            return USAGE
        _say('no load node (%s); running on main' % e)
        return _run_main(a, cmd, co.top)
    if a.node != 'auto':
        node = next((n for n in nodes if n.name == a.node), None)
        if node is None:
            _err('USAGE: no node named %s in the registry' % a.node)
            return USAGE
        if a.repo not in node.repos:
            _err('USAGE: %s has not opted in %s: add it to the node\'s "repos" in the registry'
                 % (node.name, a.repo))
            return USAGE
        if co.dirty:
            _err('USAGE: %s has tracked changes not committed; a node runs HEAD: commit first'
                 % co.top)
            return USAGE
        return _on_node(a, node, co, cmd)[0]
    takers = [n for n in nodes if a.repo in n.repos]
    if co.dirty or not takers:
        _say('running on main: %s' % ('tracked changes are not committed' if co.dirty
                                      else 'no node has opted in %s' % a.repo))
        return _run_main(a, cmd, co.top)
    results = remote.query_all(takers, runner=runner or remote.default_runner)
    try:
        main_lock = _main_lock()
    except Exception as e:
        _say('the main machine\'s lock cannot be read (%s); running on main' % type(e).__name__)
        return _run_main(a, cmd, co.top)
    choice = route.choose(takers, results, a.repo, main_lock, need)
    _say('%s: %s' % ('running on main' if choice.node is None else 'running on ' + choice.where,
                     choice.why))
    if choice.node is None:
        return _run_main(a, cmd, co.top)
    code, fallback = _on_node(a, choice.node, co, cmd)
    if fallback:
        _say('running on main instead')
        return _run_main(a, cmd, co.top)
    return code


def _named(a):
    """The registry Node named a.node, or None (said)."""
    try:
        nodes = _load(a)
    except registry.RegistryError as e:
        _err('REFUSED: %s' % e)
        return None
    node = next((n for n in nodes if n.name == a.node), None)
    if node is None:
        _err('USAGE: no node named %s in the registry' % a.node)
        return None
    if not box.JOB_RE.fullmatch(a.job):
        _err('USAGE: %s is not a job id (j-YYYYmmddTHHMMSSZ-xxxxxx)' % a.job)
        return None
    return node


def cmd_follow(a):
    node = _named(a)
    if node is None:
        return USAGE
    co = None
    if a.artifact:
        try:
            co = job.checkout(os.path.abspath(a.cwd or os.getcwd()))
        except job.JobError as e:
            _err('USAGE: %s' % e.message)
            return USAGE
    a.kind = None
    try:
        with _link(node) as link:
            nj = _node_job(a, node, link, co, job_id=a.job)
            res = nj.follow()
            if res is None:
                for line in nj.lost_lines():
                    _say(line)
                return job.EXIT_LOST
            return nj.finish(res)
    except job.JobError as e:
        _say(e.message)
        return e.code
    except OSError as e:
        _say('%s could not be used: %s' % (node.name, e))
        return job.EXIT_NODE


def cmd_stop(a):
    node = _named(a)
    if node is None:
        return USAGE
    a.artifact, a.reattach, a.kind = [], 0, None
    try:
        with _link(node) as link:
            d = _node_job(a, node, link, None, job_id=a.job).stop()
    except job.JobError as e:
        _say(e.message)
        return e.code
    except OSError as e:
        _say('%s could not be used: %s' % (node.name, e))
        return job.EXIT_NODE
    if d.get('ok'):
        print('%s: job %s: %s' % (node.name, a.job, 'had ended already' if d.get('ended')
                                  else 'asked to stop (%s)' % d.get('asked')))
        return OK
    _say('%s refused: %s' % (node.name, str(d.get('error'))[:300]))
    return FAILED


def main(argv=None, runner=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    i = argv.index('--') if '--' in argv else len(argv)
    head, cmd = argv[:i], argv[i + 1:]
    a = parser().parse_args(head)
    if a.verb == 'run':
        return cmd_run(a, cmd, runner)
    if cmd:
        _err('USAGE: only run takes a command after --')
        return USAGE
    if a.verb == 'follow':
        return cmd_follow(a)
    if a.verb == 'stop':
        return cmd_stop(a)
    try:
        nodes = _load(a)
    except registry.RegistryError as e:
        _err('REFUSED: %s' % e)
        return USAGE
    if a.verb == 'list':
        if not nodes:
            print('no load nodes in the registry')
        for line in registry.list_lines(nodes, resolve=remote.identity_path):
            print(line)
        return OK
    known = {n.name for n in nodes}
    unknown = [x for x in a.names if x not in known]
    if unknown:
        _err('USAGE: no node named %s in the registry' % ', '.join(sorted(set(unknown))))
        return USAGE
    chosen = [n for n in nodes if not a.names or n.name in a.names]
    results = remote.query_all(chosen, runner=runner or remote.default_runner, timeout_s=a.timeout)
    if a.json:
        print(json.dumps({r.name: {'state': r.state, 'detail': r.detail, 'reply': r.reply}
                          for r in results}, indent=1, sort_keys=True))
    else:
        if not chosen:
            print('no load nodes in the registry')
        for line in reply.status_lines(results, nodes, reply.kit_version(KIT_ROOT)):
            print(line)
    return OK if all(r.state == remote.OK for r in results) else FAILED


if __name__ == '__main__':
    sys.exit(main())
