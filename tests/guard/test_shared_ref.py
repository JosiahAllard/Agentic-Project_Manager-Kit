"""Shared refs (docs/guard.md "Rules", git-shared-ref): every worktree of a repository shares its
refs, so `git update-ref` run from any checkout can move or delete main, another session's branch,
a tag or the backlog store. An agent session (a subagent, the lander included, or a jailed mark
but pm) writes only refs/tmp/<name>, the per-worktree refs (refs/bisect/, refs/worktree/,
refs/rewritten/, the <NAME>_HEAD pseudorefs), and in a linked worktree its HEAD and the branch
checked out there, unless that branch is a guarded main. The owner's own session and a session
the jail does not hold are unchanged.

The world's git directory is real folders: HEAD files are written where git keeps them, so the
rule reads them as it reads a real repository's."""
import os
import unittest

from . import test_jail as tj
from . import world as W
from .claudehost import decide
from .world import event

RULE = 'git-shared-ref'


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class SharedRefBase(tj.MainGit):
    """MainGit (the lander may edit a main checkout) with HEAD files: the main checkout on main,
    the session's worktree on kit/wt, its sibling on kit/sib."""

    def setUp(self):
        tj.MainGit.setUp(self)
        self.common = os.path.join(self.w.main, '.git')
        self.wt_git = os.path.join(self.common, 'worktrees', 'wt')
        put(os.path.join(self.common, 'HEAD'), 'ref: refs/heads/main\n')
        put(os.path.join(self.wt_git, 'HEAD'), 'ref: refs/heads/kit/wt\n')
        put(os.path.join(self.common, 'worktrees', 'sib', 'HEAD'), 'ref: refs/heads/kit/sib\n')
        self.wt = W.fwd(self.w.wt)

    def worker(self, text, cwd=None, tool='Bash', agent_type='worker'):
        ev = event(tool, {'command': text, 'description': 'test'}, cwd or self.w.wt, True, agent_type)
        return decide(ev, self.w.ctx(environ={}))

    def session(self, text, env, cwd=None, tool='Bash'):
        ev = event(tool, {'command': text, 'description': 'test'}, cwd or self.w.wt)
        return decide(ev, self.w.ctx(environ=env))

    def refused(self, d, rule=RULE):
        return self.jailed(d, rule)

    def each_refused(self, texts, **kw):
        for text in texts:
            with self.subTest(text):
                self.refused(self.worker(text, **kw))

    def each_allowed(self, texts, **kw):
        for text in texts:
            with self.subTest(text):
                self.allowed(self.worker(text, **kw))


class SharedRefsFromAWorktree(SharedRefBase):
    def test_a_worker_may_not_delete_main_from_its_worktree(self):
        # the hole: refs are shared, so -C <own worktree> reaches main as surely as the main checkout
        self.each_refused(['git -C "%s" update-ref -d refs/heads/main' % self.wt,
                           'git update-ref -d refs/heads/main',
                           'git update-ref refs/heads/main HEAD~1',
                           'git update-ref -m why refs/heads/main 0123abc',
                           'git update-ref --no-deref refs/heads/main HEAD',
                           'git update-ref --create-reflog refs/heads/main HEAD',
                           'git.exe update-ref -d refs/heads/main',
                           'cd "%s" && git update-ref -d refs/heads/main' % self.wt])
        self.each_refused(['git update-ref -d refs/heads/main', '& git.exe update-ref refs/heads/main HEAD'],
                          tool='PowerShell')

    def test_a_wrapped_main_delete_reaches_the_rule(self):
        self.each_refused(['bash -c "git update-ref -d refs/heads/main"', "sh -c 'git update-ref -d refs/heads/main'",
                           'env git update-ref -d refs/heads/main', 'command git update-ref -d refs/heads/main',
                           'git update-ref -d refs/heads/main 2>/dev/null || true',
                           'git update-ref -- refs/heads/main HEAD', 'git update-ref -d -- refs/heads/main'])
        self.each_refused(['Start-Process git -ArgumentList "update-ref -d refs/heads/main"'], tool='PowerShell')

    def test_a_subcommand_the_text_does_not_name_is_refused(self):
        for env in (None, tj.COORDINATOR):
            with self.subTest(env=env):
                text = 'git $X -d refs/heads/main'
                d = self.worker(text) if env is None else self.session(text, env)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual(d.verdict, 'deny', d.reason)

    def test_every_other_shared_ref_is_refused(self):
        self.each_refused(['git update-ref refs/heads/kit/sib HEAD',        # another session's branch
                           'git update-ref refs/heads/kit/new HEAD',        # a branch nobody has checked out
                           'git update-ref refs/heads/master HEAD',
                           'git update-ref refs/tags/v1 HEAD', 'git update-ref -d refs/tags/v1',
                           'git update-ref refs/remotes/origin/main HEAD',
                           'git update-ref -d refs/remotes/origin/main',
                           'git update-ref refs/backlog/store HEAD', 'git update-ref -d refs/backlog/store',
                           'git update-ref refs/stash HEAD', 'git update-ref -d refs/stash',
                           'git update-ref refs/notes/commits HEAD',
                           'git update-ref refs/heads/MAIN HEAD',           # one file with main on NTFS
                           'git update-ref REFS/heads/main HEAD',
                           'git update-ref main HEAD',                      # git writes .git/main
                           'git update-ref main-worktree/HEAD HEAD',        # the main checkout's HEAD
                           'git update-ref worktrees/sib/HEAD HEAD',        # the sibling's HEAD
                           'git update-ref worktrees/sib/refs/bisect/bad HEAD',
                           'git update-ref refs/tmp/../heads/main HEAD',
                           'git update-ref refs/tmp HEAD', 'git update-ref refs/tmp/ HEAD',
                           'git update-ref refs/tmp/x.lock HEAD'])

    def test_a_ref_the_text_does_not_name_is_refused(self):
        self.each_refused(['git update-ref -d "$R"', 'git update-ref "$R" HEAD', 'git update-ref $(echo x) HEAD',
                           'git update-ref --stdin', 'git update-ref --std', 'git update-ref -z --stdin',
                           'git update-ref --stdin < refs.txt', 'git update-ref --batch-updates --stdin',
                           'git update-ref', 'git update-ref -d', 'git update-ref -m why',
                           'git update-ref --frobnicate refs/tmp/x HEAD',
                           'echo refs/heads/main | xargs git update-ref -d',
                           'git update-ref "refs/tmp/{}" HEAD'])
        self.each_refused(['git update-ref $env:R HEAD'], tool='PowerShell')

    def test_a_place_the_guard_cannot_read_is_refused(self):
        m = self.main
        for text in ('git -C "$D" update-ref refs/tmp/x HEAD',
                     'GIT_DIR="%s/.git" git update-ref refs/tmp/x HEAD' % m,
                     'git --git-dir="%s/.git" update-ref refs/tmp/x HEAD' % m,
                     'git -C "%s" update-ref refs/tmp/x HEAD' % W.fwd(self.w.unknown)):
            with self.subTest(text):
                d = self.worker(text)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual(d.verdict, 'deny', d.reason)

    def test_its_own_head_and_branch_may_move(self):
        self.each_allowed(['git update-ref refs/heads/kit/wt 0123abc',
                           'git update-ref -m why refs/heads/kit/wt HEAD~1 HEAD',
                           'git update-ref HEAD HEAD~1',
                           'git update-ref --no-deref HEAD HEAD~1',          # detaches its own HEAD
                           'git -C "%s" update-ref refs/heads/kit/wt HEAD~1' % self.wt])

    def test_its_own_head_or_branch_is_not_deleted(self):
        # a delete of the branch checked out there leaves it unborn; branch -d is the way
        self.each_refused(['git update-ref -d refs/heads/kit/wt', 'git update-ref -d HEAD',
                           'git update-ref --no-deref -d HEAD'])

    def test_scratch_and_per_worktree_refs_may_be_written(self):
        self.each_allowed(['git update-ref refs/tmp/x HEAD', 'git update-ref -d refs/tmp/x',
                           'git update-ref refs/tmp/kit/before-merge 0123abc',
                           'git update-ref -m keep refs/tmp/x HEAD', 'git update-ref --create-reflog refs/tmp/x HEAD',
                           'git update-ref refs/bisect/bad HEAD', 'git update-ref -d refs/bisect/bad',
                           'git update-ref refs/worktree/x HEAD', 'git update-ref refs/rewritten/x HEAD',
                           'git update-ref ORIG_HEAD HEAD~1', 'git update-ref -d CHERRY_PICK_HEAD',
                           'git -C "%s" update-ref refs/tmp/x HEAD' % self.wt])
        self.each_allowed(['git update-ref refs/tmp/x HEAD', '& git.exe update-ref -d refs/tmp/x'], tool='PowerShell')

    def test_a_worktree_whose_branch_is_main_may_not_move_it(self):
        put(os.path.join(self.wt_git, 'HEAD'), 'ref: refs/heads/main\n')
        self.each_refused(['git update-ref HEAD HEAD~1', 'git update-ref refs/heads/main HEAD~1'])
        put(os.path.join(self.wt_git, 'HEAD'), 'ref: refs/heads/Main\n')     # main's file on NTFS
        self.each_refused(['git update-ref HEAD HEAD~1', 'git update-ref refs/heads/Main HEAD~1'])

    def test_a_head_the_guard_cannot_read_moves_nothing(self):
        os.remove(os.path.join(self.wt_git, 'HEAD'))
        self.each_refused(['git update-ref HEAD HEAD~1', 'git update-ref refs/heads/kit/wt HEAD~1'])
        self.each_allowed(['git update-ref refs/tmp/x HEAD'])
        put(os.path.join(self.wt_git, 'HEAD'), '0123456789012345678901234567890123456789\n')     # detached
        self.each_allowed(['git update-ref --no-deref HEAD HEAD~1'])
        self.each_refused(['git update-ref refs/heads/kit/wt HEAD~1'])

    def test_a_symbolic_ref_is_judged_by_where_it_points(self):
        put(os.path.join(self.common, 'refs', 'tmp', 'link'), 'ref: refs/heads/main\n')
        put(os.path.join(self.wt_git, 'ORIG_HEAD'), 'ref: refs/heads/kit/sib\n')
        self.each_refused(['git update-ref refs/tmp/link HEAD', 'git update-ref -d refs/tmp/link',
                           'git update-ref ORIG_HEAD HEAD'])
        # --no-deref writes the symbolic ref itself, which is the session's own scratch ref
        self.each_allowed(['git update-ref --no-deref refs/tmp/link HEAD', 'git update-ref --no-d refs/tmp/link HEAD'])
        put(os.path.join(self.common, 'refs', 'tmp', 'mine'), 'ref: refs/heads/kit/wt\n')
        self.each_allowed(['git update-ref refs/tmp/mine HEAD'])

    def test_a_link_inside_the_ref_store_is_refused(self):
        heads = os.path.join(self.common, 'refs', 'heads')
        os.makedirs(heads)
        try:
            import _winapi
            _winapi.CreateJunction(heads, os.path.join(self.common, 'refs', 'tmp'))
        except (ImportError, AttributeError, OSError) as e:
            self.skipTest('no junction: %s' % e)
        self.each_refused(['git update-ref refs/tmp/main HEAD', 'git update-ref -d refs/tmp/main'])

    def test_a_reftable_repository_is_refused(self):
        os.makedirs(os.path.join(self.common, 'reftable'))
        self.each_refused(['git update-ref refs/tmp/x HEAD', 'git update-ref HEAD HEAD~1'])

    def test_a_sibling_worktree_head_is_its_own(self):
        # git run in the sibling moves the branch checked out there, as a commit there would
        sib = W.fwd(self.w.sib)
        self.each_allowed(['git -C "%s" update-ref refs/heads/kit/sib HEAD~1' % sib])
        self.each_refused(['git -C "%s" update-ref refs/heads/kit/wt HEAD~1' % sib])

    def test_the_lander_is_held_too(self):
        m = self.main
        for text in ('git -C "%s" update-ref refs/heads/main HEAD~1' % m, 'git -C "%s" update-ref -d refs/heads/main' % m,
                     'git -C "%s" update-ref HEAD HEAD~1' % m, 'git -C "%s" update-ref refs/heads/kit/wt HEAD' % m,
                     'git update-ref refs/heads/main HEAD~1'):
            with self.subTest(text):
                self.refused(self.worker(text, agent_type='lander'))
        for cwd in (self.w.main, self.w.wt):
            with self.subTest(cwd=cwd):
                self.refused(self.worker('git update-ref -d refs/heads/main', cwd=cwd, agent_type='lander'))
        self.allowed(self.worker('git update-ref refs/tmp/x HEAD', agent_type='lander'))
        self.allowed(self.worker('git update-ref refs/tmp/x HEAD', cwd=self.w.main, agent_type='lander'))

    def test_a_worker_in_main_keeps_the_worktree_hint_for_a_scratch_ref(self):
        # refs/tmp passes this rule; a role that may not change a main checkout still may not there
        d = self.refused(self.worker('git -C "%s" update-ref refs/tmp/x HEAD' % self.main), 'git-main-history')
        self.assertIn('Instead: git -C "%s" update-ref refs/tmp/x HEAD [' % self.wt, d.reason)
        self.allowed(self.worker(d.instead))
        d = self.refused(self.worker('git update-ref -d refs/tmp/x', cwd=self.w.main), 'git-main-history')
        self.assertIn('worktree add', d.instead)


class SharedRefSessions(SharedRefBase):
    """Which sessions the rule holds."""

    def test_coordinator_and_other_jailed_marks_are_held(self):
        for env in (tj.COORDINATOR, tj.OLD_MARK, tj.DEEP, tj.CONSULTANT):
            for cwd in (self.w.main, self.w.wt):
                with self.subTest(env=env, cwd=cwd):
                    self.refused(self.session('git update-ref refs/heads/main HEAD~1', env, cwd))
                    self.refused(self.session('git update-ref -d refs/heads/kit/sib', env, cwd))
                    self.allowed(self.session('git update-ref refs/tmp/x HEAD', env, cwd))

    def test_pm_owner_and_plain_sessions_are_not(self):
        for env in (tj.PM, {}, tj.PLAIN):
            with self.subTest(env=env):
                self.allowed(self.session('git update-ref refs/heads/main HEAD~1', env, self.w.main))
                self.allowed(self.session('git update-ref -d refs/heads/kit/sib', env, self.w.main))

    def test_kit_tools_and_words_in_text_are_not_update_ref(self):
        kit = W.fwd(self.w.kit_dir)
        for text in ('python "%s/tools/sync_base.py" -C "%s"' % (kit, self.wt),
                     'python "%s/tools/land.py" -C "%s" kit/wt --expect-tip %s' % (kit, self.main, W.SHA_WT),
                     'git commit -m "guard: refuse git update-ref refs/heads/main"',
                     'echo "git update-ref -d refs/heads/main"',
                     'git log --grep update-ref'):
            with self.subTest(text):
                self.allowed(self.worker(text))


class SharedRefMessages(SharedRefBase):
    def test_an_update_shows_the_ref_and_names_the_scratch_space(self):
        d = self.refused(self.worker('git update-ref refs/heads/main HEAD~1'))
        self.assertIn('every worktree of this repository shares', d.reason)
        self.assertIn('refs/tmp/<name>', d.reason)
        self.assertIn('Instead: git -C "%s" show-ref refs/heads/main [' % self.wt, d.reason)
        self.allowed(self.worker(d.instead))

    def test_a_delete_keeps_the_ref_delete_instead(self):
        d = self.refused(self.worker('git update-ref -d refs/heads/kit/old'))
        self.assertIn('Instead: git -C "%s" branch -d kit/old [' % self.wt, d.reason)
        self.allowed(self.worker(d.instead))
        d = self.refused(self.worker('git update-ref -d refs/tags/v1'))
        self.assertIn('Instead: git -C "%s" show-ref refs/tags/v1 [' % self.wt, d.reason)

    def test_main_is_never_offered_to_branch_d(self):
        d = self.refused(self.worker('git update-ref -d refs/heads/main'))
        self.assertNotIn('branch -d', d.instead)
        self.assertIn('Instead: git -C "%s" show-ref refs/heads/main [' % self.wt, d.reason)
        self.assertIn('main branch', d.reason)

    def test_an_unread_ref_says_so(self):
        d = self.refused(self.worker('git update-ref --stdin'))
        self.assertIn('cannot tell which ref', d.reason)
        self.allowed(self.worker(d.instead.replace('<name>', 'x').replace('<new-oid>', 'HEAD')))


class SharedRefFallback(SharedRefBase):
    """A line the parser cannot read cleanly is read again where it names update-ref."""

    def test_an_unclean_line_hiding_a_main_delete(self):
        text = "echo 'x ; git update-ref -d refs/heads/main"
        self.refused(self.worker(text))
        self.refused(self.session(text, tj.COORDINATOR))

    def test_an_unclean_update_ref_in_main_for_the_owner(self):
        d = self.session("echo 'x ; git update-ref refs/heads/main HEAD", {}, self.w.main)
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', 'unreadable'), d.reason)

    def test_an_unclean_line_in_a_worktree_for_the_owner_is_unchanged(self):
        self.allowed(self.session("echo 'x ; git update-ref refs/heads/main HEAD", {}, self.w.wt))


if __name__ == '__main__':
    unittest.main()
