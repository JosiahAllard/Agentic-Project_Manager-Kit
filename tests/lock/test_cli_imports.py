"""tools/machine_lock.py loads at start every kit module a run would otherwise import on its way
out (kitlib.proc.tcp, which kitlib.proc.daemons imports inside why_left and stop), so a landing
that writes the checkout during a run cannot hand the running entry point a late first load of kit
code. Each test loads the entry point in a child interpreter, because this suite's own sys.modules
already holds what other tests imported. The child only executes the module under a name that is
not __main__ (as tests/lock/test_cli.py does): no main(), no store, no lock, no daemon, and it runs
on the test process's environment, unchanged."""
import unittest

from tests.lazyimports import child_loaded, late_kit_imports

LOAD = """
import importlib.util, os
spec = importlib.util.spec_from_file_location(
    'machine_lock_under_test', os.path.join('tools', 'machine_lock.py'))
spec.loader.exec_module(importlib.util.module_from_spec(spec))
"""
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
        self.assertEqual(late_kit_imports(names, loaded), [])


if __name__ == '__main__':
    unittest.main()
