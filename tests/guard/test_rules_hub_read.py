"""hub-secrets for the read tools (docs/guard.md "Hub secrets"): Read and Grep of the hub's token
folder are refused in every session but the owner's own, and so is a Grep from a parent of it whose
file filter can reach it; the hub store stays readable. Glob is not hooked (it reads no token's text): a names
search of the folder passes."""
import os
import unittest

from kitlib.guard import hub as hubg

from .claudehost import decide
from .test_rules_hub import HELD, Hub
from .world import event

SUB = (('subagent', True, {}),)


class Reads(Hub):
    def read(self, path, cwd=None):
        return lambda sub: event('Read', {'file_path': path}, cwd or self.w.wt, sub)

    def test_the_token_files_and_the_folder(self):
        for name in hubg.SECRET_FILES + ('browser-sessions.json', ''):
            with self.subTest(name=name):
                path = os.path.join(self.secrets, name) if name else self.secrets
                self.denied(self.read(path), 'hub-secrets')
                self.owner_allowed(self.read(path))

    def test_slashes_case_dots_and_a_relative_path(self):
        fwd = self.secrets.replace('\\', '/')
        for path in (fwd + '/owner.token', self.secrets.upper() + '\\OWNER.TOKEN',
                     os.path.join(self.secrets, '..', os.path.basename(self.secrets), 'agent.token')):
            with self.subTest(path=path):
                self.denied(self.read(path), 'hub-secrets', SUB)
        rel = os.path.relpath(os.path.join(self.secrets, 'owner.token'), self.w.home)
        self.denied(self.read(rel, cwd=self.w.home), 'hub-secrets', SUB)

    def test_the_store_and_other_files_stay_readable(self):
        for path in (os.path.join(self.store, 'questions', 'Q-1.json'), os.path.join(self.w.wt, 'a.py'),
                     os.path.join(os.path.dirname(self.secrets), 'config.json'),
                     self.secrets + '.json'):
            with self.subTest(path=path):
                self.allowed(self.read(path))

    def test_the_reason_names_the_rule_and_the_hub_tool(self):
        d = decide(self.read(os.path.join(self.secrets, 'owner.token'))(True), self.ctx({}))
        self.assertIn('[kit guard: rule=hub-secrets role=subagent]', d.reason)
        self.assertIn('hub.py', d.instead)
        self.assertIn('questions', d.instead)


class Searches(Hub):
    def grep(self, path, glob=None, cwd=None):
        ti = {'pattern': 'x'}
        if path is not None:
            ti['path'] = path
        if glob is not None:
            ti['glob'] = glob
        return lambda sub: event('Grep', ti, cwd or self.w.wt, sub)

    def glob(self, pattern, path=None, cwd=None):
        ti = {'pattern': pattern}
        if path is not None:
            ti['path'] = path
        return lambda sub: event('Glob', ti, cwd or self.w.wt, sub)

    def test_a_search_in_the_folder(self):
        for make in (self.grep(self.secrets), self.grep(os.path.join(self.secrets, 'owner.token')),
                     self.grep(None, cwd=self.secrets), self.grep(self.secrets, glob='*.py')):
            self.denied(make, 'hub-secrets')
            self.owner_allowed(make)

    def test_a_contents_search_from_a_parent_with_no_filter(self):
        parent = os.path.dirname(self.secrets)
        for make in (self.grep(self.w.home), self.grep(parent), self.grep(self.w.base),
                     self.grep(None, cwd=self.w.home), self.grep('.', cwd=self.w.home)):
            self.denied(make, 'hub-secrets')
            self.owner_allowed(make)

    def test_a_filter_that_can_take_in_a_secret_file(self):
        os.makedirs(self.secrets)
        with open(os.path.join(self.secrets, 'sessions.json'), 'w', encoding='utf-8') as f:
            f.write('{}')
        for glob in ('*', '*.token', '*.TOKEN', 'owner*', '*.{py,token}', '*.py *.key', '*.py,*.key',
                     '!*.py', '**', '**/*', '*.json', 'hub', '*/hub/*', '**/owner.token',
                     os.path.basename(os.path.dirname(self.secrets)) + '/*/*.token', 'owner\\.token',
                     '*.{py', 'hub/*.token', '/hub/*'):
            with self.subTest(glob=glob):
                self.denied(self.grep(self.w.home, glob=glob), 'hub-secrets', SUB)

    def test_a_filter_that_cannot(self):
        for glob in ('*.py', '*.{py,md}', 'src/**/*.py', '*.txt *.md', 'docs/*'):
            with self.subTest(glob=glob):
                self.allowed(self.grep(self.w.home, glob=glob), SUB)

    def test_a_names_search_is_not_hooked(self):
        # Glob is unhooked; a names search lists names and reads no token's text, so even
        # one in the folder or naming a token file passes (it was hub-secrets' list_reaches)
        parent = os.path.basename(os.path.dirname(self.secrets))
        for make in (self.glob('*', self.secrets), self.glob('*.py', cwd=self.secrets),
                     self.glob('**/*', self.w.home), self.glob('**', cwd=self.w.home),
                     self.glob(parent + '/*/*', self.w.home), self.glob(parent + '/{hub,x}/*', self.w.home),
                     self.glob(self.secrets.replace('\\', '/') + '/*', self.w.wt),
                     self.glob(os.path.relpath(self.secrets, self.w.wt).replace('\\', '/') + '/owner.token',
                               self.w.wt), self.glob('', self.w.home),
                     self.glob(parent + '/*', self.w.home), self.glob('src/*.py', self.w.home)):
            self.allowed(make)

    def test_a_filter_meets_what_is_in_the_folder(self):
        os.makedirs(os.path.join(self.secrets, 'sessions'))
        with open(os.path.join(self.secrets, 'sessions', 'b.py'), 'w', encoding='utf-8') as f:
            f.write('')
        self.denied(self.grep(self.w.home, glob='*.py'), 'hub-secrets', SUB)
        self.allowed(self.grep(self.w.home, glob='*.md'), SUB)

    def test_unrelated_searches_and_the_store_pass(self):
        for make in (self.grep(self.w.wt), self.grep(None), self.grep(self.store), self.grep(self.w.data),
                     self.glob('**/*', self.w.data), self.glob('**/*')):
            self.allowed(make)


class Malformed(unittest.TestCase):
    def test_a_field_of_the_wrong_type_raises_so_the_hook_denies(self):
        from hosts.claude.adapter.host import ADAPTER
        for tool, ti in (('Read', {'file_path': ['x']}), ('Grep', {'pattern': 'x', 'path': 5}),
                         ('Grep', {'pattern': 'x', 'glob': ['*']}), ('Read', ['x'])):
            with self.subTest(tool=tool, ti=ti):
                with self.assertRaises((ValueError, AttributeError)):
                    ADAPTER.parse('PreToolUse', event(tool, ti, 'C:/r'))
        # Glob is no read tool (it reads no token's text): its input is not read, so no shape of it raises
        for ti in ({'pattern': None, 'path': {}}, {'pattern': 3}):
            self.assertEqual(ADAPTER.parse('PreToolUse', event('Glob', ti, 'C:/r')).kind, 'other')


class Helpers(unittest.TestCase):
    def test_expand_braces(self):
        self.assertEqual(hubg.expand_braces('*.{a,b}x{1,2}'), ['*.ax1', '*.ax2', '*.bx1', '*.bx2'])
        self.assertEqual(hubg.expand_braces('plain'), ['plain'])
        for bad in ('{a', 'a}', '{a{b}}', '{' + ','.join('abcdefghijklmnopq') + '}' * 1 + '{1,2,3,4,5,6,7,8,9,0,a,b,c,d,e,f,g}'):
            self.assertIsNone(hubg.expand_braces(bad), bad)

    def test_filter_reaches_by_place(self):
        rel, names = ['.cfg', 'hub'], list(hubg.SECRET_FILES)
        self.assertTrue(hubg.filter_reaches('.cfg/hub/*.key', rel, names))
        self.assertTrue(hubg.filter_reaches('x/.cfg/hub/*.key', ['x'] + rel, names))
        self.assertTrue(hubg.filter_reaches('hub/sub/x.py', rel, names))     # a folder inside it
        self.assertFalse(hubg.filter_reaches('.cfg/hub/*.py', rel, names))
        self.assertFalse(hubg.filter_reaches('other/*', rel, names))
        self.assertTrue(hubg.filter_reaches(None, rel, names))
        self.assertTrue(hubg.filter_reaches(' , ', rel, names))


if __name__ == '__main__':
    unittest.main()
