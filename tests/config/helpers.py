"""Shared helpers for tests/config: temp folders holding a `.kit.json` and its section files."""
import json
import os
import shutil
import tempfile

OWNER = 'owner@example.invalid'
AGENT = 'agent@example.invalid'


class TempTree(object):
    """A plain temp folder (no git) with files written into it."""

    def __init__(self, kit=None, files=None):
        self.root = tempfile.mkdtemp(prefix='kit-config-')
        if kit is not None:
            self.write('.kit.json', kit)
        for rel, body in (files or {}).items():
            self.write(rel, body)

    def write(self, rel, body):
        full = os.path.join(self.root, *rel.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        text = body if isinstance(body, str) else json.dumps(body, indent=1)
        with open(full, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        return full

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


# Synthetic copies of the shapes two real repositories' `.kit.json` files had (alpha and beta here)
# (every section and key they use, values shortened, every address a fake one), migrated to the
# kit's form: a section once read from its legacy tools/*.json is named (pattern 6), the hook
# module moved from "gate".hook to `hook`, the owner keys from "doc_budget" to `owner`, and the
# repo-specific gate keys (pin check, py-drops, pytests extras) to hook options and checks.
ALPHA_SHAPE = {
    'repo': {'name': 'alpha', 'main_branch': 'master'},
    'backlog': {'store': 'docs/backlog', 'phases': ['L0', 'L1', 'X'], 'id_grammar': 'upper',
                'ledger_repo': 'alpha', 'retired_docs': ['docs/DECISIONS.md'],
                'rename_script': 'tools/backlog_rename_priority.py'},
    'checkout_guard': {'rule_pointer': 'python tools/doc_index.py --find lock'},
    'worktree_remove': {'exempt': {'scratch-workspace': 'kept for a tool'}, 'build_output': ['build', '*.pyc'],
                        'keepers': ['*.jsonl']},
    'owner': {'email': OWNER, 'agent_emails': [AGENT]},
    'hook': {'module': 'gate_hook', 'options': {'python_tests_audit': True}},
    'doc_budget': {'budgets': {'CLAUDE.md': 3072},
                   'capped': ['CLAUDE.md'], 'state_docs': ['docs/ops/flakes.md'], 'stale_days': 14},
    'doc_index': {'find_match': 'text', 'roles_doc': 'docs/ops/brief-template.md',
                  'roles_marker': 'Lookups by role'},
    'review_pack': {'coauthor': True},
    'commit': {'coauthor_families': ['Codex']},
    'preflight': {'report_hooks': ['daily_full:summary_lines'], 'traps': 'comments tagged GOTCHA:'},
    'machine_paths': {
        'allow': [{'glob': 'CLAUDE.md', 'line': 'rulings ledger, `', 'why': 'literal on purpose'},
                  {'glob': 'docs/backlog/*.md', 'line': '^(?![a-z_]+: )', 'why': 'temporary',
                   'temporary': True}],
        'fixtures': [{'glob': 'tools/test_deploy_scripts.py', 'hits': ['[C]:\\\\Users\\\\x']}],
        'piece_fixtures': [{'piece': 'worker', 'path': 'src/test/X.java', 'hits': ['[C]:/Users/x']}],
        'known_binary': {'gradle/wrapper/gradle-wrapper.jar': 'a jar Gradle ships'}},
    'tool_tests': 'tools/tool_tests.json',
    'pieces': 'tools/pieces.json',
    'gate': {
        'comment': 'notes are allowed',
        'trailers': True, 'worktree_guard': True, 'python_tests': 'pytests',
        'steps': [
            {'name': 'main-checkout-stray', 'stage': 'first', 'always': True, 'continue': True,
             'warn': True, 'cmd': ['{python}', '{here}/checkout_guard.py', '--main-stray'],
             'merged_args': ['--allow-tracked']},
            {'name': 'forbidden', 'stage': 'last', 'always': True, 'warn': True,
             'cmd': ['{python}', '{here}/forbidden.py', '--base', '{base}']},
            {'name': 'tool-deps-check', 'stage': 'last', 'always': True, 'comment': 'a note',
             'cmd': ['{python}', '{here}/tool_deps.py', '--check']},
            {'name': 'pin-check', 'stage': 'first', 'globs': ['*.gradle.kts', 'gradle/*'],
             'check': 'pin_check'}]},
}

ALPHA_FILES = {
    'tools/tool_tests.json': {
        '_about': 'notes', '_why': {'pinned': 'a note object'},
        'mentions': 'tokens', 'selector_files': ['tool_deps.py'],
        'pinned': [['docs/reference/API_PLAN.md', ['test_backlog']]],
        'patterns': [{'comment': 'a note', 'regex': '(?:^|/)build\\.gradle\\.kts$',
                      'modules': ['test_gate_scope']}],
        'whole_tree': {'test_machine_paths.TheRealTree': 'reads the tree'},
        'dynamic_imports': {'test_kit_parity': [[], 'imports by name']},
        'guard_tests': ['test_tool_deps'], 'unmapped_tool': 'all', 'untested': ['gate_hook'],
        'extra': {'tools/x.py': ['test_x']}},
    'tools/pieces.json': {'worker': {'path': 'worker', 'anything': [1, 2]}},
    'tools/gate_hook.py': ('HOOKED = "alpha"\n\n\ndef pin_check(*args):\n    return True\n\n\n'
                           'GATE_CHECKS = {"pin_check": pin_check}\n'),
}

BETA_SHAPE = {
    'repo': {'name': 'beta'},
    'backlog': {'id_grammar': 'mixed', 'ledger_repo': 'beta'},
    'worktree_remove': {'disposable_untracked': ['*.cs.uid'], 'archive_next_free': True,
                        'branch_template': 'phase/{slug}', 'build_output': ['.godot', '*.pyc']},
    'commit': {'merge_fold': True},
    'doc_budget': {'budgets': {'CLAUDE.md': 11264}, 'capped': ['CLAUDE.md'],
                   'retired_paths': ['docs/decisions/*.md'],
                   'shard_indexes': {'docs/findings': 'docs/TESTING_FINDINGS.md'},
                   'state_docs': ['docs/ops/ports.md'], 'stale_days': 14},
    'owner': {'email': OWNER},
    'hook': {'module': 'gate_hook', 'options': {
        'python_tests_drops': 'tools/py_test_drops.txt', 'python_tests_selected_floor': True,
        'python_tests_docs': ['test_backlog_migrate'], 'python_tests_docs_floor': 105}},
    'doc_index': {'roles': {'worker': ['preflight', 'gate'], 'lander': ['landing']}},
    'preflight': {'engines': ['godot', 'engine.exe'], 'udp_ports': [7800, 7999]},
    'machine_paths': {
        'allow': [{'glob': 'docs/TESTING.md', 'line': 'settings file: ', 'hit': '[C]:/Users/\\.\\.\\.',
                   'why': 'sample output'}],
        'fixtures': [{'glob': 'tests/X/SettingsPathTests.cs', 'hits': ['[C]:/GitHub', '[c]:/users/X']}],
        'fallback_vars': ['DEV_ROOT', 'AGENT_TEMP']},
    'gate': {
        'comment': 'notes are allowed',
        'python_tests': 'pytests', 'python_tests_floor': 1000,
        'warn_untracked': True,
        'fail': ['SCRIPT ERROR|error CS\\d+'], 'warn_lines': ['Node not found'],
        'zero_tests': ['\\bUNIT TESTS OK 0\\b'],
        'errors': {'match': 'ERROR:', 'excuse': ['SCRIPT ERROR'],
                   'expect': ['GATE-EXPECT-BEGIN', 'GATE-EXPECT-END']},
        'steps': [
            {'name': 'build', 'always': True,
             'cmd': ['dotnet', 'build', '{root}/Beta.sln', '--nologo', '-v', 'q']},
            {'name': 'fast', 'globs': ['*'], 'exclude': ['*.md'], 'skip_if_selected': ['unit'],
             'cmd': ['cmd', '/c', '{root}\\test.bat', 'fast'], 'pass': ['Passed!']},
            {'name': 'unit', 'globs': ['scripts/*'], 'exclude': ['scripts/logic/*'],
             'content': {'globs': ['scripts/logic/*.cs'], 'regex': '\\bGD\\.'},
             'cmd': ['cmd', '/c', '{root}\\test.bat'], 'pass': ['Passed!', '(?s)OK.*?quitting']},
            {'name': 'backlog', 'stage': 'last', 'always': True, 'ok_exits': [0, 1],
             'cmd': ['{python}', '{here}/backlog.py', 'check'], 'pass': ['(?m)^ok'],
             'fail': ['\\b[1-9]\\d* FAILED\\b']},
            {'name': 'py-drops', 'stage': 'last', 'always': True, 'check': 'py_drops'}]},
    'tool_tests': {'ignore': ['*.md', '*.txt']},
}

BETA_FILES = {'tools/gate_hook.py': ('HOOKED = "beta"\n\n\ndef py_drops(*args):\n    return True\n\n\n'
                                         'GATE_CHECKS = {"py_drops": py_drops}\n')}
