"""Every tests/ folder that holds a test_*.py has an __init__.py.

The gate collector (kitlib/tooldeps/selftree.py, the `tests` loop in the tree builder) collects a
tests/<pkg> folder only when it holds an __init__.py, so a folder without one is silently never run.
selftree does not expose that rule, so `folders_missing_init` mirrors it.
"""
import os
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def folders_missing_init(tests_dir):
    """Folders under `tests_dir` (recursively, skipping __pycache__) with a test_*.py and no __init__.py."""
    bad = []
    for here, dirs, files in os.walk(tests_dir):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        if '__init__.py' not in files and any(f.startswith('test_') and f.endswith('.py') for f in files):
            bad.append(here)
    return sorted(bad)


class TestPackagesHaveInit(unittest.TestCase):
    def test_every_test_folder_has_init(self):
        bad = folders_missing_init(os.path.join(_ROOT, 'tests'))
        self.assertEqual([], bad, 'test folders without __init__.py are never collected by the gate: %s' % bad)

    def test_check_reports_a_folder_missing_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pkg, init in (('good', True), ('bad', False)):
                os.makedirs(os.path.join(tmp, pkg))
                open(os.path.join(tmp, pkg, 'test_x.py'), 'w').close()
                if init:
                    open(os.path.join(tmp, pkg, '__init__.py'), 'w').close()
            os.makedirs(os.path.join(tmp, 'bad', '__pycache__'))
            open(os.path.join(tmp, 'bad', '__pycache__', 'test_y.py'), 'w').close()
            os.makedirs(os.path.join(tmp, 'plain'))
            open(os.path.join(tmp, 'plain', 'helper.py'), 'w').close()
            self.assertEqual([os.path.join(tmp, 'bad')], folders_missing_init(tmp))


if __name__ == '__main__':
    unittest.main()
