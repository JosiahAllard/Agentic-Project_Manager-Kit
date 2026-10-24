"""The ledger rule over the rulebooks and the standing digest (docs/guard.md "Rules": `ledger`):
the `rules` folder and the `.md` files directly in it, and STANDING.md, in `ledger_dir` or beside
`ledger_path` (kitlib.ledger.files.of_config), are written by ledger.py alone, by any role; other
files in those folders stay open, and a broken machine config still refuses every subagent write.

A subagent's write here runs in the ledger's folder, its own root outside git, so no other rule
(write-jail, main-checkout) would refuse it: the refusal is the ledger rule's."""
import os
import unittest

from kitlib.guard import context

from . import world as W
from .claudehost import decide
from .world import bash, event, ps

BOOK = 'writes a rulebook, which only ledger.py writes'
DIGEST = 'writes the standing digest, which the rulebooks replace'


class Rulebooks(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.folder = os.path.dirname(self.w.ledger)
        self.rules = os.path.join(self.folder, 'rules')
        self.digest = os.path.join(self.folder, 'STANDING.md')
        os.makedirs(self.rules)
        self.alt = ('Instead: python "%s/tools/ledger.py" conflicts --repo <repo> --topic "<topic>" '
                    '--key <key> --text "<rule>"' % W.fwd(self.w.kit_dir))

    def refused(self, ev, why=BOOK):
        d = decide(ev, self.w.ctx())
        self.assertIsNotNone(d, ev['tool_input'])
        self.assertEqual(d.rule, 'ledger', d.reason)
        self.assertIn(why, d.reason)
        self.assertIn(self.alt, d.reason)

    def allowed(self, ev):
        d = decide(ev, self.w.ctx())
        self.assertIsNone(d, d and d.reason)

    def book(self, name='alpha.md'):
        return os.path.join(self.rules, name)

    def test_a_subagent_edit_of_a_rulebook_is_refused(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        self.refused(event('Write', {'file_path': self.book(), 'content': '# Alpha rules\n'}, f, sub=True))
        self.refused(event('Edit', {'file_path': self.book(), 'old_string': 'a', 'new_string': 'b'}, f, sub=True))
        self.refused(event('Write', {'file_path': self.book('Beta.MD'), 'content': ''}, f, sub=True))
        self.refused(event('Write', {'file_path': self.book('alpha.md.'), 'content': ''}, f, sub=True))
        self.refused(event('Write', {'file_path': self.book('alpha.md '), 'content': ''}, f, sub=True))
        self.refused(bash('echo "- a rule" >> rules/alpha.md', f, sub=True))
        self.refused(bash('cp draft.md rules/alpha.md', f, sub=True))
        self.refused(ps('Set-Content -Path %s -Value x' % self.book(), f, sub=True))

    def test_the_rulebooks_folder_itself_is_refused(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        self.refused(bash('rm -r rules', self.folder, sub=True))
        self.refused(ps('Remove-Item -Recurse -Force %s' % self.rules, self.folder, sub=True))

    def test_the_standing_digest_is_refused(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        self.refused(event('Write', {'file_path': self.digest, 'content': ''}, self.folder, sub=True), DIGEST)
        self.refused(bash('echo x >> STANDING.md', self.folder, sub=True), DIGEST)

    def test_every_role_is_refused_a_rulebook(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        self.refused(event('Write', {'file_path': self.book(), 'content': ''}, self.w.plain))
        self.w.environ = {context.ROLE_ENV: 'pm'}
        self.refused(event('Edit', {'file_path': self.book(), 'old_string': 'a', 'new_string': 'b'}, self.w.plain))
        self.refused(event('Write', {'file_path': self.digest, 'content': ''}, self.w.plain), DIGEST)

    def test_the_rules_folder_of_a_split_ledger_is_in_ledger_dir(self):
        split = os.path.join(self.w.base, 'pm', 'split')
        os.makedirs(os.path.join(split, 'rules'))
        self.w.user_config({'ledger_dir': split, 'ledger_repos': ['kit']})
        self.refused(event('Write', {'file_path': os.path.join(split, 'rules', 'kit.md'), 'content': ''},
                           split, sub=True))
        self.refused(event('Write', {'file_path': os.path.join(split, 'STANDING.md'), 'content': ''},
                           split, sub=True), DIGEST)
        # with ledger_dir set, the folder beside a ledger_path that is not set holds nothing of it
        self.allowed(event('Write', {'file_path': self.book(), 'content': ''}, self.folder, sub=True))

    def test_the_pm_tool_write_passes(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        self.w.environ = {context.ROLE_ENV: 'pm'}
        tool = W.fwd(os.path.join(self.w.kit_dir, 'tools', 'ledger.py'))
        self.allowed(bash('python "%s" conflicts --repo kit --topic "alpha" --key alpha-one --text "a rule"'
                          % tool, self.w.plain))
        self.allowed(bash('python "%s" add --repo kit --prov typed --q "q" --a "a" --decides "d" --source "s" '
                          '--rule alpha-one --topic alpha --checked 0123abcd' % tool, self.w.plain))
        self.allowed(bash('python "%s" rule-drop alpha-one --repo kit --prov typed --why "w" --source "s"'
                          % tool, self.w.plain))

    def test_an_unrelated_file_in_the_same_folder_passes(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        f = self.folder
        for path in (os.path.join(f, 'NOTES.md'), os.path.join(f, 'STANDING.md.bak'),
                     self.book('alpha.txt'), os.path.join(self.rules, 'drafts', 'alpha.md')):
            with self.subTest(path):
                self.allowed(event('Write', {'file_path': path, 'content': ''}, f, sub=True))
        self.allowed(bash('echo x >> NOTES.md', f, sub=True))
        self.allowed(bash('cat rules/alpha.md', f, sub=True))
        self.allowed(event('Read', {'file_path': self.book()}, f, sub=True))

    def test_no_ledger_config_no_rulebook_rule(self):
        self.allowed(event('Write', {'file_path': self.book(), 'content': ''}, self.folder, sub=True))
        self.allowed(event('Write', {'file_path': self.digest, 'content': ''}, self.w.plain))

    def write_config(self, raw):
        self.w.user_config({})
        with open(self.w.config_file, 'wb') as f:
            f.write(raw)

    def test_a_broken_ledger_dir_refuses_every_subagent_write(self):
        target = os.path.join(self.w.wt, 'a.py')
        for raw in (b'{"ledger_dir": 5}', b'{"ledger_dir": ["Z:/pm/split"], "ledger_repos": ["kit"]}'):
            with self.subTest(raw):
                self.write_config(raw)
                self.assertIn('ledger_dir must be a string', self.w.ctx().user().get('broken') or '')
                for ev in (event('Write', {'file_path': target, 'content': ''}, self.w.wt, sub=True),
                           bash('echo x > notes.txt', self.w.wt, sub=True)):
                    d = decide(ev, self.w.ctx())
                    self.assertIsNotNone(d, ev['tool_name'])
                    self.assertEqual(d.rule, 'ledger', d.reason)
                    self.assertIn('The owner or the PM session fixes it. Instead: python -m json.tool "%s"'
                                  % W.fwd(self.w.config_file), d.reason)
                self.assertIsNone(decide(event('Write', {'file_path': target, 'content': ''}, self.w.wt),
                                         self.w.ctx()))

    def test_a_strict_reader_refusal_still_yields_the_rulebooks(self):
        # a key this kit's schema does not know: the tolerant read, with ledger_dir relative to the
        # PM repository
        self.write_config(b'{"ledger_dir": "split", "ledger_repos": ["kit"], "a_key_this_kit_does_not_know": {}}')
        split = os.path.join(self.w.pm_repo, 'split')
        os.makedirs(os.path.join(split, 'rules'))
        user = self.w.ctx().user()
        self.assertIn('error', user)
        self.assertNotIn('broken', user)
        self.assertEqual(user['ledger_dir'], split)
        self.refused(event('Write', {'file_path': os.path.join(split, 'rules', 'kit.md'), 'content': ''},
                           split, sub=True))
        self.refused(event('Write', {'file_path': os.path.join(split, 'STANDING.md'), 'content': ''},
                           split, sub=True), DIGEST)


if __name__ == '__main__':
    unittest.main()
