from unittest import mock

import unittest


def keep_off_launcher_repos():
    """For a module's setUpModule: the checklist's stubs check reads {} for launcher.repos, not this
    machine's config (and keep_off_plugin_export). Undone with the module (unittest.addModuleCleanup)."""
    from kitlib import pmstart
    p = mock.patch.object(pmstart, "launcher_repos", lambda: {})
    p.start()
    unittest.addModuleCleanup(p.stop)
    keep_off_plugin_export()


def keep_off_plugin_export():
    """For a module's setUpModule: the checklist's plugin_current check reads neither this machine's
    config nor its plugin export state (its loader gives None: nothing checked)."""
    from kitlib import pmstart
    p = mock.patch.object(pmstart, "plugin_settings", lambda: None)
    p.start()
    unittest.addModuleCleanup(p.stop)
    keep_off_agent_temp()


def keep_off_agent_temp():
    """For a module's setUpModule: the checklist's agent_temp check reads neither this machine's
    environment, its config nor its task scheduler (every reader says "in place": no line)."""
    from kitlib import pmstart
    for name, value in (("agent_temp_env", lambda: "X:\\temp"), ("agent_temp_sweep_key", lambda: {"dir": "X:\\temp"}),
                        ("agent_temp_task_exists", lambda name=None: True)):
        p = mock.patch.object(pmstart, name, value)
        p.start()
        unittest.addModuleCleanup(p.stop)
