"""kitlib.pytests: run a repository's tools test modules in parallel, one fresh process per unit,
each in its own job object. tools/pytests.py is its entry point, docs/test-tools.md its doc.

    runner   settings, units, run_unit (one child in a kitlib.proc.job.Job), run (the log a gate
             step reads), changed_paths (--select's change list)
    child    what runs inside a unit's process: the unittest run, PY SKIP lines, the --audit hook
"""
