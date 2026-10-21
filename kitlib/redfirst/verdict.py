"""The pure parts of a red-first run: which runner takes a test file (`.kit.json` "redfirst".runners),
the command it runs, and the verdict read from its log and result files (docs/redfirst.md).

A runner is `{glob, kind, ...}`; the first whose glob matches the repo-relative path wins (fnmatch,
case-blind, `*` crosses `/`). Kinds:
  python   `python -m unittest -v <module>[.<filter>]`, cwd the throwaway root, PYTHONPATH its
           `pythonpath` folder; the module is the path relative to that folder, dotted
  gradle   `<gradlew> <project>:test --offline --tests <filter>` (no filter: `*.<FileName>`); the
           verdict from `<dir>/build/test-results/test/TEST-*.xml`
  dotnet   `dotnet test <csproj> --filter <filter>` (a bare name is `FullyQualifiedName~<name>`; no
           filter: `FullyQualifiedName~<FileName>`); the verdict from its .trx
A runner's `args` are appended to its command (a gradle or dotnet no-reuse switch, say).
"""
import fnmatch
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

RED, GREEN, NONE, ERROR = 'RED', 'GREEN', 'NONE', 'ERROR'
EXIT_RED, EXIT_GREEN, EXIT_USAGE, EXIT_UNDECIDED, EXIT_LEFTOVER = 0, 1, 2, 3, 4
KINDS = ('python', 'gradle', 'dotnet')
NEEDS = {'gradle': ('dir', 'project'), 'dotnet': ('csproj',)}


class Usage(Exception):
    """A usage error or a refusal: exit 2, nothing made."""


class Selector(object):
    def __init__(self, text, path, filt, runner, target):
        self.text, self.path, self.filter, self.runner, self.target = text, path, filt, runner, target

    @property
    def kind(self):
        return self.runner['kind']


def stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def matches(path, pattern):
    return fnmatch.fnmatchcase(path.lower(), pattern.replace('\\', '/').lower())


def module_of(path, pythonpath):
    """The unittest module of the repo-relative `path` under the folder `pythonpath`, or None."""
    root = (pythonpath or '.').replace('\\', '/').strip('/')
    rel = path
    if root not in ('', '.'):
        if not path.lower().startswith(root.lower() + '/'):
            return None
        rel = path[len(root) + 1:]
    if not rel.endswith('.py'):
        return None
    parts = rel[:-3].split('/')
    return '.'.join(parts) if all(re.fullmatch(r'[A-Za-z_]\w*', p) for p in parts) else None


def parse_selector(text, runners):
    """A Selector for `--tests` text, or Usage."""
    text = text.strip()                      # a CRLF list leaves a CR on each value
    path, _, filt = text.partition('::')
    filt = filt.strip()
    path = path.replace('\\', '/').strip()
    if not path or path.startswith('/') or '..' in path.split('/') or re.match(r'^[A-Za-z]:', path):
        raise Usage('usage: --tests %r: a repo-relative file path, then optionally ::<filter>' % text)
    for r in runners:
        if not matches(path, r['glob']):
            continue
        missing = [k for k in NEEDS.get(r['kind'], ()) if not r.get(k)]
        if missing:
            raise Usage('usage: the %s runner for %r has no %s (.kit.json "redfirst".runners)'
                        % (r['kind'], r['glob'], ', '.join(missing)))
        if r['kind'] == 'python':
            mod = module_of(path, r.get('pythonpath'))
            if mod is None:
                raise Usage('usage: --tests %r: not a .py file under %s, so no unittest module'
                            % (text, r.get('pythonpath') or '.'))
            return Selector(text, path, filt, r, mod)
        return Selector(text, path, filt, r, r['project'] if r['kind'] == 'gradle' else r['csproj'])
    raise Usage('usage: --tests %r: no runner takes it (.kit.json "redfirst".runners: %s)'
                % (text, ', '.join(r['glob'] for r in runners) or 'none'))


def trx_path(sel, log_dir, n):
    return os.path.join(log_dir, '%d-%s.trx' % (n, stem(sel.path)))


def command(sel, tw, log_dir, n, python=sys.executable):
    """(argv, cwd, env extra) for one selector run in the throwaway tree `tw`. Pure."""
    extra = list(sel.runner.get('args') or [])
    if sel.kind == 'gradle':
        gradlew = os.path.join(tw, 'gradlew.bat' if os.name == 'nt' else 'gradlew')
        return ([gradlew, '%s:test' % sel.target, '--offline', '--tests',
                 sel.filter or '*.' + stem(sel.path)] + extra, tw, {})
    if sel.kind == 'dotnet':
        filt = sel.filter or stem(sel.path)
        if not re.search(r'[=~!&|]', filt):
            filt = 'FullyQualifiedName~' + filt
        return (['dotnet', 'test', os.path.join(tw, *sel.target.split('/')), '--nologo',
                 '--filter', filt, '--results-directory', log_dir,
                 '--logger', 'trx;LogFileName=%s' % os.path.basename(trx_path(sel, log_dir, n))]
                + extra, tw, {})
    pp = (sel.runner.get('pythonpath') or '.').replace('\\', '/')
    name = sel.target + ('.' + sel.filter if sel.filter else '')
    return ([python, '-m', 'unittest', '-v', name] + extra, tw,
            {'PYTHONPATH': os.path.normpath(os.path.join(tw, *pp.split('/'))),
             'PYTHONDONTWRITEBYTECODE': '1'})


_ERROR_AFTER_PATH = r'(?::\d+: error:|\(\d+,\d+\): error CS\d+)'   # javac, csc


def names_copied(line, copied):
    """Whether `line` is a compiler ERROR at one of the `copied` repo-relative paths, named as a
    whole path: javac's `<path>:<n>: error:`, csc's `<path>(<l>,<c>): error CS<n>`, or a Kotlin
    line starting `e: ` with `<path>:<n>`. A warning never counts; separators compare as `/`;
    case-blind on Windows."""
    text = line.replace('\\', '/')
    flags = re.I if os.name == 'nt' else 0
    kotlin = text.lstrip().startswith('e: ')
    for p in copied:
        at = r'(?:^|[/\s])' + re.escape(p.replace('\\', '/'))
        if re.search(at + _ERROR_AFTER_PATH, text, flags):
            return True
        if kotlin and re.search(at + r':\d+', text, flags):
            return True
    return False


def compile_error_lines(log, copied):
    return [l for l in log.splitlines() if names_copied(l, copied)]


def gradle_verdict(rc, log, counts, copied):
    """(verdict, reason). `counts`: (tests, failures + errors) from this run's JUnit XML, or None."""
    if 'No tests found for given includes' in log:
        return NONE, 'no test matched the filter'
    if re.search(r'compile\w*Test\w*\s+FAILED|Execution failed for task \'[^\']*compile\w*Test', log):
        errs = compile_error_lines(log, copied)
        if errs:
            return RED, 'does not compile against the base: %s' % errs[0].strip()
        return ERROR, 'a test compile failed, but no error line names a checked-out file'
    if counts is None:
        return ERROR, 'a JUnit result file of this run could not be read (gradle exited %d)' % rc
    tests, bad = counts
    if tests and bad:
        return RED, '%d of %d test(s) failed' % (bad, tests)
    if tests and rc == 0:
        return GREEN, '%d test(s) passed' % tests
    if rc == 0:
        return NONE, 'no test ran'
    return ERROR, 'gradle exited %d with no test result' % rc


def dotnet_verdict(rc, log, counts, copied):
    """(verdict, reason). `counts`: (total, failed) from this run's .trx, or None."""
    if 'No test matches the given testcase filter' in log:
        return NONE, 'no test matched the filter'
    build_errors = [l for l in log.splitlines() if re.search(r'error CS\d+', l)]
    if build_errors:
        named = [l for l in build_errors if names_copied(l, copied)]
        if named:
            return RED, 'does not compile against the base: %s' % named[0].strip()
        return ERROR, 'the build failed, but no error names a checked-out file: %s' % build_errors[0].strip()
    if counts is None:
        return ERROR, 'dotnet exited %d and wrote no readable .trx in this run' % rc
    total, failed = counts
    if total == 0:
        return NONE, 'no test ran'
    if failed:
        return RED, '%d of %d test(s) failed' % (failed, total)
    if rc == 0:
        return GREEN, '%d test(s) passed' % total
    return ERROR, 'dotnet exited %d with every test passing' % rc


def python_verdict(rc, log):
    m = re.findall(r'^Ran (\d+) tests? in', log, re.M)
    if not m:
        return ERROR, 'unittest printed no summary (exit %d)' % rc
    ran = int(m[-1])
    if ran == 0:
        return NONE, 'no test ran'
    if rc == 0:
        return GREEN, '%d test(s) passed' % ran
    # unittest reports both a module that will not import and a filter naming nothing as one
    # errored `unittest.loader._FailedTest`; only the first is red. Matched at a line start: a
    # failing test's traceback may quote either string.
    # The name may hold stray whitespace (a CR from a CRLF selector), so it is not matched as \S+.
    if re.search(r'^ERROR: .*? \(unittest\.loader\._FailedTest\.', log, re.M):
        if re.search(r'^ImportError: Failed to import test module:', log, re.M):
            why = [l.strip() for l in re.findall(
                r'^(?:ImportError|ModuleNotFoundError): .*$', log, re.M)
                if not l.startswith('ImportError: Failed to import test module:')]
            if why:
                return RED, 'does not import against the base: %s' % why[-1]
            return RED, 'does not import against the base'
        return NONE, 'the filter names nothing in the test file'
    return RED, 'failed (%d test(s) ran)' % ran


def junit_counts(results_dir, since=0.0):
    """(tests, failures + errors) over the TEST-*.xml files written at or after `since`, or None
    when one would not parse or holds a count that is no integer (skipping it could turn a
    failing suite into a GREEN made of the others)."""
    tests = bad = 0
    for f in glob.glob(os.path.join(results_dir, 'TEST-*.xml')):
        try:
            if os.path.getmtime(f) < since:
                continue
        except OSError:
            continue
        try:
            root = ET.parse(f).getroot()
            tests += int(root.get('tests', 0))
            bad += int(root.get('failures', 0)) + int(root.get('errors', 0))
        except (ET.ParseError, OSError, ValueError):
            return None
    return tests, bad


def trx_counts(path, since=0.0):
    """(total, failed + error) from the .trx at `path`, or None: absent, older than `since`,
    unparseable, or a count that is no integer."""
    try:
        if os.path.getmtime(path) < since:
            return None
        root = ET.parse(path).getroot()
        for el in root.iter():
            if el.tag.endswith('Counters'):
                return int(el.get('total', 0)), int(el.get('failed', 0)) + int(el.get('error', 0))
    except (ET.ParseError, OSError, ValueError):
        return None
    return None


def judge(sel, rc, log_text, tw, log_dir, n, copied, since=0.0):
    if sel.kind == 'gradle':
        results = os.path.join(tw, *sel.runner['dir'].replace('\\', '/').split('/'),
                               'build', 'test-results', 'test')
        return gradle_verdict(rc, log_text, junit_counts(results, since), copied)
    if sel.kind == 'dotnet':
        return dotnet_verdict(rc, log_text, trx_counts(trx_path(sel, log_dir, n), since), copied)
    return python_verdict(rc, log_text)


def exit_code(verdicts, cleaned):
    if not cleaned:
        return EXIT_LEFTOVER
    if GREEN in verdicts:
        return EXIT_GREEN
    if not verdicts or any(v != RED for v in verdicts):
        return EXIT_UNDECIDED
    return EXIT_RED
