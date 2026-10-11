"""A gate step for Node-only JavaScript tests: runs them, or skips loudly without Node.

usage: python tools/js_tests.py --glob <pattern> <folder> ...   (folders relative to the cwd)

The tests use Node's own modules (node:test), so they need `node` itself.

  a folder with no file matching -> FAIL (exit 1), naming the folder: every configured folder must
                                   hold a test, so a renamed or moved one cannot pass as an empty run
  no `node` on PATH              -> a `WARNING: js-tests SKIPPED, NOT RUN` line naming the files,
                                   exit 0; give the gate step "warn": true so the skip shows in the
                                   verdict and is never silent
  node present                   -> `node --test --test-reporter=tap <files>` (Node 18.15+); PASS
                                   only when it exits 0 AND its TAP summary says `# pass N` with
                                   N > 0 AND `# fail 0` AND, when a `# cancelled N` line is there,
                                   N = 0
Exit status: 0 passed or skipped, 1 failed, 2 a bad command line.
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

SKIP_MARK = 'WARNING: js-tests SKIPPED, NOT RUN'
PASS_MARK = 'js-tests: PASS'
TAP_PASS = re.compile(r'^# pass (\d+)\s*$', re.M)
TAP_FAIL = re.compile(r'^# fail (\d+)\s*$', re.M)
TAP_CANCELLED = re.compile(r'^# cancelled (\d+)\s*$', re.M)


def folder_files(folders, pattern, root='.'):
    """{folder: its test files, relative with `/`, sorted}: `pattern` in each folder (not recursive)."""
    out = {}
    for folder in folders:
        out[folder] = sorted(os.path.relpath(p, root).replace('\\', '/')
                             for p in glob.glob(os.path.join(root, folder, pattern)))
    return out


def verdict(rc, output):
    """(passed, why) for a `node --test` run. Pure."""
    passes = [int(n) for n in TAP_PASS.findall(output)]
    fails = [int(n) for n in TAP_FAIL.findall(output)]
    cancelled = [int(n) for n in TAP_CANCELLED.findall(output)]
    if rc != 0:
        return False, 'node --test exited %d' % rc
    if not passes or not fails:
        return False, 'no TAP summary (`# pass N` and `# fail N`) in the output'
    if fails[-1] != 0:
        return False, '%d test(s) failed' % fails[-1]
    if cancelled and cancelled[-1] != 0:
        return False, '%d test(s) cancelled' % cancelled[-1]
    if passes[-1] == 0:
        return False, 'no test passed: an empty run is not a pass'
    return True, '%d test(s) passed' % passes[-1]


def run_js_tests(folders, pattern, root='.', which=shutil.which, run=subprocess.run, out=print):
    """The step: exit status, printing as it goes (module docstring)."""
    per = folder_files(folders, pattern, root)
    files = sorted({f for fs in per.values() for f in fs})
    empty = [f for f in folders if not per[f]]
    if empty:
        out('js-tests: FAIL - no %s file under %s: every configured folder must hold a test, so a '
            'moved or renamed one cannot pass as an empty run' % (pattern, ', '.join(empty)))
        return 1
    node = which('node')
    if not node:
        out('%s - no node on PATH: %d node --test file(s) under %s did not run (install Node 18.15+ '
            'to run them)' % (SKIP_MARK, len(files), ', '.join(folders)))
        for f in files:
            out('    not run: %s' % f)
        return 0
    version = run([node, '--version'], cwd=root, capture_output=True, text=True,
                  encoding='utf-8', errors='replace')
    out('js-tests: node %s (%s)' % ((version.stdout or '').strip() or '(no version)', node))
    r = run([node, '--test', '--test-reporter=tap'] + files, cwd=root, capture_output=True, text=True,
            encoding='utf-8', errors='replace')
    text = (r.stdout or '') + (r.stderr or '')
    out(text.rstrip())
    ok, why = verdict(r.returncode, text)
    if ok:
        out('%s - %s in %d file(s)' % (PASS_MARK, why, len(files)))
        return 0
    out('js-tests: FAIL - %s' % why)
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--glob', required=True, help='the test file pattern, e.g. *.test.cjs')
    p.add_argument('folders', nargs='+', help='folders (relative to the cwd) holding the tests')
    a = p.parse_args(argv)
    return run_js_tests(a.folders, a.glob)


if __name__ == '__main__':
    sys.exit(main())
