"""The gate's steps: built from `.kit.json` "gate", picked by what changed, judged by their logs
(ported from the PM kit tools/gate.py: build_steps, selects, substitute, judge; docs/gate.md).

Pure logic: no process is started here (kitlib.gate.runner runs the steps) and nothing prints.

Selection (`select`):
  diff     a step runs when it is `always`, or a changed path matches one of its `globs` and none
           of its `exclude`, or a changed path matching its `content.globs` holds `content.regex`
           (read from the working tree). `manual` steps never run here.
  full     every step that is not `manual`; build(full=True) gives each its `full_cmd` when set,
           and build(daily=True) (the daily full run) its `daily_cmd` before that.
  only     exactly the named steps (manual ones included); an unknown name is a GateConfigError.
  In diff and full, a step is then dropped when a step its `skip_if_selected` names is selected.
Order: `stage` first, main (the default), last; `defer` steps after all of them; config order
within each.

Globs are fnmatch over repo-relative posix paths, case-insensitive; `*` crosses `/`.

Judging (`judge`): a step passes only when its exit status is in `ok_exits` (default [0]), every
`pass` pattern is in its log, no `fail` pattern (its own or the gate's) is, a command step's log
is not empty, the log does not say zero tests ran (`Ran 0 tests`, or a gate `zero_tests`
pattern), and the gate's `errors` policy finds no unexplained error line. Patterns are
multi-line regexes (re.M).

Gate keys this gate does not implement refuse (GateConfigError) when set, rather than being
ignored: `python_tests` "pytests", `python_tests_floor` and `trailers` true (a step's
`kill_retry` is no key at all: kitlib.config refuses it). docs/gate.md "Not ported" says why. A
`--pid-hold` argument in `setup`, a step's `cmd`, `full_cmd`, `daily_cmd` or `merged_args`, or a
landing lane's merged_args refuses too: the pid hold of tools/pytests.py and tools/kit_tests.py
is opt-in for agent runs, never in the gate.
"""
import collections
import fnmatch
import os
import re
import sys

from kitlib.config.load import PLACEHOLDERS

STAGES = ('first', 'main', 'last')
# setup is the configured "setup" argv; gate.log is the console's; windows is the window
# monitor's result (kitlib.gate.runner.window_results)
RESERVED = ('setup', 'gate', 'windows')
RAN = re.compile(r'(?m)^Ran (\d+) tests? in ')
PID_HOLD_FLAG = '--pid-hold'    # kitlib.pytests.runner.PID_HOLD_FLAG (the gate imports no runner)
PID_HOLD_WHY = 'the pid hold is opt-in for agent test runs only, never in the gate'
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KIT_TOOLS = os.path.join(KIT_ROOT, 'tools')     # the gate's own runs: an export when pinned


def kit_live():
    """The kit checkout a printed command names (kitlib.pluginsource.live_root): when this code
    runs from a pinned export of pushed main (kitlib.gate.pin), the checkout that started it,
    since a later prune may remove the export."""
    from kitlib import pluginsource
    return pluginsource.live_root(KIT_ROOT)

Step = collections.namedtuple('Step', 'name cmd check cwd passes fails ok_exits cont warn requires '
                                      'requires_strict')


class GateConfigError(Exception):
    """The gate config asks for something this gate cannot do, or names a step it lacks."""


class UnknownStep(GateConfigError):
    """--only named a step the config does not have (a usage error, not a config error)."""


def _list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def unsupported(gate):
    """The settings of `gate` (the "gate" section over its defaults) this gate refuses."""
    out = []
    if gate.get('python_tests') == 'pytests':
        out.append('"python_tests": "pytests" (write the test run as a step)')
    if gate.get('python_tests_floor') is not None:
        out.append('"python_tests_floor" (put a floor in the step\'s "pass" pattern)')
    if gate.get('trailers'):
        out.append('"trailers": true (add the trailers.py step, docs/repo-checks.md)')
    if PID_HOLD_FLAG in (gate.get('setup') or []):
        out.append('"setup" %s (%s)' % (PID_HOLD_FLAG, PID_HOLD_WHY))
    for s in gate.get('steps') or []:
        for key in ('cmd', 'full_cmd', 'daily_cmd', 'merged_args'):
            if PID_HOLD_FLAG in (s.get(key) or []):
                out.append('step %s "%s" %s (%s)' % (s.get('name'), key, PID_HOLD_FLAG,
                                                     PID_HOLD_WHY))
    return out


def mapping(root, base, tools_dir):
    """The placeholder values (kitlib.config.load.PLACEHOLDERS)."""
    m = {'python': sys.executable, 'root': root, 'base': base or '', 'here': KIT_TOOLS,
         'tools': os.path.join(root, tools_dir)}
    assert set(m) == set(PLACEHOLDERS)
    return m


def substitute(argv, values):
    out = []
    for arg in argv:
        for key, value in values.items():
            arg = arg.replace('{%s}' % key, value)
        out.append(arg)
    return out


def argv_of(s, full=False, daily=False):
    """The argv key a cmd step runs: in a daily run (kitlib.dailyfull) its `daily_cmd`, else
    its `full_cmd`, else `cmd`; in any other full run its `full_cmd`, else `cmd`; otherwise
    `cmd`."""
    if daily and s.get('daily_cmd'):
        return s['daily_cmd']
    if (full or daily) and s.get('full_cmd'):
        return s['full_cmd']
    return s['cmd']


def build(gate, root, base, tools_dir, merged=False, extra=None, full=False, daily=False):
    """[Step] for every configured step, in config order, plus `setup` first when configured.
    `full`: a step's `full_cmd`, when it has one, is its argv instead of `cmd` (a full run: a
    step whose cmd selects by the change runs everything). `daily` (the daily full run): its
    `daily_cmd` first, then `full_cmd`, then `cmd` (argv_of). `merged`: a step's merged_args are
    appended (the landing's merged-tree gate), then `extra` (a landing lane's merged_args),
    substituted alike."""
    bad = unsupported(gate)
    if PID_HOLD_FLAG in (extra or []):
        bad.append('a landing lane\'s merged_args %s (%s)' % (PID_HOLD_FLAG, PID_HOLD_WHY))
    if bad:
        raise GateConfigError('this gate does not implement %s' % '; '.join(bad))
    values = mapping(root, base, tools_dir)
    top_fails = _list(gate.get('fail'))
    steps = []
    if gate.get('setup'):
        steps.append(Step('setup', substitute(gate['setup'], values), None, root, [], top_fails,
                          (0,), False, False, [], False))
    for s in gate.get('steps') or []:
        if s['name'] in RESERVED:
            raise GateConfigError('step name %r is reserved (%s)' % (s['name'], ', '.join(RESERVED)))
        cmd = None
        if s.get('cmd'):
            argv = argv_of(s, full, daily)
            cmd = substitute(list(argv) + (list(s.get('merged_args') or []) + list(extra or [])
                                               if merged else []), values)
        cwd = os.path.normpath(os.path.join(root, s['cwd'])) if s.get('cwd') else root
        requires = [r if isinstance(r, str) else substitute(r, values) for r in s.get('requires') or []]
        steps.append(Step(s['name'], cmd, s.get('check'), cwd, _list(s.get('pass')),
                          _list(s.get('fail')) + top_fails, tuple(s.get('ok_exits') or (0,)),
                          bool(s.get('continue')), bool(s.get('warn')), requires,
                          bool(s.get('requires_strict'))))
    return steps


def _match(path, globs):
    p = path.replace('\\', '/').lower()
    return any(fnmatch.fnmatchcase(p, g.replace('\\', '/').lower()) for g in globs or ())


def _content_hit(content, changed, read):
    if not content:
        return False
    rx = re.compile(content['regex'], re.M)
    for p in changed:
        if _match(p, content['globs']):
            text = read(p)
            if text is not None and rx.search(text):
                return True
    return False


def working_tree_reader(root):
    def read(rel):
        try:
            with open(os.path.join(root, rel), encoding='utf-8', errors='replace') as f:
                return f.read()
        except OSError:
            return None
    return read


def selects(spec, changed, read):
    """Why the step `spec` (its config object) is selected by `changed`, or None."""
    if spec.get('manual'):
        return None
    if spec.get('always'):
        return 'always'
    for p in changed:
        if _match(p, spec.get('globs')) and not _match(p, spec.get('exclude')):
            return 'changed %s' % p
    if _content_hit(spec.get('content'), changed, read):
        return 'content'
    return None


def select(gate, steps, changed, full=False, only=None, read=None):
    """`(the steps to run in order, {name: why})`. GateConfigError for an --only name no step has."""
    specs = {s['name']: s for s in gate.get('steps') or []}
    by_name = {s.name: s for s in steps}
    why = {}
    if only:
        unknown = [n for n in only if n not in by_name]
        if unknown:
            raise UnknownStep('no step named %s (steps: %s)' % (
                ', '.join(unknown), ', '.join(by_name) or 'none'))
        why = {n: 'named' for n in only}
    else:
        for name, spec in specs.items():
            if full:
                if not spec.get('manual'):
                    why[name] = 'full'
            else:
                reason = selects(spec, changed, read or (lambda rel: None))
                if reason:
                    why[name] = reason
        dropped = [n for n in why if any(o in why for o in specs[n].get('skip_if_selected') or ())]
        for n in dropped:
            del why[n]
        if 'setup' in by_name and why:
            why['setup'] = 'setup'

    def order(step):
        spec = specs.get(step.name, {})
        stage = 0 if step.name == 'setup' else 1 + STAGES.index(spec.get('stage') or 'main')
        return (1 if spec.get('defer') else 0, stage)
    chosen = sorted((s for s in steps if s.name in why), key=order)
    return chosen, why


# ---------------------------------------------------------------- judging

def _errors_problems(text, policy, fails):
    if not policy:
        return []
    match = re.compile(policy['match'])
    excuses = [re.compile(x) for x in _list(policy.get('excuse')) + list(fails)]
    begin, end = (policy.get('expect') or (None, None))
    inside, out = False, []
    for line in text.splitlines():
        if begin is not None and line.strip() == begin:
            if inside:
                out.append('an expected-errors window opens twice (%r)' % begin)
            inside = True
            continue
        if end is not None and line.strip() == end:
            if not inside:
                out.append('an expected-errors window closes unopened (%r)' % end)
            inside = False
            continue
        if not inside and match.search(line) and not any(x.search(line) for x in excuses):
            out.append('unexplained error line: %s' % line.strip()[:200])
    if inside:
        out.append('an expected-errors window never closes (%r)' % end)
    return out


def judge(step, text, rc, gate):
    """`(status, reasons)`: status 'PASS', 'WARN' or 'FAIL'."""
    why = []
    if rc not in step.ok_exits:
        why.append('exit %s' % rc)
    if step.cmd is not None and not text.strip():
        why.append('empty log: a run that never happened')
    for p in step.passes:
        if not re.search(p, text, re.M):
            why.append('missing pass pattern %r' % p)
    for p in step.fails:
        if re.search(p, text, re.M):
            why.append('matched fail pattern %r' % p)
    ran = [int(n) for n in RAN.findall(text)]
    if ran and ran[-1] == 0:
        why.append('ran zero tests (`Ran 0 tests`): a filter that matched nothing is not a pass')
    for p in _list(gate.get('zero_tests')):
        m = re.search(p, text, re.M)
        if m:
            why.append('reported zero tests run (%r)' % m.group(0).strip())
    why.extend(_errors_problems(text, gate.get('errors'), step.fails))
    if why:
        return ('WARN' if step.warn else 'FAIL'), why
    warn = [ln for ln in text.splitlines() for p in _list(gate.get('warn_lines')) if re.search(p, ln)]
    if warn:
        return 'WARN', ['warn line: %s' % ln.strip()[:200] for ln in warn[:10]]
    return 'PASS', []
