"""kitlib.hubjobs: the hub server's timed jobs, one module each, found by discovery
(docs/action-hub.md "Timed jobs"). The runner is kitlib.hubsite.jobs; this package only holds
the jobs.

  makers(package=None)    [maker], one per module of `package` (default this one) whose name
                          does not start with '_', sorted by name; nothing is imported yet
  maker(sources)          imports its module and returns module.make(sources): a
                          kitlib.hubsite.jobs.Job, or None when the job does not apply here;
                          `maker.module` is the module's full name

serve.machine_sources puts makers() in Sources.jobs for the machine's server (a scratch store
gets none), and serve.build calls each maker: a module that fails to import, or a make that
raises, is a line in the hub's start-up notes and the hub serves without that job; a make that
returns None is left out silently. The hub's stale-code check (serve.code_stamp) reads this
folder too, so a job added or changed on disk asks for a restart like hub code does.

Adding a job: write one module here, `kitlib/hubjobs/<name>.py`, with `make(sources)` returning
`jobs.Job(NAME, EVERY_S, run)` (kitlib.hubsite.jobs docstring: what run(ctx) returns, Skip,
ctx.event(word, detail=None)), and its tests in `tests/hubjobs/`. Nothing else changes: no
registration line, no list in a shared file. `sources` is serve.Sources (its `home`, the
machine config's folders). The job's name must be unique among the jobs: a second job of a
name already made is left out, with a note. A module whose name starts with '_' is not a job
(a helper several jobs share).
"""
import importlib
import pkgutil
import sys


def makers(package=None):
    """One maker per module of `package` (a package object; default kitlib.hubjobs), sorted by
    module name; a name that starts with '_' is not a job. Nothing is imported here."""
    package = package or sys.modules[__name__]
    names = sorted(name for _, name, is_pkg in pkgutil.iter_modules(package.__path__)
                   if not is_pkg and not name.startswith('_'))
    return [_maker(package.__name__ + '.' + name) for name in names]


def _maker(module):
    def maker(sources):
        return importlib.import_module(module).make(sources)
    maker.module = module
    maker.__module__ = module       # what serve's note names when the maker raises
    return maker
