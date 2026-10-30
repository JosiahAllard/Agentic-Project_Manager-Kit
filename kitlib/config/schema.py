"""The `.kit.json` schema: every section, every key, its type, its default and one line saying what
it does. docs/kit-json.md is these lines (`doc_lines()`); tests/config keeps the two equal.

A section's value is an object, or (where `FILE_OK`) a string naming a repo-relative JSON file
that holds the object. Defaults of None mean "absent": the tool that reads the key decides.
Kit-level sections (repo, owner, hook, heavy, roles) are read by kitlib itself; the rest are the
per-tool sections the PM kit's tools read (kit/tools/_kit.py SECTIONS), checked here for shape only.
"""
from kitlib.host import registry
from kitlib.host.content import MODES

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
BRANCH_TEMPLATE = Str(pattern=r'[^{}]*\{slug\}[^{}]*', what='a branch name holding {slug} once')
Paths = ListOf(RelPath())
Argv = ListOf(Str(), nonempty=True)
Regexes = Either(Regex(), ListOf(Regex()))

# Paths only the owner changes (PM kit _owner_only.DEFAULT): an entry ending `/` locks a directory.
# AGENTS.md is the canonical rules file and docs/agent-profile.md the neutral profile (each host's
# own rules file and folder point at them), so they are locked as their old homes are. Each host's
# own paths are its hosts.json profile owner_paths, and its agent worktrees under them are not
# locked (worktrees_directory).
OWNER_PATHS = list(dict.fromkeys(
    ['AGENTS.md'] + list(registry.union('owner_paths'))
    + ['docs/agent-profile.md', '.github/workflows/', '.kit.json', 'tools/gate.json',
       'kitlib/gate/strict_defaults.py', 'kitlib/backlog/roles.py',
       'hosts/claude/managed-settings.json']))
OWNER_PATHS_EXCEPT = [w + '/' for w in registry.union('worktrees_directory')]

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
    'mode': Field(Enum(*MODES), None, 'the role policy this role is held to, over its role file\'s `mode` (docs/guard.md "Role policy"); unset: the role file\'s, else none'),
    'why': Field(Str(), None, 'the ruling that grants it'),
}, what='{edit_main_checkout, mode, why}')

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

# gate.docs_only (kitlib.gate.steps.docs_only): which changes are docs, and the steps they run.
DOCS_ONLY = Obj({
    'globs': Field(ListOf(Str(), nonempty=True), None, 'every changed path matches one of these'),
    'exclude': Field(Strs, None, 'and none matches one of these'),
    'steps': Field(ListOf(Str(pattern=NAME_RE, what='a step name'), nonempty=True), None,
                   'the steps a docs-only change runs (a scrub check, say)'),
}, required=('globs', 'steps'), what='{globs, exclude, steps}')

# daily_full.bisect (kitlib.dailyfull, docs/daily-full.md "Bisect").
BISECT = Obj({
    'max_probes': Field(Int(0, 50), 5, 'most commits one bisect runs the red steps at; 0: no bisect'),
    'prefix': Field(Str(pattern=r'[A-Z][A-Z0-9-]*', what='a record id prefix'), None,
                    'files <prefix>-DAILY-RED-<commit> for the culprit found; unset: nothing is filed'),
    'session': Field(Str(pattern=r'[a-z][a-z0-9-]*', what='a session name'), None,
                     'the session the filed record carries'),
}, what='{max_probes, prefix, session}')

# A folder outside git an agent session may write, and how (kitlib/guard/jail.py, docs/guard.md "Write jail").
DATA_ROOT = Obj({
    'path': Field(Str(pattern=r'(?:[A-Za-z]:[\\/]|\\\\|//).+', what='an absolute folder path'), None, 'the folder, absolute'),
    'mode': Field(Enum('add-only', 'read-only', 'open'), None, 'add-only: new files only, never overwrite, move or delete; read-only: no write; open: any write'),
    'why': Field(Str(), None, 'the reason, a ruling or a measurement'),
}, required=('path', 'mode'), what='{path, mode, why}')

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
        'name': Field(Str(nonempty=False), '', 'repository name for reports; unset or empty: the last part of the remote\'s URL, else the main checkout\'s folder name'),
        'main_branch': Field(Str(), 'main', 'the branch landings target; unset: the branch <remote>/HEAD names, else main, else master when only that exists (docs/kit-json.md "With no file")'),
        'remote': Field(Str(), 'origin', 'the remote main is pushed to'),
        'worktrees_dir': Field(RelPath(), registry.default_profile().worktrees_directory,
                               'where agent worktrees live'),
        'worktrees_by_host': Field(MapOf(RelPath(), what='host'), {},
                                   'per-host worktree folders, keyed by hosts.json host name '
                                   '(docs/new-worktree.md "Hosts")'),
        'tools_dir': Field(RelPath(), 'tools', 'where repo tools and the hook module live'),
        'push_block': Field(Bool(), False, 'pre-push refuses a push to main without a gate-OK record; a delete or a forced push of main is refused whatever it says'),
    },
    'owner': {
        'email': Field(Email(), None, 'author email of the owner\'s own commits; unset: the machine config\'s owner_email; neither: no owner commit is provable, so every owner-only change is refused'),
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
                       'the record store: ref (refs/backlog/main), branch (the backlog branch), main (docs/backlog) or a folder (docs/backlog.md); the default ref is read-only, so a repository that writes records names its store'),
        'phases': Field(Strs, [], 'phase names in order'),
        'id_grammar': Field(Enum('upper', 'mixed'), 'upper', 'item id spelling'),
        'code_main': Field(Str(), None, 'the branch code lands on, which `add --found-on` measures a branch\'s diff from; default repo.main_branch'),
        'cap': Field(Int(1, None), 5, 'most records `backlog.py next` offers and `pull` takes in order (`next --cap` overrides it for one call); every worktree reads the main checkout\'s value'),
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
        'bisect': Field(BISECT, None, 'after a red run, bisect the first-parent commits since the last green run for the commit that turned the red steps red (docs/daily-full.md "Bisect"); unset: no bisect'),
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
        'enabled': Field(Bool(), True, 'false: doc_index does not require every .md on an index entry line, even with an index file; with no index file the check is skipped anyway (the index file is the opt-in)'),
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
        'smoke_steps': Field(ListOf(Str(pattern=NAME_RE, what='a step name')), None, 'steps a default (not --full) branch gate runs whatever changed; a landing takes such a gate\'s record for a tip once these steps passed in it (docs/gate.md "Landing"); unset: a landing needs a --full record'),
        'docs_only': Field(DOCS_ONLY, None, 'a change whose every path is docs: the branch gate runs only its steps, and a landing takes that record (docs/gate.md "Docs-only changes"); unset: no change is docs-only'),
    },
    'guard': {
        'data_roots': Field(ListOf(DATA_ROOT), [], 'folders besides their checkout and temp that agent sessions may write, each with its mode; read from the main checkout only (docs/guard.md "Write jail")'),
        'role_policy': Field(Bool(), False, 'hold roles to their role policy mode (docs/guard.md "Role policy"); off: no role is held, whatever its mode; read from the main checkout only'),
    },
    'landing': {
        'log_dir': Field(RelPath(), None, 'where gate logs live'),
        'wait_s': Field(Int(0, 86400), 21600, 'seconds tools/land.py waits for its turn (its machine-lock booking, then the landing lock) unless --wait says otherwise'),
        'batch_max': Field(Int(1, 20), 3, 'most branches one tools/land.py landing merges and gates together'),
        'self_review_max_lines': Field(Int(1, 100000), None, 'most changed lines (added plus removed, git diff --numstat: the larger of <base>...<tip> and <main as fetched>...<tip>) a verdict the queueing coordinator reviewed itself may cover: tools/verdict.py add refuses a larger self-read, or one whose size cannot be told (docs/gate.md "Strict paths"); unset: no size check'),
        'reuse_max_age_h': Field(Int(0, 168), 24, 'hours a pass stored in the gate cache serves a branch gate rerun or a landing; 0: no gate reads or stores a step result'),
        'reuse_never': Field(Strs, [], 'globs of paths whose change makes a gate (branch or landing) read and store no step result in the gate cache'),
        'in_flight': Field(IN_FLIGHT, None, 'how tools/new_worktree.py and tools/sync_base.py see a landing in flight before they fetch, and wait for it (unset: off; {}: the kit landing lock; wait_s unset: 100, inferred not measured; docs/new-worktree.md "Waiting for a landing in flight")'),
        # Not in "gate": a key there is in every gate-OK record's gate_config hash (runner.gate_config).
        'shared_paths': Field(ListOf(Str(pattern=r'(?s).*[^/\\]', what='whole-path glob (a folder is "api/*", never "api/")'), nonempty=True), None, 'globs of the repo\'s shared code: a landing takes a default-mode (scoped) record of a tip that changes none of them, no strict path and no gate config file, without "gate".smoke_steps; a tip that changes one of those needs a --full record, whatever "gate".smoke_steps or "gate".docs_only say (docs/gate.md "Shared paths"); unset: a landing needs a --full record unless "gate".smoke_steps or "gate".docs_only take it'),
        'gate_once': Field(Bool(), False, 'a landing takes a default-mode (scoped) record of any tip that changes no strict path and no gate config file, shared paths included, and its merged gate runs --full (each step\'s full_cmd) once on the merged or batched tree; a docs-only merge stays docs-only (docs/gate.md "Gate once at landing"); off: "landing".shared_paths, "gate".smoke_steps and "gate".docs_only decide alone'),
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
        'known_binary': Field(MapOf(Str(), 'path'), {}, 'binary file -> why it may hold paths'),
        'scan': Field(Strs, ['*'], 'globs scanned'),
        'fallback_vars': Field(Strs, [], 'environment variables a machine path may fall back to'),
    },
    # maint_lint.gate_time (kitlib.maintlint.gatetime, docs/maint-lint.md "Gate time").
    'maint_lint': {
        'gate_time': Field(Obj({
            'prefix': Field(Str(pattern=r'[A-Z][A-Z0-9-]*', what='a record id prefix'), None,
                            'a plain maint_lint run files <prefix>-GATE-TIME-<STEP>-<YYYYMMDD> for a gate step whose median time is over bound_s; unset: nothing is filed'),
            'bound_s': Field(Int(1, 86400), 900, 'seconds a gate step\'s median time (full gates) may reach; unset: 900'),
            'session': Field(Str(pattern=r'[a-z][a-z0-9-]*', what='a session name'), None,
                             'the session the filed record carries'),
        }, what='{prefix, bound_s, session}'), None,
            'the gate-time check of tools/maint_lint.py (docs/maint-lint.md "Gate time"): bound_s (1 to 86400, default 900) the seconds a gate step\'s median time over full gates may reach; prefix the id prefix of the record a plain run files for a step over it (unset: none is filed); session the session that record carries; unset: the check runs at 900 s and files nothing'),
    },
    # tools/module_map.py (kitlib.modulemap, docs/module-map.md).
    'modules': {
        'map': Field(RelPath(), None, 'the module map file: modules, messaging, allowed edges and ratchets (docs/module-map.md); --check refuses to run unless "owner".paths locks it; unset: no module map'),
        'ceilings': Field(RelPath(), None, 'the ratchet ceilings file, {ratchet id: ceiling} (docs/module-map.md "Ratchets"); unset: every ceiling is 0'),
        'page': Field(RelPath(), None, 'the architecture page generated from the map (docs/module-map.md "The architecture page"); --page --write writes it and --check fails when it does not match the map; unset: no page'),
    },
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
        'branch_template': Field(Either(BRANCH_TEMPLATE, ListOf(BRANCH_TEMPLATE, nonempty=True)), None, 'branch name for a slug, or a list of them (a worktree on any one is removable)'),
    },
}

# A section that may be a string naming a repo-relative JSON file. guard never: a file beside
# .kit.json would let a change to it widen what agent sessions may write past the kit-json rule.
# modules never: a file beside .kit.json would let an agent repoint the owner's module map.
FILE_OK = frozenset(SECTIONS) - {'repo', 'owner', 'hook', 'guard', 'modules'}
# Sections whose content is a table the kit does not shape-check (an object of anything).
OPAQUE = frozenset(('forbidden', 'pieces'))
# Sections that are a map of one entry type: role name -> permissions, over policy.ROLE_DEFAULTS.
MAPS = {'roles': (ROLE, 'role', 'agent type (or main, subagent) -> permissions, over the kit defaults')}


# Keys no tool reads any more, per section. A config that still carries one loads: the key is
# dropped unchecked (not an unknown-key refusal), so an install written before the removal works.
RETIRED = {'backlog': ('retired_docs', 'rename_script', 'ledger_repo'), 'landing': ('lanes', 'lock_file')}
# Whole sections no tool reads any more. A config that still carries one loads: the section is
# dropped unchecked, whatever it holds. Any other unknown section still refuses.
RETIRED_SECTIONS = ('ruling_cites', 'stubs')


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
