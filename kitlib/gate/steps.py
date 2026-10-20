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
  docs     exactly the `docs_only`.steps (a docs-only change, docs_only(); docs/gate.md
           "Docs-only changes").
  In diff and full, a step is then dropped when a step its `skip_if_selected` names is selected.
  Diff mode then adds every `smoke_steps` step not already selected (reason `smoke`, manual ones
  included, never dropped), so a landing can take its record (docs/gate.md "Landing"). A
  `smoke_steps` or `docs_only`.steps name no step has, or a `docs_only` with no steps, is a
  GateConfigError (build).
Order: `stage` first, main (the default), last; `defer` steps after all of them; config order
within each.

Globs are fnmatch over repo-relative posix paths, case-insensitive; `*` crosses `/`.

Judging (`judge`): a step passes only when its exit status is in `ok_exits` (default [0]), every
`pass` pattern is in its log, no `fail` pattern (its own or the gate's) is, a command step's log
is not empty, the log does not say zero tests ran (`Ran 0 tests`, or a gate `zero_tests`
pattern), and the gate's `errors` policy finds no unexplained error line. Patterns are
multi-line regexes (re.M). `zero_count_passes` lists the `pass` patterns that would also match a
zero-test run (a warning: judge's zero rules are then the only backstop).

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
import warnings

from kitlib.config.load import CONFIG_NAME, PLACEHOLDERS

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


ZERO_RAN = 'Ran 0 tests in 0.000s'     # the zero-count line judge() fails on (RAN)
SAMPLE_MAX = 256                        # _walk gives up on a longer sample (`\d{2000000000}`)


def _regex_parser():
    """(constants, parser) of the stdlib regex parser that zero_count_passes reads a pattern's
    shape with: re._constants and re._parser (Python 3.11 on), else sre_constants and sre_parse
    (before 3.11, deprecated after), else None. Private modules: imported here, when called, so
    a Python without them loses only the warning, never the gate."""
    try:
        from re import _constants, _parser
        return _constants, _parser
    except ImportError:
        pass
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            import sre_constants
            import sre_parse
        return sre_constants, sre_parse
    except ImportError:
        return None


def _class_has(_C, items, c):
    """True when the parsed character class `items` (an IN of parser constants `_C`) holds
    `c`."""
    cats = {_C.CATEGORY_DIGIT: c.isdigit(), _C.CATEGORY_NOT_DIGIT: not c.isdigit(),
            _C.CATEGORY_SPACE: c.isspace(), _C.CATEGORY_NOT_SPACE: not c.isspace(),
            _C.CATEGORY_WORD: c.isalnum() or c == '_',
            _C.CATEGORY_NOT_WORD: not (c.isalnum() or c == '_')}
    negate, hit = False, False
    for op, av in items:
        if op is _C.NEGATE:
            negate = True
        elif op is _C.LITERAL:
            hit = hit or av == ord(c)
        elif op is _C.RANGE:
            hit = hit or av[0] <= ord(c) <= av[1]
        elif op is _C.CATEGORY:
            hit = hit or cats[av]
        else:
            raise ValueError(op)
    return hit != negate


def _walk(_C, items, out):
    """Append to `out` one (char, free) per character of a shortest text the parsed pattern
    `items` (parser constants `_C`) matches, taking '0' wherever a digit may stand; `free` is
    True for a character a digit class chose (`\\d`, `[0-9]`: a count), False for a literal or
    a class that takes letters too (`\\S`, `.`). A repeat is walked its minimum times, except
    that one which may be empty and is the count itself (only digit classes that take 0:
    `\\d*`) is walked once ('0'); an optional group (`(?:\\d+ skipped, )?`) or a digit that
    cannot be 0 (`[1-9]?`) is left out. A repeat whose text
    would pass SAMPLE_MAX characters raises ValueError: no sample, never a hang."""
    repeats = (_C.MAX_REPEAT, _C.MIN_REPEAT, getattr(_C, 'POSSESSIVE_REPEAT', None))
    for op, av in items:
        if op is _C.LITERAL:
            out.append((chr(av), False))
        elif op is _C.NOT_LITERAL or op is _C.ANY:
            out.append(('y' if op is _C.NOT_LITERAL and av == ord('x') else 'x', False))
        elif op is _C.IN:
            out.append((next(c for c in '0aA 1x_-' if _class_has(_C, av, c)),
                        not _class_has(_C, av, 'a')))
        elif op in repeats:
            body = []                   # walked once: the walk gives the same text every time
            _walk(_C, av[2], body)
            if av[0]:
                if len(out) + len(body) * av[0] > SAMPLE_MAX:
                    raise ValueError('sample longer than %d' % SAMPLE_MAX)
                out.extend(body * av[0])
            elif body and all(free and c == '0' for c, free in body):
                out.extend(body)
        elif op is _C.SUBPATTERN:
            _walk(_C, av[-1], out)
        elif op is getattr(_C, 'ATOMIC_GROUP', None):
            _walk(_C, av, out)
        elif op is _C.BRANCH:
            _walk(_C, av[1][0], out)
        elif op is not _C.AT:
            raise ValueError(op)


def _sample(parser, pattern):
    """`[(char, free)]` of a short text `pattern` matches (re.M), each count 0 where it may be,
    or None when none could be built (an unhandled construct, or a text it does not match).
    `parser` is _regex_parser()'s pair."""
    out = []
    try:
        _walk(parser[0], parser[1].parse(pattern), out)
        text = ''.join(c for c, _ in out)
        return out if re.search(pattern, text, re.M) else None
    except (ValueError, StopIteration, KeyError, re.error, TypeError, IndexError,
            OverflowError, RecursionError, MemoryError):   # deep nesting: parse, re.search
        return None


def _first_count_zero(sample):
    """True when the first number a class chose in `sample` (a count, not a literal digit) is
    0."""
    run = ''
    for c, free in sample + [('', False)]:
        if free and c.isdigit():
            run += c
        elif run:
            return int(run) == 0
    return False


def zero_count_passes(gate):
    """One line per step `pass` pattern of `gate` that accepts a zero-test run: it matches the
    `Ran 0 tests` line judge() fails on, or a line a gate `zero_tests` pattern matches (each
    pattern's shortest text, counts 0), or its own first count may be 0 (`\\d+`). judge()
    still fails such a run when it says zero (`Ran 0 tests` or zero_tests); a pattern with a
    floor (`[1-9]\\d*`) needs no backstop. A warning only: nothing refuses on it, and with no
    stdlib regex parser (_regex_parser) it gives []; a pattern that is no string is skipped.
    Its caller is kitlib.gate.runner's zero_count_warnings, a branch gate's WARNING lines only
    (never a landing's)."""
    parser = _regex_parser()
    if parser is None:
        return []
    zero_lines = [ZERO_RAN]
    for z in _list(gate.get('zero_tests')):
        s = _sample(parser, z) if isinstance(z, str) else None
        if s:
            zero_lines.append(''.join(c for c, _ in s))
    out = []
    for step in gate.get('steps') or []:
        for p in _list(step.get('pass')):
            if not isinstance(p, str):      # config refuses it; skipped, it hides no other line
                continue
            try:
                hit = next((z for z in zero_lines if re.search(p, z, re.M)), None)
            except (re.error, OverflowError, RecursionError, MemoryError):
                continue
            own = _sample(parser, p) if hit is None else None
            if own and _first_count_zero(own):
                hit = ''.join(c for c, _ in own)
            if hit is not None:
                out.append('step %s "pass" %r accepts a zero-test run (it matches %r): put a '
                           'floor in it, as [1-9]\\d*' % (step.get('name'), p, hit))
    return out


def docs_steps(gate):
    """The `docs_only`.steps of `gate`: [] when docs_only is unset."""
    return list((gate.get('docs_only') or {}).get('steps') or [])


def unknown_names(gate):
    """What `smoke_steps` and `docs_only`.steps name that no configured step is, and a set
    `docs_only` whose steps are empty or missing (its docs-only gate would run nothing yet write
    a record: a docs-only change skips the tests but keeps the scrub)."""
    names = {s.get('name') for s in gate.get('steps') or []}
    out = []
    if gate.get('docs_only') is not None and not docs_steps(gate):
        out.append('"docs_only".steps is empty: list the repo\'s scrub step in docs_only.steps')
    for key, listed in (('"smoke_steps"', gate.get('smoke_steps') or []),
                        ('"docs_only".steps', docs_steps(gate))):
        missing = [n for n in listed if n not in names]
        if missing:
            out.append('%s names no step: %s' % (key, ', '.join(missing)))
    return out


def docs_only(gate, changed, barred=()):
    """True when the change `changed` is docs-only under `gate` (its `docs_only` set): it changes
    something, and every path matches docs_only.globs and none of its exclude, is not
    `.kit.json`, and is not in `barred` (the caller's paths no docs-only change may touch:
    kitlib.gate.reuse.docs_only_change bars reuse's never and strict paths)."""
    spec = gate.get('docs_only')
    if not spec or not changed:
        return False
    barred = {p.replace('\\', '/').lower() for p in barred}
    for p in changed:
        q = p.replace('\\', '/').lower()
        if q == CONFIG_NAME.lower() or q in barred:
            return False
        if not _match(p, spec.get('globs')) or _match(p, spec.get('exclude')):
            return False
    return True


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
    unknown = unknown_names(gate)
    if unknown:
        raise GateConfigError('%s (steps: %s)' % ('; '.join(unknown), ', '.join(
            s['name'] for s in gate.get('steps') or []) or 'none'))
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


def select(gate, steps, changed, full=False, only=None, read=None, docs=False):
    """`(the steps to run in order, {name: why})`. GateConfigError for an --only name no step has.
    `docs`: a docs-only change (docs_only()): exactly the docs_only.steps, plus `setup`."""
    specs = {s['name']: s for s in gate.get('steps') or []}
    by_name = {s.name: s for s in steps}
    why = {}
    if only:
        unknown = [n for n in only if n not in by_name]
        if unknown:
            raise UnknownStep('no step named %s (steps: %s)' % (
                ', '.join(unknown), ', '.join(by_name) or 'none'))
        why = {n: 'named' for n in only}
    elif docs:
        unknown = [n for n in docs_steps(gate) if n not in by_name]
        if unknown:
            raise GateConfigError('"docs_only".steps names no step: %s' % ', '.join(unknown))
        why = {n: 'docs-only' for n in docs_steps(gate)}
        if 'setup' in by_name and why:
            why['setup'] = 'setup'
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
        if not full:
            for n in gate.get('smoke_steps') or []:
                if n not in by_name:
                    raise GateConfigError('"smoke_steps" names no step: %s' % n)
                why.setdefault(n, 'smoke')
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
