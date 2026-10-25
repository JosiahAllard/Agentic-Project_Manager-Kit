"""kitlib.hubsite.codestale: the one text that tells the owner to restart a stale hub."""
import unittest

from kitlib.hubsite import codestale


class NoticeTest(unittest.TestCase):
    def test_a_stale_code_object_gives_the_text_with_the_exact_step(self):
        got = codestale.notice({'stale': True})
        self.assertIn('Owner step: restart the Action Hub: python "%s" restart' % codestale.HUB_TOOL, got)
        self.assertIn('python "%s" stop, then python "%s" serve' % (codestale.HUB_TOOL, codestale.HUB_TOOL), got)
        self.assertEqual(codestale.notice({'stale': True}, 'X/hub.py').count('X/hub.py'), 3)

    def test_anything_else_gives_none_and_never_raises(self):
        for code in (None, {}, {'stale': False}, {'stale': 'yes'}, {'stale': 1}, 'stale', [], 3):
            self.assertIsNone(codestale.notice(code), code)

    def test_state_is_the_object_a_server_gives(self):
        self.assertEqual(codestale.state(False), {'stale': False, 'restart': None})
        self.assertEqual(codestale.state(True), {'stale': True, 'restart': codestale.text()})
        self.assertEqual(codestale.state(None)['stale'], False)

    def test_the_console_line_is_the_same_text(self):
        from kitlib.hubsite import serve
        self.assertEqual(serve.STALE_CODE, codestale.text())
        self.assertIn('restart', serve.STALE_CODE)


if __name__ == '__main__':
    unittest.main()
