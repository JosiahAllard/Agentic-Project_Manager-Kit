"""Every kitlib module imports on any platform (docs/linux-support-plan.md section 2.1, rule 3):
no DLL is loaded and no Windows-only module (msvcrt, winreg, ctypes.WINFUNCTYPE) is reached at
import, so a missing piece fails at its first call, naming it.

The walk runs in a fresh interpreter, so it imports each module from nothing, the way a tool's
first `import` does, and no module this test run already loaded hides a failure. On Windows it
shows nothing regressed; on Linux it is the proof the seam exists."""
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WALK = r'''
import importlib, json, os, sys, traceback
root = sys.argv[1]
sys.path.insert(0, root)
failed = {}
count = 0
for folder, dirs, files in os.walk(os.path.join(root, 'kitlib')):
    dirs[:] = sorted(d for d in dirs if d != '__pycache__' and not d.startswith('.'))
    rel = os.path.relpath(folder, root).replace(os.sep, '.')
    for name in sorted(files):
        if not name.endswith('.py'):
            continue
        module = rel if name == '__init__.py' else rel + '.' + name[:-3]
        count += 1
        try:
            importlib.import_module(module)
        except BaseException as e:
            failed[module] = traceback.format_exception_only(type(e), e)[-1].strip()
print(json.dumps({'count': count, 'failed': failed}))
'''


def walk():
    """{count, failed: {module: error}} from importing every kitlib module in a new interpreter."""
    done = subprocess.run([sys.executable, '-c', WALK, ROOT], cwd=ROOT, capture_output=True,
                          text=True, timeout=300, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    if done.returncode != 0:
        raise AssertionError('the import walk failed (%d): %s' % (done.returncode, done.stderr[-2000:]))
    return json.loads(done.stdout.strip().splitlines()[-1])


def tk_missing():
    """Why this interpreter has no Tk (a Linux python without its Tk library: python:3.14-slim,
    or no python3-tk package), or None when tkinter imports. That is the machine, not the kit."""
    try:
        import tkinter      # noqa: F401
    except ImportError as e:
        return str(e)
    return None


TK = re.compile(r'tkinter|libtk|libtcl')


class EveryModuleImports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = walk()

    def test_every_kitlib_module_imports(self):
        result = self.result
        self.assertGreater(result['count'], 100)
        failed = result['failed']
        if tk_missing():
            failed = {m: e for m, e in failed.items() if not TK.search(e)}
        self.assertEqual(failed, {}, '%d of %d kitlib modules do not import on %s'
                         % (len(failed), result['count'], sys.platform))

    def test_the_tk_windows_import_where_tk_is_installed(self):
        missing = tk_missing()
        if missing:
            self.skipTest('this python has no Tk (%s): the walk leaves out what fails for it' % missing)
        self.assertEqual({m: e for m, e in self.result['failed'].items() if TK.search(e)}, {})


if __name__ == '__main__':
    unittest.main()
