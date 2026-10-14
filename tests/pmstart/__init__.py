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
    keep_off_console_tasks()


def keep_off_console_tasks():
    """For a module's setUpModule: the checklist's console_tasks check reads no task scheduler
    (its reader gives no tasks: no line). tests/pmstart/test_console_tasks.py tests the check."""
    from kitlib.pmstart import consoletasks
    p = mock.patch.object(consoletasks, "read_tasks", lambda: [])
    p.start()
    unittest.addModuleCleanup(p.stop)


# The one line the checklist's hub check gives under keep_off_hub_site.
HUB_KEPT_OFF = "hub: (the hub check, kept off this machine by the tests)"


def keep_off_hub_site():
    """For a module's setUpModule: the checklist's hub check reads no machine config, no token
    and no server; it gives the one line HUB_KEPT_OFF (tests/pmstart/test_hub_site.py tests the
    check itself). Undone with the module."""
    from kitlib import pmstart
    p = mock.patch.object(pmstart, "hub_site", lambda procs=(), **kw: [HUB_KEPT_OFF])
    p.start()
    unittest.addModuleCleanup(p.stop)
