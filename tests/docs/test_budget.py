"""kitlib.docs.budget: the doc budget check (from the PM kit's tools/test_doc_budget.py and the
doc_budget half of tools/test_doc_tools_git.py; docs/doc-tools.md "doc_budget").

Two layers, because the arithmetic and the wiring fail differently: the pure functions take sizes
directly; the rest runs budget.run() over a real throwaway repository (tests.gitfixture.TempRepo),
because the defects these pin were in which sizes and which refs were ever compared.

What changed from the source tests, and why:
  - The configuration is `.kit.json` INSIDE the fixture repository (the source kept a
    tools/doc_budget.json outside it), so each ref carries its own copy and the owner's identity
    is read from the head's committed copy.
  - A key written twice is a kitlib.config ConfigError: on disk it refuses (exit 2), at the head it
    fails the branch (exit 1) unless the base carries the same error, on the base it is a WARNING.
  - Unknown keys and wrongly typed values are the schema's refusals (exit 2, kitlib.config.schema).
  - Moved out of this check, so their source tests went with them: the projection of a merge (the
    landing gate measures the real merge), the owner-only paths (tools/commit.py and
    kitlib.guard.ownercommits) and the index listing (kitlib.docs.index).
  - Dropped: the git process counts by trace2 (replaced by counts of budget.git calls), the
    "config read by one git process" case (config reads are kitlib.config.load_at's), the
    cfgs_at / KIT_JSON / DuplicateKey units (gone with the legacy reader), the unparseable
    cat-file reply (kitlib.git.batch's own tests), the pin of the kit's shipped doc_budget.json
    (no such file here; the caps example is a pure test below) and main()'s SystemExit handling
    (run() returns the status).
"""
import json
import os
import unittest
from unittest import mock

from kitlib.docs import budget
from kitlib.git import cmd
from tests.gitfixture import EMAIL, TempRepo

BUDGET = 8192
GOTCHAS = 'docs/ops/gotchas.md'
OWNER = EMAIL                         # the fixture repos commit as this identity
AGENT = 'agent@example.invalid'
LOCKED = ['CLAUDE.md', '.claude/']    # owner-only paths that leave .kit.json editable on a branch
NEW = 'docs/ops/new.md'
INDEX0 = '`docs/INDEX.md` | index | always. `%s` | traps | once | kw: t' % GOTCHAS


def gotchas_cfg(**over):
    cfg = dict(default=40960, budgets={GOTCHAS: BUDGET}, shard_indexes={}, state_docs=[])
    cfg.update(over)
    return cfg


def kit_json(doc_budget, owner_paths=LOCKED, email=OWNER, main='master', **owner_extra):
    """A .kit.json: main branch `main`, the owner section (paths omitted with None: the defaults;
    email omitted with None: unconfigured) and the doc_budget section."""
    owner = dict(owner_extra)
    if owner_paths is not None:
        owner['paths'] = list(owner_paths)
    if email is not None:
        owner['email'] = email
    return {'repo': {'main_branch': main}, 'owner': owner, 'doc_budget': doc_budget}


class RepoCase(unittest.TestCase):
    """A TempRepo whose first commit holds only `.kit.json` (README.md removed, so every .md is the
    test's own), and the helpers the source tests used."""

    BRANCH = 'master'

    def make_repo(self, kit):
        text = json.dumps(kit)

        def prepare(repo):          # built once per process per kit; each test gets a copy
            with open(repo.path('.kit.json'), 'wb') as fh:
                fh.write(text.encode())
            repo.git('rm', '-q', 'README.md')
            repo.git('add', '.kit.json')
            repo.git('commit', '-q', '-m', 'fixture')
        self.fixture = TempRepo(branch=self.BRANCH, prepare=prepare,
                                prepare_key=('test_budget.make_repo', text))
        self.addCleanup(self.fixture.cleanup)
        self.root = self.fixture.root

    def write(self, path, body):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as fh:
            fh.write(body.encode())

    def git(self, *args):
        return self.fixture.git(*args)

    def size(self, path, n):
        """Write `path` at exactly n bytes and commit everything."""
        self.write(path, 'x' * n)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', '%s -> %d' % (path, n))

    def run_budget(self, *argv):
        """(exit status, every line joined) of budget.run() with the CLI's options."""
        kw, it = {}, iter(argv)
        for a in it:
            if a in ('--base', '--head'):
                kw[a[2:]] = next(it)
            elif a in ('--worktree', '--report'):
                kw[a[2:]] = True
            else:
                raise AssertionError('unknown option %s' % a)
        got = []
        code = budget.run(self.root, got.append, **kw)
        return code, ''.join(line + '\n' for line in got)

    def failing(self, match, code=128, stdout='', stderr='fatal: injected failure'):
        """A patcher: budget.git with every call `match(args)` accepts answering (code, stdout, stderr)."""
        real = budget.git

        def git(root, *args):
            if match(args):
                return budget.Git(code, stdout, stderr)
            return real(root, *args)
        return mock.patch.object(budget, 'git', git)

    def counting(self):
        """(patcher, calls): budget.git recording each call's arguments."""
        real, calls = budget.git, []

        def git(root, *args):
            calls.append(args)
            return real(root, *args)
        return mock.patch.object(budget, 'git', git), calls


class Verdict(unittest.TestCase):
    """budget_verdict(path, budget, head, base): None when the file may land, otherwise the report.
    `head` is the size on the branch (or disk), `base` on master. Nothing projects the merge."""

    def verdict(self, head, base, budget_=BUDGET):
        return budget.budget_verdict(GOTCHAS, budget_, head, base, 'branch', 'master')

    def test_a_file_under_budget_passes(self):
        self.assertIsNone(self.verdict(4000, 3000))

    def test_two_under_budget_sides_are_not_summed(self):
        # The merge is measured where it is real (the landing gate's step on the merged tree).
        self.assertIsNone(self.verdict(8119, 7702))

    def test_an_already_over_file_that_holds_still_passes(self):
        self.assertIsNone(self.verdict(9000, 9000))

    def test_an_already_over_file_that_shrinks_on_the_branch_passes(self):
        self.assertIsNone(self.verdict(8600, 9000))

    def test_an_already_over_file_that_grows_by_ten_bytes_fails(self):
        self.assertIsNotNone(self.verdict(9010, 9000))

    def test_over_on_the_branch_reports_every_term(self):
        v = self.verdict(8300, 4000)
        self.assertEqual(v, '  %s: over budget on the branch by 108 bytes.\n'
                            '      budget 8192 | branch 8300 | master 4000' % GOTCHAS)
        self.assertNotIn('merge', v)

    def test_no_verdict_names_a_destructive_remedy(self):
        self.assertNotIn('checkout', self.verdict(9000, 7702))

    def test_a_new_file_is_measured_at_its_full_size(self):
        self.assertIsNotNone(self.verdict(9000, 0))
        self.assertIsNone(self.verdict(8000, 0))

    def test_uncapped_never_reports_over_however_large(self):
        self.assertIsNone(self.verdict(9_000_000, 0, budget_=None))

    def test_uncapped_growth_still_passes(self):
        self.assertIsNone(self.verdict(50000, 10000, budget_=None))


class BudgetFor(unittest.TestCase):
    def cfg(self, **over):
        c = {'default': None, 'budgets': {'docs/STATUS.md': None, 'CLAUDE.md': 10240}}
        c.update(over)
        return c

    def test_explicit_null_is_uncapped(self):
        self.assertIsNone(budget.budget_for(self.cfg(), 'docs/STATUS.md'))

    def test_explicit_number_wins(self):
        self.assertEqual(budget.budget_for(self.cfg(), 'CLAUDE.md'), 10240)

    def test_unrelated_path_is_uncapped(self):
        self.assertIsNone(budget.budget_for(self.cfg(), 'docs/architecture.md'))

    def test_a_numeric_default_still_applies_to_an_unlisted_path(self):
        self.assertEqual(budget.budget_for(self.cfg(default=100), 'x/y.md'), 100)

    def test_the_documented_capped_example_is_usable(self):
        # The PM kit's shipped caps (its tools/doc_budget.json), written as docs/doc-tools.md shows
        # them: only CLAUDE.md and the two profiles are capped, no "default".
        cfg = {'capped': ['CLAUDE.md', '.claude/agent-profile.md', '.claude/coordinator-profile.md'],
               'budgets': {'CLAUDE.md': 11264, '.claude/agent-profile.md': 12288,
                           '.claude/coordinator-profile.md': 8704}}
        self.assertEqual(budget.config_errors(cfg), [])
        self.assertEqual(budget.budget_for(cfg, 'CLAUDE.md'), 11264)
        for path in ('docs/reference/API_PLAN.md', 'docs/ops/gotchas.md', 'docs/backlog/ABC-001.md'):
            self.assertIsNone(budget.budget_for(cfg, path))


class KitBudgetFor(unittest.TestCase):
    def cfg(self, **over):
        c = {'default': 40960, 'budgets': {'docs/LOG.md': None, 'CLAUDE.md': 10240},
             'uncapped_dirs': ['docs/backlog']}
        c.update(over)
        return c

    def test_unrelated_path_gets_the_default(self):
        self.assertEqual(budget.budget_for(self.cfg(), 'docs/architecture.md'), 40960)

    def test_a_path_under_an_uncapped_dir_is_uncapped(self):
        self.assertIsNone(budget.budget_for(self.cfg(), 'docs/backlog/ABC-001.md'))

    def test_a_sibling_path_that_only_shares_the_prefix_string_is_not_uncapped(self):
        self.assertEqual(budget.budget_for(self.cfg(), 'docs/backlog-notes.md'), 40960)

    def test_an_explicit_entry_under_the_uncapped_dir_still_wins(self):
        cfg = self.cfg(budgets={'docs/backlog/DESIGN.md': 4096})
        self.assertEqual(budget.budget_for(cfg, 'docs/backlog/DESIGN.md'), 4096)
        self.assertIsNone(budget.budget_for(cfg, 'docs/backlog/ABC-001.md'))

    def test_no_uncapped_dirs_configured_is_fine(self):
        self.assertEqual(budget.budget_for({'default': 100, 'budgets': {}}, 'x/y.md'), 100)

    def test_no_default_leaves_an_unlisted_path_uncapped(self):
        self.assertIsNone(budget.budget_for({'budgets': {'CLAUDE.md': 10}}, 'docs/a.md'))

    def test_a_glob_budget_caps_every_file_under_a_shard_dir(self):
        cfg = self.cfg(budgets={'docs/decisions/*.md': 4096})
        self.assertEqual(budget.budget_for(cfg, 'docs/decisions/gamma.md'), 4096)
        self.assertEqual(budget.budget_for(cfg, 'docs/decisions/netcode.md'), 4096)

    def test_a_literal_entry_still_wins_over_a_glob_budget(self):
        cfg = self.cfg(budgets={'docs/decisions/*.md': 4096, 'docs/decisions/big.md': 8192})
        self.assertEqual(budget.budget_for(cfg, 'docs/decisions/big.md'), 8192)

    def test_a_glob_budget_does_not_match_a_sibling_path(self):
        cfg = self.cfg(budgets={'docs/decisions/*.md': 4096})
        self.assertEqual(budget.budget_for(cfg, 'docs/decisions-notes.md'), 40960)


class Pure(unittest.TestCase):
    def test_stale_budget_keys(self):
        cfg = {'budgets': {'a.md': 1, 'b.md': None, 'gone.md': 5}}
        self.assertEqual(budget.stale_budget_keys(cfg, ['a.md', 'b.md', 'c.md']), ['gone.md'])
        self.assertEqual(budget.stale_budget_keys({}, ['a.md']), [])
        cfg = {'budgets': {'gone.md': 5, 'docs/*.md': 1},
               'state_docs': ['a.md', 'gone.md', 'left.md']}
        self.assertEqual(budget.stale_budget_keys(cfg, ['a.md']), ['gone.md', 'left.md'])

    def test_sizes_from_reads_blobs_only_and_an_empty_listing_is_an_answer(self):
        listing = ('100644 blob %s     12\ta b.md\x00040000 tree %s      -\tdocs\0'
                   '160000 commit %s       -\tsub\0' % ('1' * 40, '2' * 40, '3' * 40))
        self.assertEqual(budget.sizes_from(listing), {'a b.md': 12})
        self.assertEqual(budget.sizes_from(''), {})

    def test_sizes_from_keeps_non_ascii_quote_and_newline_names_raw(self):
        listing = '\x00'.join(
            ['100644 blob %s     7\tdocs/café ü.md' % ('1' * 40),
             '100644 blob %s     9\ta"q.md' % ('2' * 40),
             '100644 blob %s    11\tline\nbreak.md' % ('3' * 40), ''])
        self.assertEqual(budget.sizes_from(listing),
                         {'docs/café ü.md': 7, 'a"q.md': 9, 'line\nbreak.md': 11})

    def test_names_from_splits_on_nul_only(self):
        self.assertEqual(budget.names_from('a b.md\0café.md\0line\nbreak.md\0'),
                         ['a b.md', 'café.md', 'line\nbreak.md'])
        self.assertEqual(budget.names_from(''), [])

    def test_section_of_drops_note_keys_from_the_section_and_its_budgets(self):
        class Conf:
            def raw(self, name):
                assert name == 'doc_budget'
                return {'_why': 'note', 'comment': 'c', 'default': 5,
                        'budgets': {'_note': 'n', 'a.md': 1, 'b.md': None}}
        self.assertEqual(budget.section_of(Conf()), {'default': 5, 'budgets': {'a.md': 1, 'b.md': None}})


class BudgetRepo(RepoCase):
    """master with `.kit.json` (gotchas capped at BUDGET, owner-only CLAUDE.md and .claude/)
    committed, and an uncommitted docs/INDEX.md with no newline."""

    def setUp(self):
        self.make_repo(kit_json(gotchas_cfg()))
        self.write('docs/INDEX.md', INDEX0)

    def configure(self, doc_budget, **kw):
        self.write('.kit.json', json.dumps(kit_json(doc_budget, **kw)))


class BranchSize(BudgetRepo):
    def test_a_branch_over_budget_on_its_own_fails(self):
        self.size(GOTCHAS, 5268)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.size(GOTCHAS, 9000)
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 1, out)
        self.assertIn('9000', out)

    def test_a_non_ascii_name_is_measured_at_its_real_size(self):
        # Without -z the listing C-quotes the name, the size lookup misses and reads 0.
        name = 'docs/café notes.md'
        self.size(GOTCHAS, 100)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.size(name, 50000)
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 1, out)
        self.assertIn(name, out)
        self.assertIn('50000', out)

    def test_an_over_budget_file_that_shrinks_on_the_branch_passes(self):
        self.size(GOTCHAS, 9000)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.size(GOTCHAS, 8600)
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)

    def test_an_over_budget_file_untouched_by_the_branch_passes(self):
        self.size(GOTCHAS, 9000)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.write('docs/INDEX.md', INDEX0 + '.')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'branch work elsewhere')
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)

    def test_the_worktree_mode_measures_the_same_way(self):
        self.size(GOTCHAS, 5268)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.size(GOTCHAS, 9000)
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 1, out)
        self.assertIn('budget 8192 | worktree 9000 | master 5268', out)

    def test_two_sides_under_budget_are_not_projected_into_a_merge(self):
        # Each side under the cap, their merge (10553) over it: the merged tree is measured where
        # it exists, by the landing gate's step, not projected here.
        self.size(GOTCHAS, 5268)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.size(GOTCHAS, 8119)
        self.git('checkout', '-q', 'master')
        self.size(GOTCHAS, 7702)
        self.git('checkout', '-q', 'branch-a')
        for argv in (('--worktree',), ('--base', 'master', '--head', 'branch-a')):
            code, out = self.run_budget(*argv)
            self.assertEqual(code, 0, out)
            self.assertNotIn('10553', out)

    def test_a_crlf_checkout_measures_like_the_committed_blob(self):
        self.size(GOTCHAS, 10)
        self.write(GOTCHAS, 'ab\r\ncd\r\n')
        code, out = self.run_budget('--worktree', '--report')
        self.assertIn('       6 /     8192  %s' % GOTCHAS, out)


class IndexExempt(BudgetRepo):
    def setUp(self):
        super().setUp()
        self.budgets({'docs/INDEX.md': None})
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'initial')

    def budgets(self, budgets):
        self.configure(gotchas_cfg(budgets=budgets))

    def test_a_large_index_passes(self):
        self.write('docs/INDEX.md', '`docs/INDEX.md` | index | always. | kw: index\n' + 'x' * 9_000_000)
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 0, out)
        self.assertIn('DOC BUDGET OK', out)

    def test_another_capped_doc_still_fails_over_budget(self):
        self.budgets({GOTCHAS: BUDGET, 'docs/INDEX.md': None})
        self.write(GOTCHAS, 'x' * 9000)
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 1, out)
        self.assertIn('9000', out)


class StaleBudgetKey(BudgetRepo):
    def setUp(self):
        super().setUp()
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'initial')

    def test_a_key_naming_no_file_fails_and_names_the_key(self):
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 1, out)
        self.assertIn('names no file', out)
        self.assertIn('  ' + GOTCHAS, out.splitlines())

    def test_the_same_key_passes_once_the_file_exists(self):
        self.write(GOTCHAS, 'x' * 10)
        code, out = self.run_budget('--worktree')
        self.assertNotIn('names no file', out)

    def test_a_state_docs_entry_naming_no_file_fails_too(self):
        self.write(GOTCHAS, 'x' * 10)
        self.configure(gotchas_cfg(state_docs=[GOTCHAS, 'docs/ops/gone.md']))
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 1, out)
        self.assertIn('names no file', out)
        self.assertIn('  docs/ops/gone.md', out.splitlines())
        self.assertNotIn('  ' + GOTCHAS, out.splitlines())


class StaleRepo(BudgetRepo):
    """Helpers: the doc_budget section committed per ref, so each side of a merge has its own."""

    def budget(self, n, other=None):
        self.configure(gotchas_cfg(budgets=dict({GOTCHAS: n}, **(other or {}))))
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'budget %d' % n)

    def touch_index(self, body='.'):
        self.write('docs/INDEX.md', INDEX0 + body)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'index')

    def stale_worktree(self):
        """branch-a checked out, forked before master touched INDEX.md."""
        self.budget(BUDGET)
        self.size(GOTCHAS, 5000)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.git('checkout', '-q', 'master')
        self.touch_index()
        self.git('checkout', '-q', 'branch-a')


class StaleBase(StaleRepo):
    def test_a_file_master_trimmed_under_a_lowered_budget_does_not_fail_a_branch_that_never_touched_it(self):
        self.budget(40960)
        self.size(GOTCHAS, 27641)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.touch_index()
        self.git('checkout', '-q', 'master')
        self.size(GOTCHAS, 3146)
        self.budget(4096)
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)

    def test_a_stale_config_does_not_fail_a_branch_master_raised_the_budget_for(self):
        self.budget(BUDGET)
        self.size(GOTCHAS, 5000)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.size(GOTCHAS, 5300)
        self.git('checkout', '-q', 'master')
        self.size(GOTCHAS, 8500)
        self.budget(10240)
        self.git('checkout', '-q', 'branch-a')
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 0, out)

    def test_a_stale_branch_growing_a_file_over_budget_on_its_own_still_fails(self):
        self.budget(BUDGET)
        self.size(GOTCHAS, 9000)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.size(GOTCHAS, 9100)
        self.git('checkout', '-q', 'master')
        self.touch_index()
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 1, out)
        self.assertIn('9100', out)

    def test_a_branch_that_raises_its_own_budget_sees_its_raise_at_the_landing_check(self):
        self.budget(BUDGET)
        self.size(GOTCHAS, 5000)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.budget(10240)
        self.size(GOTCHAS, 9000)
        self.git('checkout', '-q', 'master')
        self.budget(BUDGET, {'docs/other.md': 100})            # master moves its config too
        # The budgets applied are the head's (on disk under --worktree): the raise holds here.
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)
        self.git('checkout', '-q', 'branch-a')
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 0, out)

    def test_a_stale_worktree_growing_its_own_file_over_budget_still_fails(self):
        self.stale_worktree()
        self.write(GOTCHAS, 'x' * 9000)
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 1, out)
        self.assertIn('worktree 9000', out)

    def test_a_stale_worktree_with_a_new_untracked_file_over_budget_still_fails(self):
        self.stale_worktree()
        self.write('docs/INDEX.md', '`docs/INDEX.md` | x. `%s` | y. `docs/new.md` | z | kw: t' % GOTCHAS)
        self.write('docs/new.md', 'x' * 50000)
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 1, out)
        self.assertIn('docs/new.md', out)

    def test_an_unreadable_diff_against_the_merge_base_refuses(self):
        self.stale_worktree()
        with self.failing(lambda a: a[0] == 'diff' and '-z' in a):
            code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 2, out)
        self.assertIn('cannot diff', out)

    def test_an_unreadable_base_config_warns_and_names_the_ref(self):
        # The source removed master's json; a missing .kit.json is all defaults here, so master
        # commits one that does not read instead.
        self.stale_worktree()
        self.git('checkout', '-q', 'master')
        self.write('.kit.json', json.dumps(kit_json(gotchas_cfg(budget={}))))
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'break the config')
        self.git('checkout', '-q', 'branch-a')
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 0, out)
        self.assertIn('WARNING: cannot read the kit configuration at master', out)
        self.assertIn('unknown key(s) budget', out)

    def test_a_branch_that_lowers_its_own_budget_is_held_to_it(self):
        self.budget(10240)
        self.size(GOTCHAS, 5000)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.budget(BUDGET)
        self.size(GOTCHAS, 9000)
        self.git('checkout', '-q', 'master')
        self.touch_index()
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 1, out)


def dup_kit(budgets_text):
    """.kit.json text whose doc_budget "budgets" object is `budgets_text` verbatim."""
    return ('{"repo": {"main_branch": "master"}, "owner": {"paths": ["CLAUDE.md", ".claude/"], '
            '"email": "%s"}, "doc_budget": {"default": 40960, "budgets": {%s}, '
            '"shard_indexes": {}, "state_docs": []}}' % (OWNER, budgets_text))


class DuplicateBudgetKey(StaleRepo):
    """A key written twice is a ConfigError (kitlib.config): refused on disk, a failure where the
    branch brings it, a WARNING where the base already carries it."""

    def dup_json(self, *keys):
        self.write('.kit.json', dup_kit(', '.join('"%s": 8192, "%s": 10240' % (k, k) for k in keys)))

    def test_a_key_named_twice_on_disk_refuses_and_names_it(self):
        # The source failed this (exit 1); a configuration that does not read refuses (exit 2).
        self.budget(BUDGET)
        self.dup_json(GOTCHAS)
        self.write(GOTCHAS, 'x' * 9000)
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 2, out)
        self.assertIn('doc_budget: refused: kit configuration', out)
        self.assertIn('written twice in one object: %s' % GOTCHAS, out)
        self.assertNotIn('DOC BUDGET OK', out)

    def test_a_key_named_twice_at_a_ref_does_not_read_there(self):
        self.budget(BUDGET)
        self.dup_json(GOTCHAS)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'dup')
        conf, err = budget._cfg_at(self.root, 'master')
        self.assertIsNone(conf)
        self.assertIn('written twice in one object: %s' % GOTCHAS, err)
        self.assertFalse(err.startswith('master:'), err)

    def test_a_key_named_twice_at_the_head_fails_at_the_landing_check(self):
        self.budget(BUDGET)
        self.size(GOTCHAS, 5000)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.dup_json(GOTCHAS)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'dup on the branch')
        self.git('checkout', '-q', 'master')
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 1, out)
        self.assertIn('DOC BUDGET FAILED: the kit configuration in branch-a does not read', out)
        self.assertIn('written twice in one object: %s' % GOTCHAS, out)

    def base_with_a_duplicate(self):
        self.budget(BUDGET)
        self.size(GOTCHAS, 5000)
        self.dup_json(GOTCHAS)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'dup on master')
        self.git('checkout', '-q', '-b', 'branch-a')

    WARNING = 'WARNING: cannot read the kit configuration at master'

    def assertWarnsAndPasses(self, code, out):
        # The base's duplicate is a WARNING, not this branch's failure; whether the branch may edit
        # .kit.json is the owner-only check's question (commit.py, kitlib.guard.ownercommits).
        self.assertEqual(code, 0, out)
        self.assertIn(self.WARNING, out)
        self.assertIn('written twice in one object: %s' % GOTCHAS, out)
        self.assertNotIn('does not read', out)
        self.assertIn('DOC BUDGET OK', out.splitlines()[-1])

    def test_a_branch_removing_the_bases_duplicate_passes_with_the_warning(self):
        self.base_with_a_duplicate()
        self.budget(BUDGET)                                    # the fix: one entry
        code, out = self.run_budget('--worktree')
        self.assertWarnsAndPasses(code, out)
        # The landing check from master's checkout, where the duplicate is on disk: the source
        # passed it with the warning; here a configuration on disk that does not read refuses.
        self.git('checkout', '-q', 'master')
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 2, out)
        self.assertIn('doc_budget: refused: kit configuration', out)

    def test_report_names_its_tree_on_the_first_line_before_the_duplicate_warning(self):
        self.base_with_a_duplicate()
        self.budget(BUDGET)
        for argv, first in ((('--worktree', '--report'), 'measuring the working tree'),
                            (('--base', 'master', '--head', 'branch-a', '--report'),
                             'measuring branch-a as committed')):
            code, out = self.run_budget(*argv)
            self.assertWarnsAndPasses(code, out)
            self.assertTrue(out.splitlines()[0].startswith(first), out)

    def test_a_merge_bases_duplicate_the_base_fixed_exempts_nothing(self):
        self.base_with_a_duplicate()
        self.git('checkout', '-q', 'master')
        self.budget(BUDGET)                                    # master's fix
        self.git('checkout', '-q', 'branch-a')
        self.write('.kit.json', dup_kit('"%s": 10240, "%s": 9000' % (GOTCHAS, GOTCHAS)))
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'branch rewrites the duplicate')
        self.git('checkout', '-q', 'master')
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 1, out)
        self.assertIn('DOC BUDGET FAILED: the kit configuration in branch-a does not read', out)

    def test_a_branch_adding_another_duplicate_to_such_a_base_still_refuses(self):
        self.base_with_a_duplicate()
        self.write('docs/INDEX.md', '`docs/INDEX.md` | i | a. `%s` | t | o. `%s` | n | n | kw: t' % (GOTCHAS, NEW))
        self.size(NEW, 100)
        self.dup_json(GOTCHAS, NEW)
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 2, out)
        self.assertIn(NEW, out)
        self.assertNotIn('DOC BUDGET OK', out)


class MergeBaseLeg(StaleRepo):
    """A branch is held to the base's size, never to a projected merge: over its cap and bigger
    than on master fails it, in both modes, whatever master did meanwhile."""

    def trimmed_both_sides(self, branch, master, at_mb=8300):
        self.budget(BUDGET)
        self.size(GOTCHAS, at_mb)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.size(GOTCHAS, branch)
        self.git('checkout', '-q', 'master')
        self.size(GOTCHAS, master)
        self.git('checkout', '-q', 'branch-a')

    @staticmethod
    def line(i, ch='x'):
        return '%03d' % i + ch * 96 + '\n'                  # exactly 100 bytes

    def lines(self, keep, extra=None, msg='lines'):
        body = []
        for i in keep:
            body.append(self.line(i))
            body.extend((extra or {}).get(i, []))
        self.write(GOTCHAS, ''.join(body))
        self.git('add', '-A')
        self.git('commit', '-q', '-m', msg)

    def forked(self, branch, master):
        self.budget(BUDGET)
        self.lines(range(90))
        self.git('checkout', '-q', '-b', 'branch-a')
        branch()
        self.git('checkout', '-q', 'master')
        master()
        self.git('checkout', '-q', 'branch-a')

    def assertFailsBy(self, n):
        for argv, label in ((('--base', 'master', '--head', 'branch-a'), 'branch'),
                            (('--worktree',), 'worktree')):
            code, out = self.run_budget(*argv)
            self.assertEqual(code, 1, out)
            self.assertIn('the %s by %d bytes' % (label, n), out)
            self.assertNotIn('WARNING: %s is over' % GOTCHAS, out)

    def test_a_branch_that_trimmed_less_than_master_fails_on_its_own_size(self):
        # branch 8500, master 8000: their merge may well come under the cap, but nothing here
        # projects it; the landing gate measures the real one.
        self.forked(lambda: self.lines([i for i in range(90) if not 80 <= i < 85]),
                    lambda: self.lines([i for i in range(90) if not 10 <= i < 20]))
        self.assertFailsBy(308)

    def test_the_shared_cut_fails(self):
        self.forked(lambda: self.lines([i for i in range(90) if not 10 <= i < 20],
                                       {80: [self.line(900 + k, 'n') for k in range(6)]}),
                    lambda: self.lines([i for i in range(90) if not 10 <= i < 20 and i != 50]))
        self.assertFailsBy(408)

    def test_a_stale_branch_growing_a_file_over_its_cap_still_fails(self):
        self.trimmed_both_sides(8300, 7000, at_mb=8000)
        self.assertFailsBy(108)

    def test_an_over_cap_file_held_is_a_warning_and_exits_0(self):
        self.budget(BUDGET)
        self.size(GOTCHAS, 9000)
        for argv in ((), ('--worktree',)):
            code, out = self.run_budget(*argv)
            self.assertEqual(code, 0, out)
            self.assertIn('WARNING: %s is over its budget by 808 bytes (budget 8192' % GOTCHAS, out)
            self.assertIn('1 over cap, held - see WARNING)', out.splitlines()[-1])

    def test_an_over_cap_file_a_stale_branch_never_touched_is_a_warning(self):
        self.budget(BUDGET)
        self.size(GOTCHAS, 9000)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.touch_index()
        self.git('checkout', '-q', 'master')
        self.touch_index(' more')
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)
        self.assertIn('WARNING: %s is over its budget' % GOTCHAS, out)

    def test_report_says_it_measures_the_committed_head(self):
        self.budget(BUDGET)
        self.size(GOTCHAS, 100)
        code, out = self.run_budget('--report')
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0],
                         'measuring HEAD as committed; for uncommitted edits run --worktree --report')
        code, out = self.run_budget('--worktree', '--report')
        self.assertTrue(out.splitlines()[0].startswith('measuring the working tree'), out)


class OnePass(BudgetRepo):
    """Every check runs, every failure is printed, and the last line names them all."""

    def faulty_branch(self):
        """branch-a: gotchas over budget and grown, a retired doc back, a budget naming no file."""
        self.size(GOTCHAS, 100)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.configure(gotchas_cfg(budgets={GOTCHAS: BUDGET, 'docs/gone.md': 10},
                                   retired_paths=['docs/old.md']))
        self.write('docs/old.md', 'retired')
        self.write(GOTCHAS, 'x' * 9000)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'three faults')

    def test_three_failures_are_all_named_and_the_last_line_lists_them(self):
        self.faulty_branch()
        for argv in (('--base', 'master', '--head', 'branch-a'), ('--worktree',)):
            code, out = self.run_budget(*argv)
            self.assertEqual(code, 1, out)
            self.assertIn('%s: over budget' % GOTCHAS, out)
            self.assertIn('  docs/old.md', out)
            self.assertIn('  docs/gone.md', out)
            last = out.splitlines()[-1]
            self.assertTrue(last.startswith('DOC BUDGET FAILED: 3 check(s) failed'), last)
            self.assertTrue(last.endswith('): over budget, retired path, stale budget key.'), last)

    def test_an_unexpected_error_after_a_failure_still_prints_the_failure_and_raises(self):
        self.faulty_branch()
        got = []

        def stale_budget_keys(cfg, files):
            got.append('stale_budget_keys said this first')
            raise RuntimeError('simulated crash in stale_budget_keys')
        with mock.patch.object(budget, 'stale_budget_keys', stale_budget_keys), \
                self.assertRaises(RuntimeError):
            budget.run(self.root, got.append, base='master', head='branch-a')
        out = '\n'.join(got)
        self.assertIn('stale_budget_keys said this first', out)
        self.assertIn('DOC BUDGET FAILED: over budget and grew on the branch.', out)
        self.assertLess(out.index('stale_budget_keys said'), out.index('DOC BUDGET FAILED'))
        self.assertNotIn('DOC BUDGET OK', out)

    def test_a_single_failure_still_exits_1_and_ends_on_the_summary(self):
        self.configure(gotchas_cfg(state_docs=['docs/gone.md']))
        self.size(GOTCHAS, 100)
        code, out = self.run_budget()
        self.assertEqual(code, 1, out)
        self.assertEqual(out.splitlines()[-1],
                         'DOC BUDGET FAILED: 1 check(s) failed (HEAD, 2 .md files): stale budget key.')

    def test_owner_only_paths_and_the_index_are_not_judged_here(self):
        # Moved out: owner-only paths to commit.py and kitlib.guard.ownercommits, the index to
        # kitlib.docs.index. An agent's edit of an owner-only path and an unlisted doc pass here.
        self.size(GOTCHAS, 100)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.write('CLAUDE.md', 'unapproved edit')
        self.write('docs/stray.md', 'not indexed')
        self.git('add', '-A')
        self.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m', 'no trailer')
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)
        self.assertIn('DOC BUDGET OK', out)


class Uncapped(RepoCase):
    """An explicit `null` budget beside a small capped one, end to end."""

    STATUS = 'docs/STATUS.md'
    CAPPED_BUDGET = 100

    def setUp(self):
        self.make_repo(kit_json(gotchas_cfg(budgets={self.STATUS: None, GOTCHAS: self.CAPPED_BUDGET})))
        self.write('docs/INDEX.md',
                   '`docs/INDEX.md` | index | always. `%s` | traps | once. `%s` | backlog | once | kw: t'
                   % (GOTCHAS, self.STATUS))

    def test_uncapped_file_far_over_default_passes(self):
        self.size(self.STATUS, 60000)
        self.size(GOTCHAS, 50)
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 0, out)

    def test_uncapped_file_that_grows_still_passes(self):
        self.size(self.STATUS, 60000)
        self.size(GOTCHAS, 50)
        self.size(self.STATUS, 90000)
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 0, out)

    def test_capped_file_behaviour_is_unchanged(self):
        self.size(self.STATUS, 500)
        self.size(GOTCHAS, 50)
        self.git('checkout', '-q', '-b', 'branch-a')
        self.size(GOTCHAS, self.CAPPED_BUDGET + 50)
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 1, out)
        self.assertIn(GOTCHAS, out)

    def test_report_renders_the_uncapped_marker_not_a_number(self):
        self.size(self.STATUS, 60000)
        self.size(GOTCHAS, 50)
        code, out = self.run_budget('--worktree', '--report')
        self.assertEqual(code, 0, out)
        status_line = next(l for l in out.splitlines() if l.endswith(self.STATUS))
        self.assertIn('uncapped', status_line)
        self.assertIn('60000', status_line)
        self.assertNotRegex(status_line, r'60000\s*/\s*\d')


class UncappedDirsRepo(RepoCase):
    """No "default": every unlisted .md is uncapped and a file with an explicit cap keeps it."""

    GLOB_LINE = '`docs/INDEX.md` | index | always. `docs/backlog/*.md` | one file per work item | never generated | kw: t'

    def setUp(self):
        self.make_repo(kit_json(self.cfg({})))

    @staticmethod
    def cfg(budgets):
        return dict(budgets=budgets, shard_indexes={}, state_docs=[])

    def budgets(self, budgets):
        self.write('.kit.json', json.dumps(kit_json(self.cfg(budgets))))

    def new_branch_on_top_of_index(self):
        self.size('docs/INDEX.md', 0)
        self.write('docs/INDEX.md', self.GLOB_LINE)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'index glob line')
        self.git('checkout', '-q', '-b', 'branch-a')

    def test_one_item_file_far_over_default_passes(self):
        self.new_branch_on_top_of_index()
        self.size('docs/backlog/ABC-001.md', 60000)
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)

    def test_many_item_files_in_aggregate_pass(self):
        self.new_branch_on_top_of_index()
        for i in range(20):
            self.write('docs/backlog/ITEM-%03d.md' % i, 'x' * 50000)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', '20 backlog items')
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)

    def test_a_file_with_an_explicit_cap_keeps_it(self):
        self.new_branch_on_top_of_index()
        self.budgets({GOTCHAS: BUDGET})
        self.size(GOTCHAS, BUDGET + 500)
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 1, out)
        self.assertIn('%s: over budget' % GOTCHAS, out)

    def test_a_non_md_file_needs_no_index_line_and_no_budget(self):
        self.new_branch_on_top_of_index()
        self.size('docs/backlog/MIGRATION.tsv', 60000)
        for argv in (('--worktree',), ('--base', 'master', '--head', 'branch-a')):
            code, out = self.run_budget(*argv)
            self.assertEqual(code, 0, out)
            self.assertNotIn('MIGRATION.tsv', out)


# --- The kit's own cases: "uncapped_dirs" and the refusals ---


class KitRepo(BudgetRepo):
    """BudgetRepo with the default owner section and INDEX_LINE in docs/INDEX.md, both
    uncommitted, before each test."""

    CONFIG = gotchas_cfg()
    INDEX_LINE = '`docs/INDEX.md` | index | always. `%s` | traps | once | kw: index, gotchas' % GOTCHAS

    def setUp(self):
        super().setUp()
        self.configure(self.CONFIG)
        self.write('docs/INDEX.md', self.INDEX_LINE)

    def configure(self, doc_budget, **kw):
        kw.setdefault('owner_paths', None)
        super().configure(doc_budget, **kw)


class Refusals(KitRepo):
    def test_an_unknown_base_refuses_with_exit_2(self):
        self.size(GOTCHAS, 10)
        code, out = self.run_budget('--base', 'no-such-branch')
        self.assertEqual(code, 2, out)
        self.assertIn('base ref no-such-branch does not exist here', out)
        self.assertNotIn('DOC BUDGET OK', out)

    def test_an_unreadable_config_refuses_with_exit_2(self):
        self.size(GOTCHAS, 10)
        self.write('.kit.json', '{broken')
        code, out = self.run_budget()
        self.assertEqual(code, 2, out)
        self.assertIn('doc_budget: refused: kit configuration', out)
        self.assertNotIn('DOC BUDGET OK', out)

    def test_an_unknown_key_refuses_with_exit_2(self):
        self.size(GOTCHAS, 10)
        self.configure(dict(self.CONFIG, budget={}))
        code, out = self.run_budget()
        self.assertEqual(code, 2, out)
        self.assertIn('unknown key(s) budget', out)
        self.assertNotIn('DOC BUDGET OK', out)

    def test_git_that_cannot_run_refuses_with_exit_2(self):
        self.size(GOTCHAS, 10)
        with mock.patch.object(budget.cmd, 'call',
                               return_value=cmd.Result(None, b'', 'could not run git: git missing')):
            code, out = self.run_budget()
        self.assertEqual(code, 2, out)
        self.assertIn('could not run: could not run git: git missing', out)


class UncappedDirs(KitRepo):
    CONFIG = dict(default=40960, budgets={}, uncapped_dirs=['docs/backlog'], shard_indexes={}, state_docs=[])
    GLOB_LINE = '`docs/INDEX.md` | index | always. `docs/backlog/*.md` | one file per work item | never | kw: index, backlog'
    INDEX_LINE = GLOB_LINE

    def new_branch_on_top_of_index(self):
        self.write('docs/INDEX.md', self.GLOB_LINE)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'index glob line')
        self.git('checkout', '-q', '-b', 'branch-a')

    def test_one_item_file_far_over_default_passes(self):
        self.new_branch_on_top_of_index()
        self.size('docs/backlog/ABC-001.md', 60000)
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)

    def test_many_item_files_in_aggregate_pass(self):
        self.new_branch_on_top_of_index()
        for i in range(20):
            self.write('docs/backlog/ITEM-%03d.md' % i, 'x' * 50000)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', '20 backlog items')
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 0, out)

    def test_a_file_outside_the_uncapped_dir_keeps_its_ordinary_cap(self):
        self.new_branch_on_top_of_index()
        self.configure(dict(self.CONFIG, budgets={GOTCHAS: BUDGET}))
        self.write(GOTCHAS, 'x' * (BUDGET + 500))
        self.write('docs/INDEX.md', self.GLOB_LINE + '\n`%s` | traps | once | kw: gotchas' % GOTCHAS)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'grow gotchas')
        code, out = self.run_budget('--base', 'master', '--head', 'branch-a')
        self.assertEqual(code, 1, out)
        self.assertIn('%s: over budget' % GOTCHAS, out)


# --- "capped" (only CLAUDE.md and the profiles carry a cap) and "retired_paths": main branch
# `main`, work on phase/x. ---

CAPPED = ['CLAUDE.md', '.claude/agent-profile.md', '.claude/coordinator-profile.md']
G_INDEX = ('`CLAUDE.md` | rules | keys: rules\n`docs/INDEX.md` | this index | keys: index\n'
           '`.claude/agent-profile.md` | profile | keys: profile\n'
           '`.claude/coordinator-profile.md` | profile | keys: profile\n')


def capped_config(caps=None, **extra):
    """A doc_budget section: every CAPPED path at 100000 bytes unless `caps` says otherwise."""
    budgets = dict.fromkeys(CAPPED, 100000)
    budgets.update(caps or {})
    return dict(extra, capped=list(CAPPED), budgets=budgets)


def capped_kit(cfg):
    return kit_json(cfg, owner_paths=None, main='main')


class CappedRepo(RepoCase):
    """main with one owner commit (CLAUDE.md, both profiles, the index, .kit.json), phase/x
    checked out."""

    BRANCH = 'main'

    def setUp(self):
        self.make_repo(capped_kit(capped_config()))
        for path, body in (('docs/INDEX.md', G_INDEX), ('CLAUDE.md', 'rules\n'),
                           ('.claude/agent-profile.md', 'profile\n'),
                           ('.claude/coordinator-profile.md', 'profile\n')):
            self.write(path, body)
        self.git('add', '-A')
        self.commit(OWNER, 'base')
        self.git('checkout', '-q', '-b', 'phase/x')

    def commit(self, author, message):
        self.git('-c', 'user.email=%s' % author, 'commit', '-q', '--author', 'test <%s>' % author,
                 '-m', message)

    def edit_claude(self, author, message):
        self.write('CLAUDE.md', 'rules\nnew rule\n')
        self.git('add', 'CLAUDE.md')
        self.commit(author, message)

    def write_config(self, cfg):
        self.write('.kit.json', json.dumps(capped_kit(cfg)))

    def set_config(self, cfg):
        """Commit `cfg` on main and re-fork phase/x from it."""
        self.git('checkout', '-q', 'main')
        self.write_config(cfg)
        self.git('add', '.kit.json')
        self.commit(OWNER, 'config')
        self.git('checkout', '-q', '-B', 'phase/x')

    def run_budget(self, *argv):
        return super().run_budget(*(argv or ('--head', 'phase/x')))


class CapTests(CappedRepo):
    CAP = 50

    def setUp(self):
        super().setUp()
        self.set_config(capped_config({'CLAUDE.md': self.CAP}))

    def base_doc(self, path, size):
        self.git('checkout', '-q', 'main')
        self.write(path, 'x' * (size - 1) + '\n')
        self.git('add', path)
        self.commit(OWNER, '%s on main' % path)
        self.git('checkout', '-q', '-B', 'phase/x')

    def owner_claude(self, size, char='x'):
        self.write('CLAUDE.md', char * (size - 1) + '\n')
        self.git('add', 'CLAUDE.md')
        self.commit(OWNER, 'trim rules\n\nOwner-Approved: CLAUDE.md')

    def agent_doc(self, path, size):
        self.write(path, 'x' * (size - 1) + '\n')
        self.git('add', path)
        self.commit(AGENT, 'edit %s' % path)

    def test_claude_md_over_cap_and_grew_fails(self):
        self.base_doc('CLAUDE.md', 40)
        self.owner_claude(60)
        code, out = self.run_budget()
        self.assertEqual(code, 1, out)
        self.assertIn('DOC BUDGET FAILED: over budget and grew', out)
        self.assertIn('  CLAUDE.md: over budget on the branch by 10 bytes.\n'
                      '      budget 50 | branch 60 | main 40', out)
        self.assertNotIn('merge', out)

    def test_claude_md_over_cap_but_held_passes(self):
        self.base_doc('CLAUDE.md', 60)
        self.owner_claude(60, char='y')
        code, out = self.run_budget()
        self.assertEqual(code, 0, out)
        self.assertIn('DOC BUDGET OK (phase/x, 4 .md files; 1 over cap, held - see WARNING)', out)

    def test_claude_md_over_cap_but_shrank_passes(self):
        self.base_doc('CLAUDE.md', 70)
        self.owner_claude(60)
        code, out = self.run_budget()
        self.assertEqual(code, 0, out)

    def test_profile_over_cap_and_grew_fails(self):
        self.set_config(capped_config({'.claude/agent-profile.md': self.CAP}))
        self.write('.claude/agent-profile.md', 'x' * 59 + '\n')
        self.git('add', '.claude/agent-profile.md')
        self.commit(OWNER, 'profile\n\nOwner-Approved: .claude/')
        code, out = self.run_budget()
        self.assertEqual(code, 1, out)
        self.assertIn('  .claude/agent-profile.md: over budget on the branch by 10 bytes', out)
        self.assertIn('budget 50 | branch 60 | main 8', out)

    def test_non_profile_doc_far_over_old_caps_passes(self):
        self.base_doc('docs/a.md', 40)
        self.write('docs/INDEX.md', G_INDEX + '`docs/a.md` | a doc | keys: a\n`docs/new.md` | new | keys: new\n')
        self.git('add', 'docs/INDEX.md')
        self.agent_doc('docs/a.md', 100000)
        self.agent_doc('docs/new.md', 100000)
        code, out = self.run_budget()
        self.assertEqual(code, 0, out)
        self.assertIn('DOC BUDGET OK (phase/x, 6 .md files)', out)

    def test_report_lists_sizes_and_caps(self):
        self.base_doc('docs/a.md', 40)
        self.write('docs/INDEX.md', G_INDEX + '`docs/a.md` | a doc | keys: a\n')
        self.git('add', 'docs/INDEX.md')
        self.agent_doc('docs/a.md', 45)
        code, out = self.run_budget('--head', 'phase/x', '--report')
        self.assertEqual(code, 0, out)
        self.assertIn('      45 / uncapped  docs/a.md', out)
        self.assertIn('       6 /       50  CLAUDE.md', out)

    def test_worktree_mode_matches_head_mode(self):
        self.base_doc('CLAUDE.md', 40)
        self.owner_claude(60)
        head = self.run_budget()
        tree = self.run_budget('--worktree')
        self.assertEqual(head[0], 1, head[1])
        self.assertEqual(tree[0], 1, tree[1])
        line = '  CLAUDE.md: over budget on the %s by 10 bytes.\n      budget 50 | %s 60 | main 40'
        self.assertIn(line % ('branch', 'branch'), head[1])
        self.assertIn(line % ('worktree', 'worktree'), tree[1])

    def test_worktree_mode_reads_an_uncommitted_uncapped_doc(self):
        # docs/unlisted.md has no index line: the index is kitlib.docs.index's check, not this one.
        self.base_doc('docs/a.md', 40)
        self.write('docs/INDEX.md', G_INDEX + '`docs/a.md` | a doc | keys: a\n')
        self.git('add', 'docs/INDEX.md')
        self.agent_doc('docs/a.md', 45)
        self.write('docs/unlisted.md', 'x\n')
        self.git('add', 'docs/unlisted.md')
        self.commit(AGENT, 'unlisted')
        head = self.run_budget('--head', 'phase/x', '--report')
        self.git('reset', '-q', '--soft', 'HEAD~2')   # the fixture's own repo: the same edits, uncommitted
        tree = self.run_budget('--worktree', '--report')
        for code, out in (head, tree):
            self.assertEqual(code, 0, out)
            self.assertIn('      45 / uncapped  docs/a.md', out)
            self.assertIn('       2 / uncapped  docs/unlisted.md', out)


class CapConfigTests(CappedRepo):
    """With "capped" set, a cap on any other path, a missing or null cap, or a "default" is a
    configuration error (exit 2)."""

    def run_with(self, cfg):
        self.write_config(cfg)
        return self.run_budget()

    def test_cap_on_another_doc_is_refused(self):
        code, out = self.run_with(capped_config({'docs/a.md': 50}))
        self.assertEqual(code, 2, out)
        self.assertIn('"budgets" names docs/a.md: only CLAUDE.md', out)

    def test_default_is_refused(self):
        code, out = self.run_with(capped_config(default=40960))
        self.assertEqual(code, 2, out)
        self.assertIn('"default" is gone', out)

    def test_missing_or_null_cap_is_refused(self):
        cfg = capped_config({'CLAUDE.md': None})
        code, out = self.run_with(cfg)
        self.assertEqual(code, 2, out)
        self.assertIn('"budgets" CLAUDE.md must be an integer byte cap', out)
        del cfg['budgets']['.claude/coordinator-profile.md']
        code, out = self.run_with(cfg)
        self.assertEqual(code, 2, out)
        self.assertIn('.claude/coordinator-profile.md must be an integer byte cap, not None', out)


class RetiredPathsTests(CappedRepo):
    def setUp(self):
        super().setUp()
        self.set_config(capped_config(retired_paths=['docs/DECISIONS.md', 'docs/decisions/*.md']))
        self.write('docs/INDEX.md', G_INDEX + '`docs/DECISIONS.md` | old | keys: old\n'
                   '`docs/decisions/*.md` | old | keys: old\n')
        self.git('add', 'docs/INDEX.md')
        self.commit(AGENT, 'index')

    def add(self, path):
        self.write(path, 'a ruling\n')
        self.git('add', path)

    def test_a_branch_without_a_retired_path_passes(self):
        code, out = self.run_budget()
        self.assertEqual(code, 0, out)
        self.assertIn('DOC BUDGET OK', out)

    def test_a_recreated_register_fails(self):
        self.add('docs/DECISIONS.md')
        self.commit(AGENT, 'bring the register back')
        code, out = self.run_budget()
        self.assertEqual(code, 1, out)
        self.assertIn('DOC BUDGET FAILED: retired path(s) exist on phase/x:', out)
        self.assertIn('  docs/DECISIONS.md', out)
        self.assertIn('"retired_paths" retires them', out)

    def test_a_recreated_shard_fails(self):
        self.add('docs/decisions/net.md')
        self.commit(AGENT, 'bring a shard back')
        code, out = self.run_budget()
        self.assertEqual(code, 1, out)
        self.assertIn('  docs/decisions/net.md', out)

    def test_worktree_mode_catches_an_uncommitted_or_untracked_one(self):
        self.write('docs/decisions/ui.md', 'untracked\n')
        code, out = self.run_budget('--worktree')
        self.assertEqual(code, 1, out)
        self.assertIn('retired path(s) exist on the working tree', out)
        self.assertIn('  docs/decisions/ui.md', out)

    def test_retired_paths_must_be_a_list_of_paths(self):
        # The schema's refusal now (kitlib.config.schema), not the tool's own sentence.
        for bad in ('docs/DECISIONS.md', [''], [3], None):
            self.write_config(capped_config(retired_paths=bad))
            code, out = self.run_budget()
            self.assertEqual(code, 2, (bad, out))
            self.assertIn('doc_budget: refused: kit configuration', out)
            self.assertIn('"doc_budget".retired_paths', out)


class FailedGitReads(BudgetRepo):
    def test_failed_merge_base_refuses(self):
        self.size(GOTCHAS, 100)
        for code, stderr in ((128, 'fatal: bad ref'), (1, 'fatal: interrupted'), (0, '')):
            with self.subTest(code=code, stderr=stderr):
                with self.failing(lambda a: a[0] == 'merge-base', code, '', stderr):
                    status, out = self.run_budget('--worktree')
                self.assertEqual(status, 2, out)
                self.assertIn('cannot find the merge base', out)
                self.assertIn('git exit %d' % code, out)
                if stderr:
                    self.assertIn(stderr, out)

    def test_no_common_history_warns_and_keeps_the_base(self):
        self.size(GOTCHAS, 100)
        with self.failing(lambda a: a[0] == 'merge-base', 1, '', ''):
            code, out = self.run_budget('--worktree')
        self.assertEqual(code, 0, out)
        self.assertIn('WARNING: git merge-base', out)
        self.assertIn('no common history', out)

    def test_failed_worktree_listing_names_the_git_error(self):
        self.size(GOTCHAS, 100)
        with self.failing(lambda a: a[0] == 'ls-files', stderr='fatal: cannot list tree'):
            code, out = self.run_budget('--worktree')
        self.assertEqual(code, 2, out)
        self.assertIn('git exit 128: fatal: cannot list tree', out)

    def test_failed_size_listing_names_the_git_error(self):
        self.size(GOTCHAS, 100)
        with self.failing(lambda a: a[:3] == ('ls-tree', '-r', '--long'), stderr='fatal: bad ref'):
            code, out = self.run_budget()
        self.assertEqual(code, 2, out)
        self.assertIn('cannot list the sizes at HEAD (git exit 128: fatal: bad ref)', out)

    def test_an_empty_listing_refuses_rather_than_passing(self):
        self.size(GOTCHAS, 100)
        with self.failing(lambda a: a[:2] == ('ls-tree', '-r') and '--name-only' in a, 0, '', ''):
            code, out = self.run_budget()
        self.assertEqual(code, 2, out)
        self.assertIn('git exit 0, but it listed no file', out)

    def test_base_ref_exit_one_is_absent_and_any_other_exit_is_a_failed_read(self):
        self.size(GOTCHAS, 100)
        verify = lambda a: a[:3] == ('rev-parse', '--verify', '--quiet')
        with self.failing(verify, code=1, stderr=''):
            code, out = self.run_budget('--worktree')
        self.assertEqual(code, 2, out)
        self.assertIn('base ref master does not exist here', out)
        with self.failing(verify):
            code, out = self.run_budget('--worktree')
        self.assertEqual(code, 2, out)
        self.assertIn('could not read base ref master (git exit 128: fatal: injected failure)', out)
        self.assertNotIn('does not exist', out)


class FailedDiffsNameTheCause(StaleRepo):
    def failing_diff(self, against_base):
        return self.failing(lambda a: a[:2] == ('diff', '--name-only') and a[4:5] == ('master',)
                            if against_base else
                            a[:2] == ('diff', '--name-only') and a[4:5] != ('master',),
                            stderr='fatal: injected diff failure')

    def test_failed_merge_base_diff_names_the_cause(self):
        self.stale_worktree()
        with self.failing_diff(against_base=False):
            code, out = self.run_budget('--worktree')
        self.assertEqual(code, 2, out)
        self.assertIn('cannot diff HEAD against the merge base', out)
        self.assertIn('(git exit 128: fatal: injected diff failure)', out)


class DocBudgetGitCalls(RepoCase):
    """The doc_budget half of the PM kit's test_doc_tools_git.py: on phase/x with n docs and n
    commits to CLAUDE.md, the git calls do not grow with n."""

    BRANCH = 'main'

    def build(self, n):
        self.make_repo({'owner': {'email': OWNER}, 'doc_budget': {'budgets': {'CLAUDE.md': 100000}}})
        index = ['# Doc index', '', '## Rules',
                 '- `CLAUDE.md` | rules | always | kw: rules',
                 '- `docs/INDEX.md` | this index | finding a doc | kw: index']
        self.write('docs/INDEX.md', '\n'.join(index) + '\n')
        self.write('CLAUDE.md', 'rules\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')
        self.git('checkout', '-q', '-b', 'phase/x')
        index += ['', '## Docs']
        for i in range(n):
            self.write('docs/d%d.md' % i, '# Doc %d\n\n## Index\n\n- Part\n\n## Part\n\ntext\n' % i)
            index.append('- `docs/d%d.md` | doc %d | reading it | kw: doc%d' % (i, i, i))
        self.write('docs/INDEX.md', '\n'.join(index) + '\n')
        self.git('add', '-A')
        self.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m', 'docs')
        for i in range(n):
            self.write('CLAUDE.md', 'rules\nrule %d\n' % i)
            self.git('add', '-A')
            self.git('commit', '-q', '-m', 'rule %d\n\nOwner-Approved: CLAUDE.md' % i)
        self.git('checkout', '-q', 'main')

    def calls(self, n):
        self.build(n)
        patcher, calls = self.counting()
        with patcher:
            code, out = self.run_budget('--head', 'phase/x')
        self.assertEqual(code, 0, out)
        self.assertIn('DOC BUDGET OK', out)
        return len(calls)

    def test_git_calls_do_not_grow_with_docs_or_commits(self):
        small = self.calls(2)
        large = self.calls(8)
        self.assertEqual(small, large, 'git calls: 2 docs -> %s, 8 docs -> %s' % (small, large))


if __name__ == '__main__':
    unittest.main()
