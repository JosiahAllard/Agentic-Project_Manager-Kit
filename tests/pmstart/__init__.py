from unittest import mock

import unittest


def keep_off_launcher_repos():
    """For a module's setUpModule: the checklist's stubs check reads {} for launcher.repos, not this
    machine's config. Undone with the module (unittest.addModuleCleanup)."""
    from kitlib import pmstart
    p = mock.patch.object(pmstart, "launcher_repos", lambda: {})
    p.start()
    unittest.addModuleCleanup(p.stop)
