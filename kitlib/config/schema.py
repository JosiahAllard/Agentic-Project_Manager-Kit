"""The `.kit.json` schema: every section, every key, its type, its default and one line saying what
it does. docs/kit-json.md is these lines (`doc_lines()`); tests/config keeps the two equal.

A section's value is an object, or (where `FILE_OK`) a string naming a repo-relative JSON file
that holds the object. Defaults of None mean "absent": the tool that reads the key decides.
Kit-level sections (repo, owner, hook, heavy, roles) are read by kitlib itself; the rest are the
per-tool sections the PM kit's tools read (kit/tools/_kit.py SECTIONS), checked here for shape only.
"""
from .types import (Any, Bool, Either, Email, Enum, Field, Int, ListOf, MapOf, Obj, Regex, RelPath,
                    Str, T)


class _Null(T):
    """JSON null: an explicit "no cap" in doc_budget "budgets" (kitlib/docs/budget.py budget_for)."""

    def check(self, v, where, errs):
        if v is not None:
            errs.append('%s must be null' % where)

    def describe(self):
        return 'null'


NAME_RE = r'[A-Za-z0-9][A-Za-z0-9._-]*'
MODULE_RE = r'[A-Za-z_][A-Za-z0-9_]*'
STAGES = ('first', 'main', 'last')

Strs = ListOf(Str())
Paths = ListOf(RelPath())
Argv = ListOf(Str(), nonempty=True)
Regexes = Either(Regex(), ListOf(Regex()))

# Paths only the owner changes (PM kit _owner_only.DEFAULT): an entry ending `/` locks a directory.
OWNER_PATHS = ['CLAUDE.md', '.claude/', '.github/workflows/', '.kit.json', 'tools/gate.json',
               'kitlib/gate/strict_defaults.py', 'kitlib/backlog/roles.py']
OWNER_PATHS_EXCEPT = ['.claude/worktrees/']

# The heavy-program entry shape: kit defaults (policy.HEAVY_DEFAULTS) and `.kit.json` "heavy".programs.
HEAVY_ENTRY = Obj({
    'program': Field(Str(), None, 'image-name glob, case-insensitive, matched against the basename (godot*, test.bat)'),
    'kind': Field(Str(pattern=NAME_RE, what='a kind name'), None, 'the kinds-table kind a run of it books'),
    'when_args': Field(Strs, [], 'heavy only when one of these arguments is present (empty: always)'),
    'unless_args': Field(Strs, [], 'not heavy when one of these arguments is present'),
    'why': Field(Str(), None, 'the reason, a ruling or a measurement'),
}, required=('program', 'kind', 'why'), what='{program, kind, when_args, unless_args, why}')

# How a landing in flight is seen (kitlib/records/inflight.py): the kit lock, or a repo command.
IN_FLIGHT = Obj({
    'wait_s': Field(Int(0, 3600), None, 'most seconds to wait; unset: 100 (inferred, not measured: under the agent shell 120 s timeout)'),
    'command': Field(Argv, None, 'argv run in the main checkout: exit 0 in flight (first line names the holder), 1 none, else in flight'),
}, what='{wait_s, command}')

ROLE = Obj({
    'edit_main_checkout': Field(Bool(), None, 'may edit files in a main checkout; unset: the base role (main: true, any other: false)'),
    'why': Field(Str(), None, 'the ruling that grants it'),
}, what='{edit_main_checkout, why}')

STEP = Obj({
    'name': Field(Str(pattern=NAME_RE, what='a step name'), None, 'the step name, unique'),
    'globs': Field(Strs, None, 'changed paths that select the step'),
    'exclude': Field(Strs, None, 'changed paths that never select it'),
    'cmd': Field(Argv, None, 'the argv; {python} {root} {base} {here} {tools} are substituted'),
    'full_cmd': Field(Argv, None, 'the argv a full run (gate.py --full, land --full, daily_full) runs instead of cmd; placeholders as cmd'),
    'daily_cmd': Field(Argv, None, 'the argv the daily full run (daily_full) runs instead of full_cmd and cmd; placeholders as cmd'),
    'check': Field(Str(pattern=NAME_RE, what='a check name'), None, 'a gate check the repo hook module provides (GATE_CHECKS), run instead of cmd'),
    'cwd': Field(RelPath(), None, 'run directory, repo-relative'),
    'pass': Field(Regexes, None, 'patterns the log must hold'),
    'fail': Field(Regexes, None, 'patterns that fail the step'),
    'always': Field(Bool(), None, 'run whatever changed'),
    'manual': Field(Bool(), None, 'run only when named'),
    'warn': Field(Bool(), None, 'a failure warns, never fails'),
    'continue': Field(Bool(), None, 'later steps run after a failure'),
    'defer': Field(Bool(), None, 'run after the other steps'),
    'skip_if_selected': Field(Strs, None, 'skip when one of these steps is selected'),
    'merged_args': Field(Strs, None, 'arguments added on the merged tree at landing'),
    'stage': Field(Enum(*STAGES), None, 'first, main or last'),
    'ok_exits': Field(ListOf(Int(), nonempty=True), None, 'exit codes that pass'),
    'content': Field(Obj({'globs': Field(Argv, None, 'files read'),
                          'regex': Field(Regex(), None, 'content that selects')},
                         required=('globs', 'regex'), what='{globs, regex}'),
                     None, 'select by changed file content'),
    'requires': Field(ListOf(Either(Str(), Argv), nonempty=True), None, 'commands or probe argvs; missing: skip'),
    'requires_strict': Field(Bool(), None, 'a missing requirement fails instead of skipping'),
    'reuse': Field(Either(Bool(), Str(pattern=NAME_RE, what='a group name')), None, 'a branch gate rerun or a landing does not run the step again while the gate cache holds a pass of its key (its input files, command, gate config and kit files); a group name: its group is reused together or not at all'),
}, required=('name',), what='step object')

# A folder outside git an agent session may write, and how (kitlib/guard/jail.py, docs/guard.md "Write jail").
DATA_ROOT = Obj({
    'path': Field(Str(pattern=r'(?:[A-Za-z]:[\\/]|\\\\|//).+', what='an absolute folder path'), None, 'the folder, absolute'),
    'mode': Field(Enum('add-only', 'read-only', 'open'), None, 'add-only: new files only, never overwrite, move or delete; read-only: no write; open: any write'),
    'why': Field(Str(), None, 'the reason, a ruling or a measurement'),
}, required=('path', 'mode'), what='{path, mode, why}')

# A landing lane (gate.py land --lane).
LANE = Obj({
    'branch_prefix': Field(Strs, None, 'a branch landing on the lane starts with one of these'),
    'paths': Field(Strs, None, 'every path the branch changes matches one of these globs'),
    'why': Field(Str(), None, 'the ruling that made the lane'),
    'merged_args': Field(Strs, None, 'arguments added to every step command of a landing on this lane, after the step merged_args'),
}, what='{branch_prefix, paths, why, merged_args}')

# A red-first runner (tools/redfirst.py, kitlib/redfirst/verdict.py): the first whose glob matches.
RUNNER = Obj({
    'glob': Field(Str(), None, 'repo-relative test files it takes (fnmatch, case-blind, * crosses /)'),
    'kind': Field(Enum('python', 'gradle', 'dotnet'), None, 'python unittest, a Gradle module test, a dotnet test project'),
    'pythonpath': Field(RelPath(), None, 'python: the folder on PYTHONPATH, modules relative to it (default the root)'),
    'dir': Field(RelPath(), None, 'gradle: the module folder holding build/test-results'),
    'project': Field(Str(), None, 'gradle: the project path, :core say'),
    'csproj': Field(RelPath(), None, 'dotnet: the test project file'),
    'args': Field(Strs, None, 'arguments appended to the command'),
    'lock': Field(Str(pattern=NAME_RE, what='a kind name'), None, 'the kinds-table kind booked around its command (unset: the kind of the heavy-list entry the command matches - build for gradle and dotnet, none for python)'),
}, required=('glob', 'kind'), what='{glob, kind, pythonpath, dir, project, csproj, args, lock}')

SECTIONS = {
    'repo': {
        'name': Field(Str(nonempty=False), '', 'repository name for reports and the ledger'),
        'main_branch': Field(Str(), 'main', 'the branch landings target'),
        'remote': Field(Str(), 'origin', 'the remote main is pushed to'),
        'worktrees_dir': Field(RelPath(), '.claude/worktrees', 'where agent worktrees live'),
        'tools_dir': Field(RelPath(), 'tools', 'where repo tools, stubs and the hook module live'),
        'push_block': Field(Bool(), False, 'pre-push refuses a push to main without a gate-OK record'),
    },
    'owner': {
        'email': Field(Email(), None, 'author email of the owner\'s own commits; unset: no owner commit is provable'),
        'agent_emails': Field(ListOf(Email()), [], 'author emails known to be agents\', never the owner\'s'),
        'paths': Field(Strs, OWNER_PATHS, 'owner-only paths; an entry ending / locks a directory; replaces the default'),
        'paths_except': Field(Strs, OWNER_PATHS_EXCEPT, 'what under a locked directory is not locked'),
        'trailer': Field(Str(pattern=r'[A-Za-z][A-Za-z-]*', what='a trailer key'), 'Owner-Approved', 'the trailer key approving an owner-only change'),
    },
    'hook': {
        'module': Field(Str(pattern=MODULE_RE, what='a module name'), None, 'the one repo hook module, <tools_dir>/<module>.py'),
        'options': Field(MapOf(Any()), {}, 'passed to the hook unread'),
    },
    'heavy': {
        'programs': Field(ListOf(HEAVY_ENTRY), [], 'repo heavy programs, added to the kit defaults'),
    },
    'roles': {},
    'backlog': {
        'store': Field(Either(Enum('ref', 'branch', 'main'), RelPath()), 'ref',
                       'the record store: ref (refs/backlog/main), branch (the backlog branch), main (docs/backlog) or a folder (docs/backlog.md)'),
        'phases': Field(Strs, [], 'phase names in order'),
        'id_grammar': Field(Enum('upper', 'mixed'), 'upper', 'item id spelling'),
        'ledger_repo': Field(Str(), None, 'the ledger repo column; default the repo name'),
        'code_main': Field(Str(), None, 'the branch code lands on, which `add --found-on` measures a branch\'s diff from; default repo.main_branch'),
    },
    'board': {
        'state_file': Field(RelPath(), None, 'the board state file, under the git dir (default <git common dir>/kit/board-state.md)'),
    },
    'checkout_guard': {
        'rule_pointer': Field(Str(), None, 'where a refusal points for the rule'),
    },
    'commit': {
        'merge_fold': Field(Bool(), False, 'commit.py folds a merge commit'),
        'coauthor_families': Field(Strs, [], 'accepted Co-Authored-By families beyond Claude'),
    },
    'daily_full': {
        'enabled': Field(Bool(), False, 'daily_full.py runs at all (opt-in; it is never scheduled by the kit)'),
        'ref': Field(Str(), None, 'the ref it tests; default <remote>/<main branch>'),
        'worktree': Field(RelPath(), None, 'its detached worktree; default <worktrees_dir>/daily-full'),
        'kind': Field(Str(pattern=NAME_RE, what='a kind name'), 'daily-full', 'the kinds-table kind the run books'),
        'max_wait': Field(Int(0), 7200, 'seconds it waits for its lock slot before it skips'),
        'stale_hours': Field(Int(1), 36, 'hours after which --summary calls the last result stale'),
    },
    'doc_budget': {
        'default': Field(Int(0), None, 'byte cap for a capped doc with no budget'),
        'budgets': Field(MapOf(Either(Int(0), _Null()), 'path'), {}, 'doc path or glob -> byte cap; null: uncapped'),
        'uncapped_dirs': Field(Paths, [], 'folders never capped'),
        'capped': Field(Paths, [], 'docs that must stay under budget'),
        'retired_paths': Field(Paths, [], 'paths that must not come back'),
        'shard_indexes': Field(MapOf(RelPath(), 'folder'), {}, 'shard folder -> its index doc'),
        'state_docs': Field(Paths, [], 'docs checked for staleness'),
        'stale_days': Field(Int(1), None, 'days before a state doc is stale'),
    },
    'doc_index': {
        'enabled': Field(Bool(), True, 'false: doc_index does not require every .md on an index entry line, nor the index file (a repo with no .kit.json skips it too)'),
        'index': Field(RelPath(), 'docs/INDEX.md', 'the doc index both doc tools read'),
        'find_match': Field(Enum('keywords', 'text'), 'keywords', 'how --find matches'),
        'roles': Field(MapOf(Strs, 'role'), None, 'role -> lookup topics'),
        'roles_doc': Field(RelPath(), None, 'doc holding the role map instead'),
        'roles_marker': Field(Str(), None, 'line before the role bullets in roles_doc'),
        'role_lines': Field(Int(1), 80, 'lines printed per role section'),
    },
    'forbidden': {},
    'gate': {
        'steps': Field(ListOf(STEP), [], 'the repo gate steps'),
        'setup': Field(Argv, None, 'argv run before the steps'),
        'python_tests': Field(Enum('discover', 'pytests'), 'discover', 'how the Python tests run'),
        'python_tests_floor': Field(Int(0), None, 'fewest tests a full run may report'),
        'trailers': Field(Bool(), None, 'check commit trailers'),
        'worktree_guard': Field(Bool(), None, 'refuse to gate in a main checkout'),
        'warn_untracked': Field(Bool(), None, 'warn about untracked files'),
        'fail': Field(Regexes, None, 'log patterns that fail every step'),
        'warn_lines': Field(Regexes, None, 'log patterns that warn'),
        'zero_tests': Field(Regexes, None, 'log patterns meaning no test ran'),
        'errors': Field(Obj({'match': Field(Regex(), None, 'an error line'),
                             'excuse': Field(Regexes, None, 'lines that excuse it'),
                             'expect': Field(ListOf(Str(), length=2), None, 'begin and end markers of expected errors')},
                            required=('match',), what='{match, excuse, expect}'),
                        None, 'error-line policy'),
        'kind': Field(Str(pattern=NAME_RE, what='a kind name'), 'engine-test', 'the kinds-table kind a gate run books (gate.py and gate.py land)'),
    },
    'guard': {
        'data_roots': Field(ListOf(DATA_ROOT), [], 'folders besides their checkout and temp that agent sessions may write, each with its mode; read from the main checkout only (docs/guard.md "Write jail")'),
    },
    'landing': {
        'lock_file': Field(Str(), None, 'legacy landing lock: git:<name>, a file in the git common dir; any other value refuses'),
        'log_dir': Field(RelPath(), None, 'where gate logs live'),
        'lanes': Field(MapOf(LANE, 'lane'), {}, 'lanes besides full that gate.py land --lane takes'),
        'wait_s': Field(Int(0, 86400), 3600, 'seconds tools/land.py waits for its turn at the landing lock unless --wait says otherwise'),
        'batch_max': Field(Int(1, 20), 3, 'most branches one tools/land.py landing merges and gates together'),
        'reuse_max_age_h': Field(Int(0, 168), 24, 'hours a pass stored in the gate cache serves a branch gate rerun or a landing; 0: no gate reads or stores a step result'),
        'reuse_never': Field(Strs, [], 'globs of paths whose change makes a gate (branch or landing) read and store no step result in the gate cache'),
        'in_flight': Field(IN_FLIGHT, None, 'how tools/new_worktree.py and tools/sync_base.py see a landing in flight before they fetch, and wait for it (unset: off; {}: the kit landing lock; wait_s unset: 100, inferred not measured; docs/new-worktree.md "Waiting for a landing in flight")'),
    },
    'ledger': {
        'pre_commit_check': Field(Bool(), False, 'githooks/pre-commit runs tools/ledger.py check --base HEAD before each commit that changes the ledger file or its standing digest (opt-in; docs/ledger.md "Pre-commit check")'),
    },
    'machine_paths': {
        'allow': Field(ListOf(Obj({'glob': Field(Str(), None, 'files'),
                                   'line': Field(Regex(), None, 'lines'),
                                   'hit': Field(Regex(), None, 'the allowed hit'),
                                   'why': Field(Str(), None, 'reason'),
                                   'temporary': Field(Bool(), None, 'to be removed')},
                                  required=('glob', 'why'), what='{glob, line, hit, why, temporary}')),
                       [], 'machine-path hits allowed'),
        'fixtures': Field(ListOf(Obj({'glob': Field(Str(), None, 'files'),
                                      'hits': Field(ListOf(Regex()), None, 'hits'),
                                      'line': Field(Regex(), None, 'lines'),
                                      'why': Field(Str(), None, 'reason')},
                                     required=('glob', 'hits'), what='{glob, hits, line, why}')),
                          [], 'test fixtures that name machine paths'),
        'piece_fixtures': Field(ListOf(Obj({'piece': Field(Str(), None, 'piece'),
                                            'path': Field(Str(), None, 'path in the piece'),
                                            'hits': Field(ListOf(Regex()), None, 'hits'),
                                            'why': Field(Str(), None, 'reason')},
                                           required=('piece', 'hits'), what='{piece, path, hits, why}')),
                                [], 'fixtures named by piece; machine_paths.py refuses a non-empty list until it resolves pieces'),
        'known_binary': Field(MapOf(Str(), 'path'), {}, 'binary file -> why it may hold paths'),
        'scan': Field(Strs, ['*'], 'globs scanned'),
        'fallback_vars': Field(Strs, [], 'environment variables a machine path may fall back to'),
    },
    'new_worktree': {},
    'pieces': {},
    'preflight': {
        'engines': Field(Strs, [], 'process-name prefixes listed as running engines'),
        'report_hooks': Field(ListOf(Str(pattern=r'[\w.]+:\w+', what='"module:function"')), [], 'module:function report hooks'),
        'traps': Field(Str(), None, 'where the traps are written down'),
        'udp_ports': Field(ListOf(Int(0, 65535), length=2), None, '[low, high] ports checked free'),
    },
    'redfirst': {
        'runners': Field(ListOf(RUNNER), [{'glob': 'tests/*test_*.py', 'kind': 'python'}], 'which runner takes a test file, first match wins; none written: this default, then <tools_dir>/*test_*.py as python with the tools folder as pythonpath, unless tools_dir is . or that glob is listed (docs/redfirst.md); a runner\'s lock is the kinds-table kind the run books around its command (unset: the kind of the heavy-list entry the command matches)'),
        'hold_wait': Field(Int(0), 60, 'seconds a held throwaway tree is re-checked before it is left'),
    },
    'review_pack': {
        'coauthor': Field(Bool(), False, 'list the branch\'s commits with no Co-Authored-By trailer, judged by the trailers rule; a failing one blocks'),
    },
    'ruling_cites': {
        'repo': Field(Str(), None, 'the ledger repo column cited'),
        'skip': Field(Strs, None, 'globs of paths not checked; unset: <tools_dir>/test_*.py and <tools_dir>/fixtures/*'),
    },
    'stubs': {
        'tools': Field(Strs, None, 'kit tools stubbed into the repo'),
    },
    'testlist': {
        'junit': Field(Strs, ['**/build/test-results/**/*.xml'], 'JUnit result globs'),
        'trx': Field(Strs, ['**/*.trx'], '.NET result globs'),
    },
    'tool_tests': {
        'mentions': Field(Enum('path', 'tokens'), 'path', 'how a tool mention is found'),
        'selector_files': Field(Strs, ['tool_deps.py', 'pytests.py'], 'changes that select every test'),
        'extra': Field(MapOf(Strs, 'file'), {}, 'file -> test modules it selects'),
        'pinned': Field(ListOf(Any('[path, [modules]]')), [], 'path -> modules pins'),
        'patterns': Field(ListOf(Obj({'regex': Field(Regex(), None, 'paths'),
                                      'modules': Field(Strs, None, 'modules')},
                                     required=('regex', 'modules'), what='{regex, modules}')),
                          [], 'path regex -> modules'),
        'whole_tree': Field(MapOf(Str(), 'test class'), {}, 'classes reading the whole tree -> why'),
        'untested': Field(Strs, [], 'modules with no test, each listed'),
        'dynamic_imports': Field(MapOf(Any('[modules, why]'), 'module'), {}, 'computed imports allowed'),
        'guard_tests': Field(Strs, ['test_tool_deps'], 'tests guarding the table'),
        'unmapped_tool': Field(Enum('guard', 'all'), 'guard', 'what an unmapped tool change selects'),
        'always_run': Field(Strs, [], 'tests every run includes'),
        'sources': Field(Strs, [], 'token mode: source globs'),
        'skip_dirs': Field(Strs, ['__pycache__'], 'folders never scanned'),
        'ignore': Field(Strs, [], 'changes that select nothing'),
        'shards': Field(MapOf(Int(1), 'module'), {}, 'pytests: module -> shard count'),
        'serial': Field(Strs, [], 'pytests: modules or classes run alone'),
        'split_classes': Field(Bool(), False, 'pytests: one unit per class'),
        'jobs_max': Field(Int(1), 6, 'pytests: most parallel jobs'),
        'jobs_divisor': Field(Int(1), 4, 'pytests: cores per job'),
        'jobs_env': Field(Str(), None, 'pytests: env var overriding the job count'),
        'unit_timeout': Field(Int(1), 3600, 'pytests: seconds per unit'),
    },
    'worktree_remove': {
        'exempt': Field(MapOf(Str(), 'folder'), {}, 'worktree folder -> the ruling exempting it'),
        'disposable_untracked': Field(Strs, [], 'untracked file globs removal may discard'),
        'build_output': Field(ListOf(Either(Str(), Obj({'piece': Field(Str(), None, 'piece id'), 'path': Field(Str(), None, 'folder in the piece')}, required=('piece', 'path'), what='{piece, path}'))), [], 'build output path globs, or {piece, path} folders resolved through the pieces table'),
        'keepers': Field(Strs, [], 'file globs that refuse a removal, added to the kit list'),
        'archive_next_free': Field(Bool(), False, 'archive under the next free name'),
        'branch_template': Field(Str(pattern=r'[^{}]*\{slug\}[^{}]*', what='a branch name holding {slug} once'), None, 'branch name for a slug'),
    },
}

# A section that may be a string naming a repo-relative JSON file. guard never: a file beside
# .kit.json would let a change to it widen what agent sessions may write past the kit-json rule.
FILE_OK = frozenset(SECTIONS) - {'repo', 'owner', 'hook', 'guard'}
# Sections whose content is a table the kit does not shape-check (an object of anything).
OPAQUE = frozenset(('forbidden', 'pieces'))
# Sections that are a map of one entry type: role name -> permissions, over policy.ROLE_DEFAULTS.
MAPS = {'roles': (ROLE, 'role', 'agent type (or main, subagent) -> permissions, over the kit defaults')}


# Keys no tool reads any more, per section. A config that still carries one loads: the key is
# dropped unchecked (not an unknown-key refusal), so an install written before the removal works.
RETIRED = {'backlog': ('retired_docs', 'rename_script')}


def section_type(name):
    if name in OPAQUE:
        return MapOf(Any(), 'key')
    if name in MAPS:
        return MapOf(MAPS[name][0], MAPS[name][1])
    return Obj(SECTIONS[name], what='"%s"' % name)


def defaults(name):
    """A fresh dict of section `name`'s defaults, every key present (None where unset)."""
    import copy
    return {k: copy.deepcopy(f.default) for k, f in SECTIONS.get(name, {}).items()}


def _fmt(v):
    import json
    return json.dumps(v)


def doc_lines():
    """docs/kit-json.md's field lines, one per key, in schema order."""
    out = []
    for name, fields in SECTIONS.items():
        if name in OPAQUE:
            out.append('- `%s`: a table the kit passes through unchecked (a file path or an object)' % name)
            continue
        if name in MAPS:
            t, what, doc = MAPS[name]
            out.append('- `%s` (map %s -> %s): %s' % (name, what, t.describe(), doc))
            for k, f in t.fields.items():
                d = '' if f.default is None else ', default %s' % _fmt(f.default)
                out.append('- `%s.<%s>.%s` (%s%s): %s' % (name, what, k, f.type.describe(), d, f.doc))
            continue
        if not fields:
            out.append('- `%s`: no keys' % name)
            continue
        for k, f in fields.items():
            d = '' if f.default is None else ', default %s' % _fmt(f.default)
            out.append('- `%s.%s` (%s%s): %s' % (name, k, f.type.describe(), d, f.doc))
    return out
