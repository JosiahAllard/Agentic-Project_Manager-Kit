"""tools/machine_lock.py has kitlib.proc.tcp loaded once it is loaded, and after that load no
function-level import in the entry point itself, or in any kitlib.lock or kitlib.proc module it
loaded, names a kitlib module that is not loaded. The check is per module, not per call path: it
reads every function of those modules, reached by a run or not, and no module outside them. Why:
kitlib.proc.daemons imports kitlib.proc.tcp inside why_left and stop, at the end of a run, and a
first load then could read a checkout a landing has rewritten.

Each test loads the entry point in a child interpreter, because this suite's own sys.modules
already holds what other tests imported. The child only executes the module under a name that is
not __main__ (as tests/lock/test_cli.py does): no main(), no store, no lock, no daemon, and it runs
on the test process's environment, unchanged."""
import importlib.util
import os
import unittest

from tests.lazyimports import ROOT, child_loaded, late_kit_imports

LOAD = """
import importlib.util, os
spec = importlib.util.spec_from_file_location(
    'machine_lock_under_test', os.path.join('tools', 'machine_lock.py'))
spec.loader.exec_module(importlib.util.module_from_spec(spec))
"""
ENTRY = 'tools.machine_lock'        # the entry point's source, found by name and never executed here
PACKAGES = ('kitlib.lock', 'kitlib.proc')


def lock_and_proc(loaded):
    return sorted(m for m in loaded
                  if m in PACKAGES or m.startswith(tuple(p + '.' for p in PACKAGES)))


class CliImportsTest(unittest.TestCase):
    def test_the_cli_has_loaded_tcp_before_any_run(self):
        loaded = child_loaded(LOAD)
        self.assertIn('kitlib.lock.run', loaded)        # the child did load the entry point
        self.assertEqual([m for m in ('kitlib.proc.tcp',) if m not in loaded], [])

    def test_no_lock_or_proc_module_imports_a_kit_module_late(self):
        loaded = child_loaded(LOAD)
        names = lock_and_proc(loaded)
        self.assertIn('kitlib.proc.daemons', names)     # the module whose late import this guards
        entry = importlib.util.find_spec(ENTRY)         # the walk below reads this very file
        self.assertEqual(os.path.normcase(os.path.abspath(entry.origin)),
                         os.path.normcase(os.path.join(ROOT, 'tools', 'machine_lock.py')))
        self.assertEqual(late_kit_imports([ENTRY] + names, loaded), [])


if __name__ == '__main__':
    unittest.main()
