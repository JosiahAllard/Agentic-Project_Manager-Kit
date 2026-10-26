"""Every kitlib module imports on any platform (docs/linux-support-plan.md section 2.1, rule 3):
no DLL is loaded and no Windows-only module (msvcrt, winreg, ctypes.WINFUNCTYPE) is reached at
import, so a missing piece fails at its first call, naming it.

The walk runs in a fresh interpreter, so it imports each module from nothing, the way a tool's
first `import` does, and no module this test run already loaded hides a failure. On Windows it
shows nothing regressed; on Linux it is the proof the seam exists."""
import json
import os
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


class EveryModuleImports(unittest.TestCase):
    def test_every_kitlib_module_imports(self):
        result = walk()
        self.assertGreater(result['count'], 100)
        self.assertEqual(result['failed'], {}, '%d of %d kitlib modules do not import on %s'
                         % (len(result['failed']), result['count'], sys.platform))


if __name__ == '__main__':
    unittest.main()
