"""The doc tools (docs/doc-tools.md): byte budgets and owner-only paths over a branch's docs
(budget), the doc index checks, staleness report and index lookups (index), and a code map of
the tracked source (codemap).

tools/doc_budget.py, tools/doc_index.py and tools/codemap.py parse, print and exit; everything
else is here. Nothing here prints: every line goes to an `out` callable the caller passes.
"""
