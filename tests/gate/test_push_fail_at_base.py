"""kitlib.gate.land Lander.push: a push git refused (exit 1) while <remote>/<main> is still the
base published nothing, so the landing is NOT LANDED (FAILED) and the lock is released, as on a
red merged gate. Every push whose outcome git cannot settle stays STUCK: exit 128 (git died, so a
dropped connection after the upload may still be applied), no exit (timed out or not started),
exit 0 that the remote does not show, an unreadable or absent remote main.

The push is replaced by a fake git result; the ls-remote that follows reads the scratch remote.
Scratch repositories with a bare remote in temp and the scratch lock store only."""
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land
from kitlib.git import cmd, refs
from tests.gate.test_land import BRANCH, PushCase, main_only


class PushFailsAtBase(PushCase):
    def land_with_push(self, tip, result, ls=None):
        """Land `tip` with `git push` answering `result` without pushing; `ls`, when given, is
        what ls-remote answers for refs/heads/main."""
        real = land.Lander.git
        pushes = []

        def git(lander, *args, **kw):
            if args[:1] == ('push',):
                pushes.append(args)
                return result
            return real(lander, *args, **kw)
        with mock.patch.object(land.Lander, 'git', git):
            if ls is None:
                out = self.lander(BRANCH, self.main, tip).run()
            else:
                with mock.patch.object(refs, 'ls_remote', side_effect=main_only(ls)):
                    out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(len(pushes), 1, pushes)        # the push step really ran
        return out

    def test_a_refused_push_at_the_base_is_not_landed_and_releases_the_lock(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.land_with_push(tip, cmd.Result(
            1, b'', "error: failed to push some refs to 'origin'"))
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.lines[-1].startswith('NOT LANDED: '), out.lines)
        self.assertIn('failed to push some refs', out.lines[-1])
        self.assertIn('still the base %s' % self.main[:10], out.lines[-1])
        self.assertFalse([l for l in out.lines if l.startswith(('STUCK', 'GATE OK'))], out.lines)
        self.assertTrue(out.merge)
        self.assertIsNone(self.lock().read())           # released: the next landing may start
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.main_now(), self.main)
        again = self.lander(BRANCH, self.main, tip).run()   # and it lands, with no hand step
        self.assertEqual(again.code, G.OK, again.lines)
        self.assertEqual(self.remote_main(), again.merge)

    def test_a_push_git_died_in_at_the_base_is_stuck(self):
        # exit 128: a connection dropped after the upload may still be applied on the remote
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.land_with_push(tip, cmd.Result(
            128, b'', 'fatal: the remote end hung up unexpectedly'))
        self.assert_stuck(out, tip)
        self.assertIn('failed (fatal: the remote end hung up unexpectedly)', out.lines[0])

    def test_a_push_that_timed_out_at_the_base_is_stuck(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.land_with_push(tip, cmd.Result(
            None, b'', 'git push timed out after 600 seconds'))
        self.assert_stuck(out, tip)
        self.assertIn('timed out', out.lines[0])

    def test_a_refused_push_with_the_remote_unreadable_is_stuck(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.land_with_push(tip, cmd.Result(
            1, b'', "error: failed to push some refs to 'origin'"), ls=(None, 'network down'))
        self.assert_stuck(out, tip)
        self.assertIn('unreadable (network down)', out.lines[0])

    def test_a_refused_push_with_the_remote_main_absent_is_stuck(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.land_with_push(tip, cmd.Result(
            1, b'', "error: failed to push some refs to 'origin'"), ls=('', None))
        self.assert_stuck(out, tip)
        self.assertIn('is absent', out.lines[0])


if __name__ == '__main__':
    unittest.main()
