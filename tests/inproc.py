"""A tool's main(argv) run in the test process, for tests of the tool's own command line: its
arguments, its output and its exit code (AGENTS.md). It saves a Python start per call; a test of
library behaviour calls kitlib instead, and a test that needs the process boundary itself (the
`__main__` block, the interpreter's exit) keeps a real subprocess.

Each call sees what a child process would: os.environ is exactly `env`, the working directory is
`cwd`, and stdout and stderr are fresh buffers. All three are restored after the call, however it
ends. SystemExit gives its code, as the interpreter would; any other exception gives 1 and its
traceback on stderr, as an uncaught exception in a child does.
`env` is the one the same test passes to its real children (gitfixture.fixture_env, built from this
process's environment): tools/kit_tests.py has already dropped the session's variables from it
(child_env), so an in-process call is fenced exactly as a child is and adds no variable of its own."""
import contextlib
import importlib.util
import io
import os
import sys
import traceback
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_tool(name):
    """tools/<name>.py as a module of its own (not `__main__`, so its entry block does not run)."""
    path = os.path.join(ROOT, 'tools', name + '.py')
    spec = importlib.util.spec_from_file_location('%s_in_process' % name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _code(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    sys.stderr.write('%s\n' % value)        # sys.exit('text'): the interpreter prints it, exits 1
    return 1


def run_main(main, argv, env, cwd):
    """(exit code, stdout, stderr) of main(argv) run as a child with `env` in `cwd` would be."""
    out, err = io.StringIO(), io.StringIO()
    old_cwd = os.getcwd()
    with mock.patch.dict(os.environ, env, clear=True), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        os.chdir(cwd)
        try:
            try:
                code = _code(main(list(argv)))
            except SystemExit as exc:
                code = _code(exc.code)
            except Exception:                  # noqa: BLE001 - a child would print it, exit 1
                traceback.print_exc()
                code = 1
        finally:
            os.chdir(old_cwd)
    return code, out.getvalue(), err.getvalue()
