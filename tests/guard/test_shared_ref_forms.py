"""git-shared-ref beyond update-ref (docs/guard.md "Rules"): a push into the repository itself,
a fetch or pull refspec's destination, symbolic-ref, replace, a notes write, a forced or moved
branch or tag, checkout -B, a checkout past another worktree's lock, worktree add -B or -f, a forced
tag fetch, rebase --update-refs, a remote's removal, rename or head, fast-import and filter-branch
move refs every worktree shares as update-ref does, so each is judged by the same allowance:
refs/tmp/<name>, the per-worktree refs, and in a linked worktree its HEAD and the branch checked
out there."""
import os
import re
import time
import unittest

from kitlib.guard import fallback, rules

from . import test_jail as tj
from .test_shared_ref import RULE, SharedRefBase, put


class PushIntoItself(SharedRefBase):
    def denied(self, texts, **kw):
        """git-push-main (or any rule before git-shared-ref) refuses these already."""
        for text in texts:
            with self.subTest(text):
                d = self.worker(text, **kw)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual(d.verdict, 'deny', d.reason)

    def test_a_push_into_this_repository_moves_a_shared_ref(self):
        m = self.main
        self.each_refused(['git push . HEAD:refs/heads/kit/sib',
                           'git push . +HEAD:refs/heads/kit/sib',
                           'git push . :refs/heads/kit/sib',
                           'git push -d . refs/tags/v1',
                           'git push --delete . refs/heads/kit/sib',
                           'git push . HEAD:refs/tags/v1',
                           'git push "%s" HEAD:refs/heads/kit/sib' % m,
                           'git push "%s/.git" HEAD:refs/heads/kit/sib' % m,
                           'git push "file:///%s" HEAD:refs/heads/kit/sib' % m,
                           'git -C "%s" push . HEAD:refs/heads/kit/sib' % self.wt,
                           'git push . HEAD:refs/tmp/x HEAD:refs/heads/kit/sib'])

    def test_main_stays_refused(self):
        self.denied(['git push . HEAD:refs/heads/main', 'git push . HEAD:main', 'git push . main',
                     'git push . --all', 'git push --mirror .'])

    def test_a_push_into_itself_the_guard_cannot_read(self):
        self.each_refused(['git push . HEAD:kit/sib',               # git matches a short name there
                           'git push . --tags', 'git push .', 'git push --repo=.',
                           'git push . "$SPEC"', 'git push "$R" HEAD:refs/heads/kit/sib',
                           'git push "./$X" HEAD:refs/heads/kit/sib'])

    def test_its_own_branch_and_scratch_refs(self):
        self.each_allowed(['git push . HEAD:refs/heads/kit/wt', 'git push . +HEAD~1:refs/heads/kit/wt',
                           'git push . HEAD:refs/tmp/x', 'git push . :refs/tmp/x', 'git push -d . refs/tmp/x',
                           'git push . kit/wt'])                        # no `:`: a ref onto itself

    def test_another_repository_is_a_remote(self):
        other = os.path.join(self.w.temp, 'other')
        os.makedirs(os.path.join(other, '.git'))
        with open(os.path.join(other, '.git', 'HEAD'), 'w', encoding='utf-8') as f:
            f.write('ref: refs/heads/main\n')
        for text in ('git push "%s" HEAD:refs/heads/main' % other.replace('\\', '/'),
                     'git push https://example.invalid/x.git HEAD:refs/heads/main',
                     'git push host:x.git HEAD:refs/heads/main'):
            with self.subTest(text):
                d = self.worker(text)
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)

    def test_a_linked_worktrees_git_dir_opens_this_repository(self):
        # git opens a git dir with a commondir file as the repository that common dir is
        with open(os.path.join(self.wt_git, 'commondir'), 'w', encoding='utf-8') as f:
            f.write('../..\n')
        self.each_refused(['git push "%s" HEAD:refs/heads/kit/sib' % self.wt_git.replace('\\', '/')])

    def test_from_the_main_checkout_its_branch_is_not_its_own(self):
        self.each_refused(['git push . HEAD:refs/heads/kit/wt'], cwd=self.w.main)
        self.each_allowed(['git push . HEAD:refs/tmp/x'], cwd=self.w.main)


class FetchRefspecs(SharedRefBase):
    def test_a_fetch_into_a_shared_branch(self):
        self.each_refused(['git fetch origin main:main',
                           'git fetch origin +main:refs/heads/main',
                           'git fetch origin x:heads/main',
                           'git fetch --update-head-ok origin main:main',
                           'git fetch -u origin main:main',
                           'git fetch . HEAD:refs/heads/kit/sib',
                           'git fetch origin refs/heads/x:refs/tags/v1',
                           'git fetch origin main:refs/remotes/upstream/main',      # another remote's
                           'git fetch origin HEAD:refs/remotes/origin/HEAD',
                           'git fetch --depth 1 origin main:main',
                           'git fetch -j 2 origin main:main',
                           'git fetch --refmap=+refs/heads/*:refs/heads/* origin main',
                           'git -c remote.origin.fetch=+refs/heads/*:refs/heads/* fetch origin',
                           'git -c fetch.pruneTags=true fetch origin',
                           'git -c remote.origin.pruneTags=true fetch origin',
                           'git fetch -P origin', 'git fetch --prune-tags origin',
                           'git fetch --tags --force origin', 'git fetch -tf origin',
                           'git pull origin main:main', 'git pull --rebase origin main:main',
                           'git pull -s ours origin main:main'])

    def test_a_fetch_the_guard_cannot_read(self):
        self.each_refused(['git fetch --stdin origin', 'git fetch origin "$SRC:refs/heads/main"',
                           'git fetch origin "+$SPEC"', 'git fetch --bogus origin main:main'])

    def test_tracking_refs_its_branch_and_scratch_refs(self):
        self.each_allowed(['git fetch origin', 'git fetch origin main', 'git fetch --all',
                           'git fetch --prune origin', 'git fetch --tags origin',
                           'git fetch --multiple origin upstream',
                           'git fetch origin main:refs/remotes/origin/main',
                           'git fetch origin refs/heads/main:refs/remotes/origin/main',
                           'git fetch origin +refs/heads/*:refs/remotes/origin/*',
                           'git fetch origin main:refs/tmp/x',
                           'git fetch origin x:refs/heads/kit/wt', 'git fetch origin x:kit/wt',
                           'git fetch origin ^refs/heads/main',
                           'git fetch origin "$BRANCH"'])              # no `:`: FETCH_HEAD only
        for text in ('git pull origin main', 'git pull --rebase origin main', 'git pull'):
            with self.subTest(text):                # git-plain-commit judges a worker's merge
                d = self.worker(text)
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)

    def test_the_guard_reads_a_fetch_destination_as_git_does(self):
        for dst, ref in (('main', 'refs/heads/main'), ('heads/x', 'refs/heads/x'), ('tags/v1', 'refs/tags/v1'),
                         ('remotes/o/x', 'refs/remotes/o/x'), ('refs/notes/x', 'refs/notes/x'),
                         ('Refs/heads/x', 'refs/heads/Refs/heads/x')):
            self.assertEqual(rules.fetch_dst_ref(dst), ref)


class SymbolicRef(SharedRefBase):
    def test_a_symref_pointing_at_or_naming_a_shared_ref(self):
        self.each_refused(['git symbolic-ref HEAD refs/heads/main',
                           'git symbolic-ref HEAD refs/heads/kit/sib',
                           'git symbolic-ref -m why HEAD refs/heads/main',
                           'git symbolic-ref refs/heads/main refs/heads/kit/wt',
                           'git symbolic-ref refs/tmp/x refs/heads/main',         # a write through it moves main
                           'git symbolic-ref -d HEAD', 'git symbolic-ref --delete refs/heads/kit/wt',
                           'git symbolic-ref HEAD "$B"', 'git symbolic-ref --bogus HEAD refs/heads/main'])
        self.each_refused(['git symbolic-ref HEAD refs/heads/kit/wt'], cwd=self.w.main)

    def test_reads_and_its_own_head(self):
        self.each_allowed(['git symbolic-ref HEAD', 'git symbolic-ref --short HEAD', 'git symbolic-ref -q HEAD',
                           'git symbolic-ref HEAD refs/heads/kit/wt',
                           'git symbolic-ref -m why HEAD refs/heads/kit/wt',
                           'git symbolic-ref refs/tmp/x refs/tmp/y', 'git symbolic-ref -d refs/tmp/x'])

    def test_head_points_at_switch(self):
        d = self.refused(self.worker('git symbolic-ref HEAD refs/heads/main'))
        self.assertIn('points HEAD at refs/heads/main', d.reason)
        self.assertIn('Instead: git -C "%s" switch <branch> [' % self.wt, d.reason)


class Replace(SharedRefBase):
    def test_a_replace_ref_is_shared(self):
        self.each_refused(['git replace 0123abc 4567def', 'git replace -f 0123abc 4567def',
                           'git replace -d 0123abc', 'git replace --delete 0123abc',
                           'git replace --graft 0123abc', 'git replace --edit 0123abc',
                           'git replace --convert-graft-file', 'git replace "$A" "$B"'])

    def test_a_listing_reads(self):
        self.each_allowed(['git replace', 'git replace -l', 'git replace --list "01*"',
                           'git replace --format=long -l'])


class Notes(SharedRefBase):
    def test_a_notes_write_is_shared(self):
        self.each_refused(['git notes add -m x', 'git notes append -m x HEAD', 'git notes remove HEAD',
                           'git notes prune', 'git notes copy HEAD~1 HEAD', 'git notes merge x',
                           'git notes --ref=refs/tmp/x add -m x',          # git writes refs/notes/refs/tmp/x
                           'git notes --ref x edit', 'git notes "$VERB"', 'git notes -x add'])

    def test_a_notes_read(self):
        self.each_allowed(['git notes', 'git notes list', 'git notes show HEAD', 'git notes --ref x show',
                           'git notes get-ref', 'git notes --ref=y list HEAD'])


class Sessions(SharedRefBase):
    WRITES = ('git push . HEAD:refs/heads/kit/sib', 'git fetch origin main:main', 'git symbolic-ref HEAD refs/heads/main',
              'git replace 0123abc 4567def', 'git notes add -m x', 'git branch -f kit/sib HEAD',
              'git branch -m kit/wt kit/x', 'git tag -d v1', 'git checkout -B kit/sib',
              'git switch --ignore-other-worktrees kit/sib', 'git worktree add -B kit/sib .claude/worktrees/y',
              'git fetch -f origin tag v1', 'git rebase --update-refs main', 'git remote remove origin',
              'git fast-import')

    def test_coordinator_and_other_jailed_marks_are_held(self):
        for env in (tj.COORDINATOR, tj.DEEP, tj.CONSULTANT):
            for text in self.WRITES:
                with self.subTest(text, env=env):
                    self.refused(self.session(text, env))

    def test_the_pm_the_owner_and_an_unheld_mark_are_not(self):
        for env in (tj.PM, {}, tj.PLAIN):
            for text in self.WRITES:
                with self.subTest(text, env=env):
                    d = self.session(text, env)
                    self.assertTrue(d is None or d.rule != RULE, d and d.reason)


class Fallback(SharedRefBase):
    """A line the parser cannot read cleanly (an unclosed quote) is read again where it names one of
    these forms, as it is where it names update-ref (fallback.SHARED_REF_PATTERNS)."""
    UNCLEAN = "echo 'x ; "

    def each_unclean_refused(self, texts):
        for text in texts:
            for who, env in (('coordinator', tj.COORDINATOR), ('deep', tj.DEEP)):
                with self.subTest(text, who=who):
                    self.refused(self.session(self.UNCLEAN + text, env))
            with self.subTest(text, who='worker'):
                self.refused(self.worker(self.UNCLEAN + text))

    def test_a_push_into_itself(self):
        self.each_unclean_refused(['git push . HEAD:refs/heads/kit/sib', 'git push -d . refs/tags/v1',
                                   'git push "%s" HEAD:refs/heads/kit/sib' % self.main,
                                   'git push "file:///%s" HEAD:refs/tags/v1' % self.main,
                                   'git push --repo=.'])       # alone: git takes a word after it as the repository

    def test_a_fetch_or_pull_refspec(self):
        self.each_unclean_refused(['git fetch origin main:main', 'git fetch origin +x:refs/heads/kit/sib',
                                   'git pull origin main:main', 'git fetch -P origin',
                                   'git fetch --prune-tags origin', 'git fetch --stdin origin',
                                   'git -c remote.origin.fetch=+refs/heads/*:refs/heads/* fetch origin'])

    def test_a_symbolic_ref_write(self):
        self.each_unclean_refused(['git symbolic-ref HEAD refs/heads/main', 'git symbolic-ref -m why HEAD refs/heads/main',
                                   'git symbolic-ref -d HEAD', 'git symbolic-ref -dq HEAD',
                                   'git symbolic-ref --delete refs/heads/kit/wt'])

    def test_a_replace(self):
        self.each_unclean_refused(['git replace 0123abc 4567def', 'git replace -d 0123abc'])

    def test_a_notes_write(self):
        self.each_unclean_refused(['git notes add -m x', 'git notes --ref=x remove HEAD', 'git notes prune'])

    def test_a_fetch_option_git_reads_abbreviated(self):
        # git takes any unambiguous start of a long option: --ta is --tags, --for --force, --std --stdin
        self.each_unclean_refused(['git fetch --ta --for origin', 'git fetch --tags --fo origin',
                                   'git fetch --std origin', 'git fetch --prune-t origin',
                                   'git -c remote.origin.pruneTags=true fetch origin',
                                   'git -c fetch.pruneTags=true fetch origin',
                                   'git fetch -f origin tag v1', 'git fetch --force origin tag v1',
                                   'git fetch --fo origin tag v1', 'git pull --forc origin tag v1'])

    def test_an_option_that_only_starts_like_force_is_no_forced_tag(self):
        # --f is ambiguous to git (--filter, pull's --ff); --filter and --foo are no --force
        for text in ('git fetch --filter blob origin tag v1', 'git fetch --foo origin tag v1',
                     'git pull --ff origin tag v1', 'git fetch --forced origin tag v1'):
            with self.subTest(text):
                self.assertEqual([p for p in fallback.SHARED_REF_PATTERNS if re.search(p, text)], [])

    def test_a_push_to_a_computed_or_home_repository(self):
        self.each_unclean_refused(['git push "$R" HEAD:refs/heads/kit/sib', 'git push $R HEAD:refs/heads/kit/sib',
                                   'git push ~/../repo HEAD:refs/heads/kit/sib',
                                   'git push -f ~/../repo HEAD:refs/heads/kit/sib'])

    def test_a_branch_tag_or_checkout_that_moves_a_shared_ref(self):
        self.each_unclean_refused(['git branch -f kit/sib HEAD', 'git branch --force kit/sib',
                                   'git branch -m kit/wt kit/x', 'git branch -M x main', 'git branch -C x kit/sib',
                                   'git branch --move kit/sib x',
                                   'git tag -f v1', 'git tag --force v1', 'git tag -d v1', 'git tag --delete v1',
                                   'git checkout -B kit/sib', 'git checkout --ignore-other-worktrees kit/sib',
                                   'git switch -C kit/sib', 'git switch --force-create kit/sib',
                                   'git switch --ignore-other-worktrees main',
                                   'git worktree add -B kit/sib .claude/worktrees/y',
                                   'git worktree add -f .claude/worktrees/y kit/sib',
                                   'git worktree add --force .claude/worktrees/y main'])

    def test_a_rebase_remote_or_history_tool_that_moves_shared_refs(self):
        self.each_unclean_refused(['git rebase --update-refs main', 'git rebase --upd main',
                                   'git -c rebase.updateRefs=true rebase main',
                                   'git -c rebase.updateRefs=true pull --rebase origin main',
                                   'git remote remove origin', 'git remote rm origin', 'git remote rename origin x',
                                   'git remote set-head origin -a', 'git remote -v remove origin',
                                   'git remote add --mirror=fetch backup https://example.invalid/x.git',
                                   'git fast-import', 'git filter-branch -- --all'])

    def test_reads_and_allowed_writes_meet_no_shared_ref_refusal(self):
        for text in ('git fetch origin', 'git fetch https://example.invalid/x.git', 'git fetch origin main',
                     'git push origin HEAD:refs/heads/kit/x', 'git symbolic-ref HEAD', 'git symbolic-ref --short HEAD',
                     'git notes show', 'git replace -l', 'git push . HEAD:refs/tmp/x',
                     'git fetch origin main:refs/remotes/origin/main', 'git symbolic-ref HEAD refs/heads/kit/wt',
                     'git fetch --tags origin', 'git fetch --prune origin', 'git fetch origin tag v1',
                     'git branch kit/new', 'git branch -c kit/wt kit/copy', 'git branch -d kit/x', 'git tag v9',
                     'git checkout -b kit/new', 'git switch -c kit/new', 'git checkout -B kit/wt',
                     'git worktree add .claude/worktrees/y -b kit/new', 'git rebase main',
                     'git rebase --no-update-refs main', 'git remote -v', 'git remote prune origin',
                     'git push origin HEAD:refs/heads/kit/wt'):
            with self.subTest(text):
                d = self.worker(self.UNCLEAN + text)
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)

    def test_a_line_of_options_does_not_hang_the_pattern(self):
        texts = ('git symbolic-ref' + ' -mx' * 24 + ' |',   # 24: about 5 s with the old pattern, doubling per word
                 'git -c ' + 'remote.' * 400 + ' fetch |',
                 'git -c ' + 'remote.x.fetch.' * 4000 + ' fetch |')   # about 1.5 s when each `remote.` starts a scan
        for text in texts:
            for pat in fallback.SHARED_REF_PATTERNS:
                with self.subTest(text[:24], pat=pat):
                    start = time.perf_counter()
                    re.search(pat, text)
                    self.assertLess(time.perf_counter() - start, 0.25, pat)

    def test_a_remote_key_after_c_or_a_quote(self):
        for text in ('git -cremote.origin.fetch=+refs/heads/*:refs/heads/* fetch origin',
                     'git -c "remote.origin.fetch=+refs/heads/*:refs/heads/*" fetch origin',
                     'git --config-env=remote.origin.pruneTags=P fetch origin',
                     'git -c Remote.Origin.PruneTags=true fetch origin'):
            with self.subTest(text):
                self.assertTrue(any('prunetags' in p and re.search(p, text) for p in fallback.SHARED_REF_PATTERNS))

    def test_the_owner_is_not_held(self):
        for text in ('git push . HEAD:refs/heads/kit/sib', 'git fetch origin main:main', 'git notes add -m x'):
            with self.subTest(text):
                d = self.session(self.UNCLEAN + text, {})
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)


class FormsBase(SharedRefBase):
    """Each refusal names what to run instead (rules._judge_refs): `instead` pins the command."""

    def each_refused(self, texts, **kw):
        for text in texts:
            with self.subTest(text):
                d = self.refused(self.worker(text, **kw))
                self.assertIn(' Instead: ', d.reason)

    def instead(self, text, command, **kw):
        with self.subTest(text):
            d = self.refused(self.worker(text, **kw))
            got = d.reason.split(' Instead: ', 1)[1]
            self.assertEqual(got[:got.index(' [kit guard: ')], command, d.reason)

    def at(self, rest):
        return 'git -C "%s" %s' % (self.wt, rest)

    def not_this_rule(self, texts, **kw):
        """Another rule may judge these (the jail, git-plain-commit); git-shared-ref does not."""
        for text in texts:
            with self.subTest(text):
                d = self.worker(text, **kw)
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)


class BranchForce(FormsBase):
    def test_a_forced_branch_moves_a_shared_branch(self):
        self.each_refused(['git branch -f main HEAD', 'git branch -f kit/sib HEAD', 'git branch --force kit/sib',
                           'git branch --forc kit/sib main', 'git branch -fq kit/sib HEAD',
                           'git branch -f kit/x HEAD',                  # any branch but its own is shared
                           'git branch -C x kit/sib', 'git branch -c -f x kit/sib', 'git branch --copy --force x main'])
        self.each_refused(['git branch -f kit/sib HEAD'], cwd=self.w.main)

    def test_a_forced_branch_the_guard_cannot_read(self):
        self.each_refused(['git branch -f "$B" HEAD', 'git branch -C x "$B"', 'git branch -f --bogus kit/x'])

    def test_it_names_a_new_branch_instead(self):
        self.instead('git branch -f kit/sib HEAD', self.at('branch <new-name> HEAD'))
        self.instead('git branch --force kit/sib', self.at('branch <new-name>'))
        self.instead('git branch -C x kit/sib', self.at('branch -c x <new-name>'))

    def test_its_own_branch_and_a_new_one(self):
        self.each_allowed(['git branch kit/new', 'git branch kit/new main', 'git branch -c kit/wt kit/copy',
                           'git branch -f kit/wt HEAD~1', 'git branch -d kit/x',
                           'git branch --list', 'git branch -v', 'git branch -a', 'git branch --contains HEAD',
                           'git branch -u origin/main', 'git branch --show-current'])


class BranchMove(FormsBase):
    """A rename deletes the old name and writes the new one: refused even for the session's own
    branch, which the coordinator lands by its name (docs/guard.md "Rules")."""

    def test_a_move_deletes_a_shared_name(self):
        self.each_refused(['git branch -m main x', 'git branch -M x main', 'git branch -m kit/sib x',
                           'git branch --move kit/sib x', 'git branch --mo kit/sib x',
                           'git branch -m kit/wt kit/renamed', 'git branch -M kit/wt kit/renamed',
                           'git branch -m x',                    # renames the branch checked out where git runs
                           'git branch -m "$A" x'])
        self.each_refused(['git branch -m main old'], cwd=self.w.main)

    def test_it_names_a_copy_instead(self):
        self.instead('git branch -m kit/sib x', self.at('branch x kit/sib'))
        self.instead('git branch -m kit/wt kit/renamed', self.at('branch kit/renamed kit/wt'))
        self.instead('git branch -m x', self.at('branch x kit/wt'))

    def test_one_name_renames_the_branch_head_names(self):
        d = self.refused(self.worker('git branch -m x'))
        self.assertTrue(d.reason.startswith('`git branch -m x` renames refs/heads/kit/wt to refs/heads/x, deleting the '
                                            'old name: refs/heads/kit/wt is not one it may delete; '), d.reason)
        d = self.refused(self.worker('git branch -M kit/sib x'))
        self.assertTrue(d.reason.startswith('`git branch -M kit/sib x` renames refs/heads/kit/sib to refs/heads/x, '
                                            'deleting the old name: refs/heads/kit/sib is not one it may delete; '),
                        d.reason)

    def test_one_name_with_head_detached_or_unread(self):
        put(os.path.join(self.wt_git, 'HEAD'), '0123abc4567def0123abc4567def0123abc4567\n')
        self.not_this_rule(['git branch -m x'])          # git renames nothing: no branch is checked out
        os.remove(os.path.join(self.wt_git, 'HEAD'))
        d = self.refused(self.worker('git branch -m x'))
        self.assertIn('the guard cannot tell which refs it writes', d.reason)


class TagForms(FormsBase):
    def test_a_forced_or_deleted_tag_is_shared(self):
        self.each_refused(['git tag -f v1 HEAD', 'git tag --force v1', 'git tag -fa v1 -m x', 'git tag -af v1 -m x',
                           'git tag -d v1', 'git tag -d v1 v2', 'git tag --delete v1', 'git tag --d v1',
                           'git tag -d "$T"', 'git tag -f "$T"'])

    def test_it_names_a_new_tag_or_a_read_instead(self):
        self.instead('git tag -f v1 HEAD', self.at('tag <new-name> HEAD'))
        self.instead('git tag -d v1', self.at('show-ref refs/tags/v1'))

    def test_a_new_tag_and_a_listing(self):
        self.each_allowed(['git tag v9', 'git tag -a v9 -m x', 'git tag', 'git tag -l', 'git tag --list "v*"',
                           'git tag -n', 'git tag --contains HEAD', 'git tag -v v1'])


class CheckoutReset(FormsBase):
    def test_a_reset_branch_is_shared(self):
        self.each_refused(['git checkout -B main', 'git checkout -B kit/sib HEAD', 'git checkout -qB kit/sib',
                           'git switch -C kit/sib', 'git switch --force-create kit/sib', 'git switch --force-c kit/sib',
                           'git checkout -B "$B"', 'git switch -C "$B"'])

    def test_it_names_a_new_branch_instead(self):
        self.instead('git checkout -B kit/sib HEAD', self.at('switch -c <new-name> HEAD'))
        self.instead('git switch -C kit/sib', self.at('switch -c <new-name>'))

    def test_its_own_branch_and_a_new_one(self):
        self.each_allowed(['git checkout -b kit/new', 'git switch -c kit/new', 'git checkout -B kit/wt',
                           'git switch -C kit/wt HEAD~1'])


class IgnoreOtherWorktrees(FormsBase):
    """--ignore-other-worktrees checks out a branch another worktree holds: a commit there then
    moves it under that worktree."""

    def test_another_worktrees_branch(self):
        self.each_refused(['git switch --ignore-other-worktrees main', 'git checkout --ignore-other-worktrees main',
                           'git checkout --ignore-other-worktrees kit/sib', 'git switch --ignore-o kit/sib',
                           'git switch --ignore-other-worktrees -', 'git switch --ignore-other-worktrees @{-1}',
                           'git switch --ignore-other-worktrees "$B"'])

    def test_it_names_a_detached_checkout_instead(self):
        self.instead('git switch --ignore-other-worktrees main', self.at('switch --detach main'))

    def test_a_new_branch_a_detached_head_or_its_own(self):
        self.each_allowed(['git checkout --ignore-other-worktrees -b kit/new',
                           'git switch --ignore-other-worktrees -c kit/new',
                           'git switch --ignore-other-worktrees --detach main',
                           'git checkout --ignore-other-worktrees HEAD',
                           'git switch --ignore-other-worktrees kit/wt',
                           'git switch --ignore-other-worktrees --no-ignore-other-worktrees kit/sib'])

    def test_a_branch_checked_out_nowhere_or_packed(self):
        put(os.path.join(self.common, 'refs', 'heads', 'kit', 'loose'), '0123abc4567def0123abc4567def0123abc4567\n')
        put(os.path.join(self.common, 'packed-refs'),
            '# pack-refs with: peeled fully-peeled sorted\n'
            '0123abc4567def0123abc4567def0123abc4567 refs/heads/kit/packed\n')
        self.each_refused(['git switch --ignore-other-worktrees kit/loose',
                           'git checkout --ignore-other-worktrees kit/packed',
                           'git worktree add -f .claude/worktrees/y kit/packed'])

    def test_a_tag_or_a_commit_checks_out_detached(self):
        # git checks out a name that is no branch detached (or makes a new branch from a remote's):
        # no branch another worktree has moves
        put(os.path.join(self.common, 'refs', 'tags', 'v1'), '0123abc4567def0123abc4567def0123abc4567\n')
        self.each_allowed(['git checkout --ignore-other-worktrees v1',
                           'git switch --ignore-other-worktrees 0123abc4567def',
                           'git checkout --ignore-other-worktrees 0123abc4567def0123abc4567def0123abc4567',
                           'git checkout --ignore-other-worktrees kit/new-from-origin',
                           'git worktree add -f .claude/worktrees/y v1',
                           'git worktree add -f .claude/worktrees/v1'])


class WorktreeAdd(FormsBase):
    def test_a_reset_or_forced_branch(self):
        self.each_refused(['git worktree add -B main .claude/worktrees/y',
                           'git worktree add -B kit/sib .claude/worktrees/y HEAD',
                           'git worktree add -f .claude/worktrees/y main',
                           'git worktree add -f .claude/worktrees/y kit/sib',
                           'git worktree add --force .claude/worktrees/y kit/sib',
                           'git worktree add -f .claude/worktrees/main',        # git takes the folder's name
                           'git worktree add -f .claude/worktrees/y "$B"'])

    def test_it_names_a_new_branch_or_a_detached_one_instead(self):
        self.instead('git worktree add -B kit/sib .claude/worktrees/y HEAD',
                     self.at('worktree add -b <new-name> .claude/worktrees/y HEAD'))
        self.instead('git worktree add -f .claude/worktrees/y kit/sib',
                     self.at('worktree add --detach .claude/worktrees/y kit/sib'))
        self.instead('git worktree add -f .claude/worktrees/main',
                     self.at('worktree add --detach .claude/worktrees/main <commit-ish>'))

    def test_a_new_branch_stays_open(self):
        self.each_allowed(['git worktree add .claude/worktrees/y -b kit/new',
                           'git worktree add .claude/worktrees/y kit/new-branch',
                           'git worktree add -b kit/new .claude/worktrees/y main',
                           'git worktree add --detach .claude/worktrees/y HEAD',
                           'git worktree add -f --detach .claude/worktrees/y main',
                           'git worktree add -f -b kit/new .claude/worktrees/y'])
        self.not_this_rule(['git worktree add ../y -b kit/new', 'git worktree list'])


class FetchTag(FormsBase):
    def test_a_forced_tag_fetch_moves_the_tag(self):
        self.each_refused(['git fetch -f origin tag v1', 'git fetch --force origin tag v1 tag v2',
                           'git fetch --forc origin tag v1', 'git fetch -qf origin tag v1',
                           'git pull -f origin tag v1', 'git fetch -f origin tag "$T"'])

    def test_it_names_a_scratch_ref_instead(self):
        self.instead('git fetch -f origin tag v1', self.at('fetch origin refs/tags/v1:refs/tmp/v1'))

    def test_an_unforced_tag_fetch_adds_only_a_new_tag(self):
        self.each_allowed(['git fetch origin tag v1', 'git fetch origin tag v1 tag v2', 'git fetch -f origin main'])


class RebaseUpdateRefs(FormsBase):
    """--update-refs moves every branch whose tip the rebased commits hold, wherever it is checked out."""

    def test_update_refs_moves_other_branches(self):
        self.each_refused(['git rebase --update-refs main', 'git rebase --upd main', 'git rebase -i --update-refs main',
                           'git -c rebase.updateRefs=true rebase main', 'git -c rebase.updateRefs rebase main',
                           'git -c rebase.updateRefs=yes rebase -i main',
                           'git rebase --no-update-refs --update-refs main',          # the last one counts
                           'git -c rebase.updateRefs=false rebase --update-refs main',
                           'git -c "rebase.updateRefs=$X" rebase main'])

    def test_it_names_no_update_refs_instead(self):
        self.instead('git rebase --update-refs main', self.at('rebase --no-update-refs main'))
        self.instead('git -c rebase.updateRefs=true rebase -i main', self.at('rebase --no-update-refs -i main'))

    def test_a_plain_rebase(self):
        self.each_allowed(['git rebase main', 'git rebase -i main', 'git rebase --no-update-refs main',
                           'git rebase --update-refs --no-update-refs main',
                           'git -c rebase.updateRefs=false rebase main',
                           'git -c rebase.updateRefs=true rebase --no-update-refs main'])


    def test_a_rebasing_pull_with_update_refs_on(self):
        # git pull takes no --update-refs, but the rebase it runs reads rebase.updateRefs
        self.each_refused(['git -c rebase.updateRefs=true pull --rebase origin main',
                           'git -c rebase.updateRefs pull -r origin main',
                           'git -c rebase.updateRefs=true pull -qr origin main',
                           'git -c rebase.updateRefs=true pull --reb=merges origin main',
                           'git -c rebase.updateRefs=true -c pull.rebase=true pull origin main',
                           'git -c pull.rebase -c rebase.updateRefs=yes pull origin main',
                           'git -c rebase.updateRefs=true pull origin main',     # the repository's pull.rebase may rebase
                           'git -c rebase.updateRefs=true pull --no-rebase --rebase origin main',
                           'git -c rebase.updateRefs=true -c pull.rebase=false pull -r origin main',
                           'git -c rebase.updateRefs=true pull "--rebase=$R" origin main',
                           'git -c "rebase.updateRefs=$X" pull --rebase origin main',
                           'git -c rebase.updateRefs=true -c "pull.rebase=$P" pull origin main'])

    def test_it_names_a_pull_with_update_refs_off_instead(self):
        self.instead('git -c rebase.updateRefs=true pull --rebase origin main',
                     self.at('-c rebase.updateRefs=false pull --rebase origin main'))

    def test_a_pull_that_merges_or_leaves_update_refs_off(self):
        self.not_this_rule(['git pull --rebase origin main', 'git pull -r origin main', 'git pull origin main',
                           'git -c rebase.updateRefs=false pull --rebase origin main',
                           'git -c rebase.updateRefs=true -c rebase.updateRefs=false pull -r origin main',
                           'git -c pull.rebase=true pull origin main',
                           'git -c rebase.updateRefs=true pull --no-rebase origin main',
                           'git -c rebase.updateRefs=true pull --rebase=false origin main',
                           'git -c rebase.updateRefs=true pull -r --no-rebase origin main',
                           'git -c rebase.updateRefs=true -c pull.rebase=false pull origin main'])


class RemoteRefs(FormsBase):
    def test_a_remote_removal_rename_or_head(self):
        self.each_refused(['git remote remove origin', 'git remote rm origin', 'git remote rename origin x',
                           'git remote -v rename origin x', 'git remote set-head origin -a',
                           'git remote set-head origin main', 'git remote set-head origin -d',
                           'git remote add --mirror=fetch backup https://example.invalid/x.git',
                           'git remote add --mirror backup https://example.invalid/x.git',
                           'git remote "$V" origin', 'git remote --bogus remove origin'])

    def test_it_names_a_read_or_a_new_remote_instead(self):
        self.instead('git remote remove origin', self.at('remote -v'))
        self.instead('git remote rename origin x', self.at('remote add x <url>'))
        self.instead('git remote set-head origin -a', self.at('remote show origin'))

    def test_reads_prune_and_a_new_remote(self):
        self.each_allowed(['git remote', 'git remote -v', 'git remote show origin', 'git remote get-url origin',
                           'git remote prune origin', 'git remote update',
                           'git remote add backup https://example.invalid/x.git',
                           'git remote add --mirror=push backup https://example.invalid/x.git',
                           'git remote set-url origin https://example.invalid/y.git'])


class HistoryRewrite(FormsBase):
    def test_fast_import_and_filter_branch_write_any_ref(self):
        self.each_refused(['git fast-import', 'git fast-import --force', 'git filter-branch -- --all',
                           'git filter-branch --msg-filter cat HEAD', 'git filter-branch -f --index-filter x HEAD'])

    def test_it_names_a_clone_under_the_temp_folder_instead(self):
        self.instead('git fast-import', 'git clone "%s" "%s/<name>"' % (self.wt, self.w.temp.replace(os.sep, '/')))

    def test_help(self):
        self.each_allowed(['git fast-import -h', 'git filter-branch --help'])


class PushHome(FormsBase):
    """~ in a push's repository is the home folder, as the shell expands it."""

    def test_a_push_through_home_into_this_repository(self):
        self.each_refused(['git push ~/../repo HEAD:refs/heads/kit/sib',
                           'git push ~user/x HEAD:refs/heads/kit/sib'])     # another user's home: not read

    def test_a_push_to_another_repository_under_home(self):
        self.not_this_rule(['git push ~/x HEAD:refs/heads/kit/sib', 'git push ~/../repo HEAD:refs/tmp/x'])

if __name__ == '__main__':
    unittest.main()
