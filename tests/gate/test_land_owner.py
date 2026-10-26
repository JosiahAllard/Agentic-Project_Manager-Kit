"""kitlib.gate.land and owner-only paths (docs/gate.md "Landing", step 4): a branch with a commit
that changes an owner-only path and is not authored by the owner ("owner".email, read from
--expect-main's config) is REFUSED before any merge, whatever its trailers say; the owner's own
commit lands, and so does an agent commit with an owner approval the hub verifies for it."""
import json
from unittest import mock

from kitlib import gate as G
from kitlib.guard import ownercommits
from kitlib.hubsite import approvals as ap
from tests.gate import test_land
from tests.guard import test_ownercommits as toc
from tests.home import no_machine_owner

BRANCH = test_land.BRANCH
OWNER = 'owner@example.invalid'
AGENT = 'agent@example.invalid'


class OwnerLandCase(test_land.LandCase):
    KIT = dict(test_land.LandCase.KIT, owner={'email': OWNER})

    def branch_as(self, email, files, message='work', name='feature', trailer=None):
        wt = self.repo.worktree(name)
        for rel, text in files.items():
            self.repo.write(rel, text, root=wt)
        self.repo.git('add', '--', *files, cwd=wt)
        args = ['-c', 'user.email=%s' % email, 'commit', '-q', '-m', message]
        if trailer:
            args += ['-m', trailer]
        self.repo.git(*args, cwd=wt)
        return wt, self.repo.git('rev-parse', 'HEAD', cwd=wt)


class OwnerPaths(OwnerLandCase):
    def test_an_agent_commit_to_an_owner_only_path_is_refused_before_any_merge(self):
        _, tip = self.branch_as(AGENT, {'CLAUDE.md': 'agent\n', 'a.py': 'a\n'},
                                trailer='Owner-Approved: CLAUDE.md')
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('nothing was merged', out.lines[0])
        self.assertIn('%s by %s changes CLAUDE.md' % (tip[:10], AGENT), out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_the_owners_commit_lands(self):
        _, tip = self.branch_as(OWNER, {'CLAUDE.md': 'owner\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)

    def test_an_agent_branch_without_owner_only_paths_lands(self):
        _, tip = self.branch_as(AGENT, {'a.py': 'a\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)

    def test_the_branch_cannot_name_its_own_author_the_owner(self):
        kit = json.loads(self.repo.git('show', '%s:.kit.json' % self.main))
        kit['owner'] = {'email': AGENT, 'paths': []}
        wt = self.repo.worktree('feature')
        self.repo.write('.kit.json', json.dumps(kit), root=wt)
        self.repo.write('CLAUDE.md', 'agent\n', root=wt)
        self.repo.git('add', '.kit.json', 'CLAUDE.md', cwd=wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m', 'take over', cwd=wt)
        tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('changes .kit.json, CLAUDE.md', out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_an_evil_merge_on_the_branch_is_refused(self):
        wt, _ = self.branch_as(AGENT, {'a.py': 'a\n'})
        self.repo.write('CLAUDE.md', 'owner\n')
        self.repo.git('add', 'CLAUDE.md')
        self.repo.git('-c', 'user.email=%s' % OWNER, 'commit', '-q', '-m', 'owner edit')
        self.main = self.repo.git('rev-parse', 'HEAD')
        self.repo.git('push', '-q', 'origin', 'main')
        self.repo.git('-c', 'user.email=%s' % AGENT, 'merge', '-q', '--no-ff', '--no-commit', 'main',
                      cwd=wt)
        self.repo.write('CLAUDE.md', 'agent, inside the merge\n', root=wt)
        self.repo.git('add', 'CLAUDE.md', cwd=wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '--no-edit', cwd=wt)
        tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('%s by %s changes CLAUDE.md' % (tip[:10], AGENT), out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_a_clean_merge_of_main_into_the_branch_lands(self):
        wt, _ = self.branch_as(AGENT, {'a.py': 'a\n'})
        self.repo.write('CLAUDE.md', 'owner\n')
        self.repo.git('add', 'CLAUDE.md')
        self.repo.git('-c', 'user.email=%s' % OWNER, 'commit', '-q', '-m', 'owner edit')
        self.main = self.repo.git('rev-parse', 'HEAD')
        self.repo.git('push', '-q', 'origin', 'main')
        self.repo.git('-c', 'user.email=%s' % AGENT, 'merge', '-q', '--no-ff', '--no-edit', 'main',
                      cwd=wt)
        tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)

    def test_an_agent_change_then_revert_of_an_owner_only_path_lands(self):
        wt, edit = self.branch_as(AGENT, {'CLAUDE.md': 'agent\n', 'a.py': 'a\n'})
        self.repo.git('-c', 'user.email=%s' % AGENT, 'revert', '--no-edit', edit, cwd=wt)
        self.repo.write('a.py', 'a\n', root=wt)
        self.repo.git('add', 'a.py', cwd=wt)
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m', 'keep a.py', cwd=wt)
        tip = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)


class OwnerApproval(OwnerLandCase):
    """An agent commit carrying `Owner-Approval: hub:A-<n>` lands when the hub verifies that
    approval for exactly that commit (kitlib.guard.ownercommits, through its approvals seam: the
    stand-in hub of tests/guard/test_ownercommits.py), and is refused like any agent commit when
    it does not."""

    def setUp(self):
        super().setUp()
        self.hub = toc.StandInHub()
        patcher = mock.patch.object(ownercommits, 'default_approvals', lambda: self.hub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def approve_and_commit(self, content, consume=True, moved=None):
        """`moved` ({path: text}): main moves on by an owner commit of those files after the
        approval is signed and before the branch is made (move_main)."""
        base = self.repo.git('rev-parse', '--verify', '-q', '%s:CLAUDE.md' % self.main,
                             check=False) or None
        doc = ap.new_doc(toc.REPO, 'owner-only edit', self.main, [
            {'path': 'CLAUDE.md', 'base_blob': base, 'content': 'approved\n'}], toc.NOW)
        doc.update(ap.decide_fields(toc.KEY, 'A-001', doc, ap.APPROVE, toc.NOW))
        self.hub.docs['A-001'] = doc
        if moved:
            self.move_main(moved)
        _, tip = self.branch_as(AGENT, {'CLAUDE.md': content}, message='owner-approved edit',
                                trailer='Owner-Approval: hub:A-001')
        if consume:
            doc.update(ap.consume_fields(toc.KEY, 'A-001', doc, tip, toc.NOW))
        return tip

    def test_an_approved_agent_commit_lands(self):
        tip = self.approve_and_commit('approved\n')
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual([a[0] for a in self.hub.asked], ['A-001'])

    def test_a_forged_approval_is_refused_before_any_merge(self):
        tip = self.approve_and_commit('not what the owner approved\n')
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('%s by %s changes CLAUDE.md (owner approval hub:A-001 refused: %s)'
                      % (tip[:10], AGENT, ap.WHY_FILES), out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_an_approval_never_consumed_is_refused(self):
        tip = self.approve_and_commit('approved\n', consume=False)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('owner approval hub:A-001 refused: %s' % ap.WHY_COMMIT, out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def hub_seam(self):
        """The hub's own seam (HubApprovals) on the stand-in verify route, in place of setUp's."""
        hub = ownercommits.HubApprovals(client=toc.VerifyRoute(self.hub), repo=toc.REPO)
        patcher = mock.patch.object(ownercommits, 'default_approvals', lambda: hub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def move_main(self, files):
        """An owner commit of `files` on main, pushed: the new main."""
        for rel, text in files.items():
            self.repo.write(rel, text)
        self.repo.git('add', '--', *files)
        self.repo.git('-c', 'user.email=%s' % OWNER, 'commit', '-q', '-m', 'main moves on')
        self.main = self.repo.git('rev-parse', 'HEAD')
        self.repo.git('push', '-q', 'origin', 'main')
        return self.main

    def test_an_approved_commit_on_a_main_moved_by_other_paths_lands(self):
        approved_on = self.main
        self.hub_seam()
        tip = self.approve_and_commit('approved\n', moved={'a.py': 'a\n'})
        self.assertNotEqual(self.main, approved_on)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.hub.asked[-1][1], approved_on)

    def test_an_approved_commit_on_a_main_where_the_owner_changed_its_path_is_refused(self):
        approved_on = self.main
        self.hub_seam()
        tip = self.approve_and_commit('approved\n', moved={'CLAUDE.md': 'owner\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('%s by %s changes CLAUDE.md (owner approval hub:A-001 refused: %s: CLAUDE.md'
                      ' at %s is not as the approved base %s has it)'
                      % (tip[:10], AGENT, ap.WHY_HEAD, self.main[:12], approved_on[:12]),
                      out.lines[0])
        self.assert_untouched(out, G.REFUSED)


class NoOwnerEmail(test_land.LandCase):
    """The base names no owner, and neither does the machine config (no_machine_owner: the lander
    runs in this process and would otherwise read the real home's, which the home fence refuses):
    no commit is provable as the owner's, so every owner-only change is refused (fail closed)."""

    def setUp(self):
        super().setUp()
        no_machine_owner(self)

    def test_any_owner_only_change_is_refused(self):
        wt = self.repo.worktree('feature')
        self.repo.write('CLAUDE.md', 'x\n', root=wt)
        tip = self.repo.commit('edit', 'CLAUDE.md', cwd=wt)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('identity is not configured', out.lines[0])
        self.assert_untouched(out, G.REFUSED)


if __name__ == '__main__':
    import unittest
    unittest.main()
