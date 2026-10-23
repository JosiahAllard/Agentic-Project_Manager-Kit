"""git-shared-ref beyond update-ref (docs/guard.md "Rules"): a push into the repository itself,
a fetch or pull refspec's destination, symbolic-ref, replace and a notes write move refs every
worktree shares as update-ref does, so each is judged by the same allowance: refs/tmp/<name>, the
per-worktree refs, and in a linked worktree its HEAD and the branch checked out there."""
import os
import unittest

from kitlib.guard import rules

from . import test_jail as tj
from .test_shared_ref import RULE, SharedRefBase


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
              'git replace 0123abc 4567def', 'git notes add -m x')

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
                                   'git symbolic-ref -d HEAD', 'git symbolic-ref --delete refs/heads/kit/wt'])

    def test_a_replace(self):
        self.each_unclean_refused(['git replace 0123abc 4567def', 'git replace -d 0123abc'])

    def test_a_notes_write(self):
        self.each_unclean_refused(['git notes add -m x', 'git notes --ref=x remove HEAD', 'git notes prune'])

    def test_reads_and_allowed_writes_meet_no_shared_ref_refusal(self):
        for text in ('git fetch origin', 'git fetch https://example.invalid/x.git', 'git fetch origin main',
                     'git push origin HEAD:refs/heads/kit/x', 'git symbolic-ref HEAD', 'git symbolic-ref --short HEAD',
                     'git notes show', 'git replace -l', 'git push . HEAD:refs/tmp/x',
                     'git fetch origin main:refs/remotes/origin/main', 'git symbolic-ref HEAD refs/heads/kit/wt'):
            with self.subTest(text):
                d = self.worker(self.UNCLEAN + text)
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)

    def test_the_owner_is_not_held(self):
        for text in ('git push . HEAD:refs/heads/kit/sib', 'git fetch origin main:main', 'git notes add -m x'):
            with self.subTest(text):
                d = self.session(self.UNCLEAN + text, {})
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)


if __name__ == '__main__':
    unittest.main()
