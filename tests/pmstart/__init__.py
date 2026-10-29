from unittest import mock

import unittest


# A current handoff fake: the one file exists in a PM repo, no stray notes (kitlib.pmstart handoff).
HANDOFF_FAKE = {"globber": lambda p: [], "exists": lambda p: True, "pm_repo": lambda c: "X:/pm"}


def keep_off_launcher_repos():
    """For a module's setUpModule: the checklist's checks read {} for launcher.repos, not this
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
    keep_off_machine_config()


def keep_off_machine_config():
    """For a module's setUpModule: the checklist's rulebooks check reads an empty machine config,
    not this machine's (it then gives the "none" line; tests/pmstart/test_rulebooks.py tests it)."""
    from kitlib import pmstart
    p = mock.patch.object(pmstart, "_user_config", lambda: {})
    p.start()
    unittest.addModuleCleanup(p.stop)


# The one line the checklist's hub check gives under keep_off_hub_site.
HUB_KEPT_OFF = "hub: (the hub check, kept off this machine by the tests)"


def keep_off_hub_site():
    """For a module's setUpModule: the checklist's hub check reads no machine config, no token
    and no server; it gives the one line HUB_KEPT_OFF (tests/pmstart/test_hub_site.py tests the
    check itself), and keep_off_session_job. Undone with the module."""
    from kitlib import pmstart
    p = mock.patch.object(pmstart, "hub_site", lambda procs=(), **kw: [HUB_KEPT_OFF])
    p.start()
    unittest.addModuleCleanup(p.stop)
    keep_off_session_job()


def keep_off_session_job():
    """For a module's setUpModule: the checklist's watcher check asks no hub for its session-watch
    job (kitlib.pmwatch.hub_session_job_block gives None: no job covers). Undone with the module."""
    from kitlib import pmwatch
    p = mock.patch.object(pmwatch, "hub_session_job_block", lambda load=None, make_client=None: None)
    p.start()
    unittest.addModuleCleanup(p.stop)


def _no_config_lines(loc=None, run=None, rules=None):
    return []


def _no_ignore_problem(pm):
    return None


REAL_CONFIG_LINES = None
REAL_UNIGNORED = None


def stub_config_lines():
    """For a test package's __init__: the session-start machine config check
    (kitlib.pmstart.configcheck.lines) gives no line, reading no home folder, pointer or PM
    repository, in every test that does not put the real one back (REAL_CONFIG_LINES, as
    tests/pmstart/test_config_check.py does). Its auto-tasks ignore check (configcheck._unignored)
    finds no problem even then, since the fake git has no check-ignore (REAL_UNIGNORED is the real
    one). Once per process; never undone."""
    global REAL_CONFIG_LINES, REAL_UNIGNORED
    from kitlib.pmstart import configcheck
    if configcheck.lines is not _no_config_lines:
        REAL_CONFIG_LINES = configcheck.lines
        configcheck.lines = _no_config_lines
    if configcheck._unignored is not _no_ignore_problem:
        REAL_UNIGNORED = configcheck._unignored
        configcheck._unignored = _no_ignore_problem


stub_config_lines()
