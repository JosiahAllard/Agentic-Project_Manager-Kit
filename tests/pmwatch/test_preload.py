"""kitlib.pmwatch.preload: a Watcher loads at start every kit module its checks would otherwise
import on their first pass, so a landing that writes the checkout later cannot mix old and new
modules in the running watcher. Each test builds the Watcher in a child interpreter, because this
suite's own sys.modules already holds what other tests imported. The child only imports and builds
objects (no start, no poll, no git, no home read) and runs on the test process's environment,
unchanged."""
import unittest

from tests.lazyimports import child_loaded, late_kit_imports

PRELOADED = ('kitlib.pluginsource', 'kitlib.pluginsource.claude', 'kitlib.machinesetup.extras',
             'kitlib.launcher.settings', 'kitlib.launcher.usage', 'kitlib.launcher.pipes', 'kitlib.launcher.stop', 'kitlib.proc.process')
WATCH_PATH = ('kitlib.pmwatch',) + PRELOADED + ('kitlib.launcher.accounts',)

# As tools/pm_watch.py builds it: the host's entry point passes its own modules in.
BUILD = """
from hosts.claude.adapter import claude_host
from kitlib import pmwatch
pmwatch.Watcher({}, [], lambda: {}, 'alerts', 'sessions.json', lambda name: None,
                preload_modules=claude_host.PRELOAD)
"""


class PreloadTest(unittest.TestCase):
    def test_a_new_watcher_has_loaded_every_module_its_checks_use(self):
        loaded = child_loaded(BUILD)
        self.assertEqual([m for m in PRELOADED if m not in loaded], [])

    def test_no_watch_path_module_imports_a_kit_module_late(self):
        loaded = child_loaded(BUILD)
        self.assertEqual(late_kit_imports(WATCH_PATH, loaded), [])


if __name__ == '__main__':
    unittest.main()
