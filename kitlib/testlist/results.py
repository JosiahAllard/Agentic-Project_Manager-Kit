"""Every test name in the JUnit XML and .NET `.trx` result files under a folder, sorted, with
each name's status when asked, and the diff of two lists (docs/test-tools.md, testlist).

WHERE RESULTS ARE: the checkout's `.kit.json` "testlist".junit (globs under the folder, default
`**/build/test-results/**/*.xml`, Gradle's) and .trx (default `**/*.trx`). A result file under a
host's own folder (kitlib.host.registry.host_folders; `.claude` on Claude: agent worktrees) is
skipped unless the folder is itself inside one.
"""
import glob
import io
import os
import xml.etree.ElementTree as ET

from kitlib.config import ConfigError
from kitlib.host import registry

SECTION = 'testlist'
DEFAULTS = {'junit': ['**/build/test-results/**/*.xml'], 'trx': ['**/*.trx']}
TRX_NS = {'t': 'http://microsoft.com/schemas/VisualStudio/TeamTest/2010'}
RANK = {'passed': 0, 'skipped': 1, 'failed': 2}
SHOWN = 40


def settings(cfg):
    """`{'junit': [globs], 'trx': [globs]}` from a kitlib.config Config's "testlist" (the schema
    checks the keys and types at load); ConfigError when both are empty."""
    section = cfg.section(SECTION)
    out = {key: list(section[key]) for key in DEFAULTS}
    if not out['junit'] and not out['trx']:
        raise ConfigError('.kit.json "%s": junit and trx are both empty - nothing could ever '
                          'be read' % SECTION)
    return out


def _files(top, patterns):
    folders = [os.sep + f + os.sep for f in registry.host_folders()]
    inside = any(f in os.path.abspath(top) + os.sep for f in folders)
    seen = set()
    for pattern in patterns:
        for path in glob.glob(os.path.join(top, pattern), recursive=True):
            if path in seen or (not inside and any(f in path for f in folders)):
                continue
            seen.add(path)
            yield path


def load(top, statuses=None, globs=None):
    """`(sorted test names under top, newest result file's mtime or 0.0)`. `statuses`, when a dict,
    also gets name -> 'passed' / 'failed' / 'skipped' (the worst seen for a repeated name)."""
    globs = DEFAULTS if globs is None else globs
    tests, newest = set(), 0.0

    def status(name, st):
        if statuses is not None and RANK[st] >= RANK[statuses.get(name, 'passed')]:
            statuses[name] = st
    for path in _files(top, globs['junit']):
        newest = max(newest, os.path.getmtime(path))
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for case in root.iter('testcase'):
            name = '%s.%s' % (case.get('classname'), case.get('name'))
            tests.add(name)
            bad = case.find('failure') if case.find('failure') is not None else case.find('error')
            status(name, 'passed')
            if bad is not None:
                status(name, 'failed')
            if case.find('skipped') is not None:
                status(name, 'skipped')
    for path in _files(top, globs['trx']):
        newest = max(newest, os.path.getmtime(path))
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for r in root.iterfind('.//t:UnitTestResult', TRX_NS):
            name = r.get('testName')
            tests.add(name)
            status(name, 'passed')
            if r.get('outcome') == 'Failed':
                status(name, 'failed')
            elif r.get('outcome') == 'NotExecuted':
                status(name, 'skipped')
    return sorted(tests), newest


def load_names(top, statuses=None, globs=None):
    """Sorted test names under `top` (`load` without the mtime)."""
    return load(top, statuses, globs)[0]


def read_list(path):
    with io.open(path, encoding='utf-8') as fh:
        return set(fh.read().split('\n')) - {''}


def write_list(path, names):
    with io.open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(names) + '\n')


def diff(old, new):
    """`(lines, code)`: the counts, then the names removed (`  - `) and added (`  + `), at most
    SHOWN of each; code 1 when any differ, else 0."""
    added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
    lines = ['names before %d | after %d | added %d | removed %d'
             % (len(old), len(new), len(added), len(removed))]
    lines += ['  - ' + n for n in removed[:SHOWN]] + ['  + ' + n for n in added[:SHOWN]]
    return lines, (1 if added or removed else 0)
