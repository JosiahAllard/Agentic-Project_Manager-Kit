"""kitlib.tooldeps: which tools test modules a change can break - the derived import/mention graph
of a repository's tools folder, the selection a test runner runs, and the audit of the
"tool_tests" tables. tools/tool_deps.py is its entry point, docs/test-tools.md its doc.

    deps   config (the checked "tool_tests" section), scan -> Graph, select / selection, audit
"""
