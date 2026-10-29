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
      each GLOB, relative to the checkout's top, ASCII) are copied back into the checkout, all
      or none
  node.py gate [--node auto|NAME] [--note TEXT] [--reattach S] [-C DIR]
      the checkout's HEAD gated `--full` on a node by the node's own kit, against the default base;
      the gate-OK record it writes is checked here and taken into this machine's record folder
      (auto: the main machine's own gate runs when no node that serves `gate` has the gate's
      kind free; a kit checkout always gates on main, with its own tools/gate.py)
  node.py follow NODE JOB [--artifact GLOB]... [-C DIR] [--reattach S]
      stream a job's output from its start to its end, then fetch its artifacts (a gate job: take
      its record into the checkout -C names) and clean it
  node.py stop NODE JOB
      ask a node to stop a job

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.loadnode, prints and exits.
Exit codes, list and status: 0 every node asked answered; 1 at least one did not (every node is
still listed); 2 usage, or a registry that is missing, invalid or not ignored by git.
run, gate and follow: the command's own exit code (gate: the gate's); 2 usage (an unknown node or
kind, a repository the node has not opted in, tracked changes not committed for --node NAME, a
kit checkout gated on a named node); 6 the node was busy; 9 the
connection was lost (the job runs on: the follow and stop lines are said); 10 the node could not
be used (nothing ran); 13 the job succeeded but its artifacts did not all come or could not all be
copied in (none was); 14 the node started the job but it ended there with no exit code of its own
or could not be followed; 15 a gate passed on the node but its gate-OK record was not taken here
(the job is kept: the follow line, with -C, takes it again); 130 Ctrl+C (a started job is asked to stop). With --node auto a node
that fails before its job starts, or is busy, gives way to the main machine; a job the node
started never runs again on main. stop: 0 asked, 1 refused.
"""
import argparse
import json
import os
import re
import sys

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT_ROOT)

from kitlib.loadnode import box, dispatch, job, registry, remote, reply, route  # noqa: E402

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
        raise argparse.ArgumentTypeError('%r is not a relative glob under the checkout (%s)'
                                         % (text, box.GLOB_RULE))
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
    sub = p.add_subparsers(dest='verb', metavar='{list,status,run,gate,follow,stop}',
                           parser_class=Parser)
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
    g = sub.add_parser('gate', help='gate HEAD --full on a node, or on main', allow_abbrev=False)
    g.add_argument('--node', default='auto', help='a node\'s name, or auto (default)')
    g.add_argument('--note')
    _follow_args(g)
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
    return dispatch.notice_dir(kind)


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


def _is_kit(top):
    from kitlib.content import oncommit
    return oncommit.is_kit(top)


def _gate_facts(top):
    from kitlib.loadnode import gaterecord
    return gaterecord.gate_facts(top)


def _repo_of(top):
    from kitlib.loadnode import gaterecord
    return gaterecord.repo_of(top)


def _importer():
    from kitlib.loadnode import gaterecord
    return gaterecord.import_gate_record


def _run_gate_main(top):
    """The main machine's own `gate.py -C top --full` (it books the machine lock itself): its exit
    code. A kit checkout's gate is its own tools/gate.py, else this kit's."""
    from kitlib.proc import spawn
    kit = top if _is_kit(top) else KIT_ROOT
    try:
        return spawn.call([sys.executable, os.path.join(kit, 'tools', 'gate.py'), '-C', top,
                           '--full'])
    except KeyboardInterrupt:
        return 130


# -- the verbs -------------------------------------------------------------------------------------

def _load(a):
    return registry.load(a.registry if a.registry else registry.path())


def _node_job(a, node, link, co, cmd=None, job_id=None, gate=None):
    return job.NodeJob(node, link, co.top if co else None, co.sha if co else None,
                       getattr(a, 'repo', None), getattr(a, 'slug', None), getattr(a, 'kind', None),
                       cmd or [], a.artifact, wait_s=getattr(a, 'wait_s', None),
                       expect_s=getattr(a, 'expect_s', None), no_wait=getattr(a, 'no_wait', False),
                       note=getattr(a, 'note', None), say=_say, out=_out,
                       notice_dir=_notice_dir(a.kind) if cmd else None, reattach_s=a.reattach,
                       job=job_id, tool=TOOL, gate=gate, importer=_importer())


def _on_node(a, node, co, cmd):
    """(exit code, fallback): kitlib.loadnode.dispatch.on_node."""
    return dispatch.on_node(node, _link, lambda link: _node_job(a, node, link, co, cmd), _say)


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


def cmd_gate(a, runner=None):
    try:
        co = job.checkout(os.path.abspath(a.cwd or os.getcwd()))
    except job.JobError as e:
        _err('USAGE: %s' % e.message)
        return USAGE
    if _is_kit(co.top):
        if a.node != 'auto':
            _err('USAGE: %s is a kit checkout: its gate is its own tools/gate.py, never a node\'s '
                 'installed kit; run node.py gate without --node' % co.top)
            return USAGE
        _say('gating on main: a kit checkout gates with its own tools/gate.py')
        return _run_gate_main(co.top)
    try:
        facts = _gate_facts(co.top)
    except job.JobError as e:
        if a.node != 'auto':
            _err('USAGE: %s' % e.message)
            return USAGE
        _say('gating on main: %s' % e.message)
        return _run_gate_main(co.top)
    a.repo, a.slug, a.kind, a.artifact = facts['repo'], facts['slug'], facts['kind'], []
    a.wait_s, a.expect_s, a.no_wait = None, None, False
    gate = {k: facts[k] for k in ('base', 'remote', 'main')}
    try:
        nodes = _load(a)
    except registry.RegistryError as e:
        if a.node != 'auto':
            _err('REFUSED: %s' % e)
            return USAGE
        _say('no load node (%s); gating on main' % e)
        return _run_gate_main(co.top)
    if not registry.repo_ok(a.repo):
        if a.node != 'auto':
            _err('USAGE: %r is not a repo name a node takes (letters, digits, "_", "." and "-")'
                 % a.repo)
            return USAGE
        _say('gating on main: %r is not a repo name a node takes' % a.repo)
        return _run_gate_main(co.top)
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
            _err('USAGE: %s has tracked changes not committed; a node gates HEAD: commit first'
                 % co.top)
            return USAGE
        return _on_gate(a, node, co, gate)[0]
    takers = [n for n in nodes if a.repo in n.repos]
    if co.dirty or not takers:
        _say('gating on main: %s' % ('tracked changes are not committed' if co.dirty
                                     else 'no node has opted in %s' % a.repo))
        return _run_gate_main(co.top)
    try:
        need = _need(a.kind)
    except Exception as e:
        _say('the gate\'s kind %s cannot be sized here (%s); gating on main' % (a.kind, e))
        return _run_gate_main(co.top)
    results = remote.query_all(takers, runner=runner or remote.default_runner)
    by_name = {r.name: r for r in results}
    gaters = []
    for n in takers:
        r = by_name.get(n.name)
        if r is not None and r.reply and box.GATE_VERB not in (r.reply.get('verbs') or ()):
            _say('%s: its kit serves no gate verb (update it)' % n.name)
            continue
        gaters.append(n)
    try:
        main_lock = _main_lock()
    except Exception as e:
        _say('the main machine\'s lock cannot be read (%s); gating on main' % type(e).__name__)
        return _run_gate_main(co.top)
    choice = route.choose(gaters, results, a.repo, main_lock, need)
    _say('%s: %s' % ('gating on main' if choice.node is None else 'gating on ' + choice.where,
                     choice.why))
    if choice.node is None:
        return _run_gate_main(co.top)
    code, fallback = _on_gate(a, choice.node, co, gate)
    if fallback:
        _say('gating on main instead')
        return _run_gate_main(co.top)
    return code


def _on_gate(a, node, co, gate):
    """(exit code, fallback) of a gate job on `node`: kitlib.loadnode.dispatch.on_node."""
    return dispatch.on_node(node, _link, lambda link: _node_job(a, node, link, co, None, gate=gate),
                            _say)


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
    if a.artifact or a.cwd:         # -C: a gate job's record is taken into this checkout
        try:
            co = job.checkout(os.path.abspath(a.cwd or os.getcwd()))
        except job.JobError as e:
            _err('USAGE: %s' % e.message)
            return USAGE
        co = co._replace(sha=None)  # the job's commit is the node's to say, not HEAD now
        a.repo = _repo_of(co.top)
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
    if a.verb == 'gate' and not cmd:
        return cmd_gate(a, runner)
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
