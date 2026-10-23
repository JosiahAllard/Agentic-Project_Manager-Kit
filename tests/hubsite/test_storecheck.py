"""kitlib.hubsite.storecheck: the findings of a hub store folder as it is on disk. Every folder is
a scratch one under a temp folder."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.hubsite import storecheck
from kitlib.hubsite.storecheck import StoreReadError
from kitlib.hubsite.store import HubError, Store, normal_text
from tests.home import machine_config

AT = '2026-01-02T03:04:05+00:00'


def question(**kw):
    d = {'status': 'open', 'title': 'which way', 'repo': 'pm', 'asked_at': AT,
         'options': [{'label': 'a', 'description': 'b'}], 'recommended': 0}
    d.update(kw)
    return d


# A store's documents, one or more of each collection; Q-007 is tagged `kit`, which repos/ names.
DOCS = {
    'questions': {
        'Q-001': question(),
        'Q-002': question(status='answered', answer={'choice': 0, 'at': AT}),
        'Q-007': question(status='logged', repo='kit'),
    },
    'inbox': {'M-001': {'text': 'the answer is in', 'ref': 'Q-001', 'at': AT, 'read': False}},
    'todos': {
        'T-001': {'status': 'open', 'title': 'run it', 'from': 'pm', 'commands': ['python x.py']},
        'T-002': {'status': 'done', 'title': 'ran it', 'from': 'you', 'done_at': AT,
                  'pm_ack': True},
    },
    'to_pm': {'P-001': {'status': 'done', 'kind': 'question', 'text': 'why', 'reply': 'because'}},
    'stories': {'S-001': {'status': 'added', 'repo': 'pm', 'title': 's', 'story': 'as an owner'}},
    'pages': {'guide': {'title': 'Guide'}},
    'page_html': {'guide': {'html': '<p>read me</p>'}},
    'repos': {'kit': {'name': 'kit', 'added_at': AT, 'aliases': ['k']}},
    'parked': {'one': {'title': 'parked a', 'q': 'Q-001'}},
}


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(obj if isinstance(obj, str) else normal_text(obj))


def fill(folder, docs):
    for coll, by_id in docs.items():
        for doc_id, fields in by_id.items():
            write(os.path.join(folder, coll, doc_id + '.json'), fields)
    return folder


def tree(folder):
    """{relative path: (bytes, mtime)} of every file under folder."""
    out = {}
    for base, _dirs, names in os.walk(folder):
        for n in names:
            path = os.path.join(base, n)
            with open(path, 'rb') as f:
                out[os.path.relpath(path, folder).replace(os.sep, '/')] = (
                    f.read(), os.stat(path).st_mtime_ns)
    return out


def on_disk(root, coll, doc_id):
    with open(os.path.join(root, coll, doc_id + '.json'), encoding='utf-8') as f:
        return json.load(f)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kit-hubstorecheck-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = fill(os.path.join(self.tmp, 'store'), DOCS)
        # validate reads the machine's repo names: a temp home, so this machine's are not used
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        env = mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home})
        env.start()
        self.addCleanup(env.stop)


class ValidateCase(Case):
    def test_a_clean_store_has_no_finding_and_says_so(self):
        self.assertEqual(storecheck.validate(self.root), [])
        self.assertEqual(storecheck.validate_lines([]), ['VALIDATE clean: no finding'])

    def test_a_document_a_new_write_would_refuse_is_reported(self):
        write(os.path.join(self.root, 'todos', 'T-003.json'),
              {'status': 'open', 'title': 'a hand step', 'from': 'pm'})       # no commands
        write(os.path.join(self.root, 'questions', 'Q-003.json'), question(status='shelved'))
        write(os.path.join(self.root, 'inbox', 'M-002.json'), {'at': AT})   # no text
        findings = storecheck.validate(self.root)
        self.assertEqual(sorted((f.rel, f.code) for f in findings), [
            ('inbox/M-002', 'schema-field'), ('questions/Q-003', 'status-unknown'),
            ('todos/T-003', 'handstep-commands')])
        lines = storecheck.validate_lines(findings)
        self.assertEqual(lines[-1], 'VALIDATE 2 error(s), 1 warning(s)')
        # the one document the store's own check refuses to write again is said so, plainly
        self.assertEqual([x for x in lines if 'the api will refuse' in x], [
            'VALIDATE inbox/M-002: the api will refuse every update of this document until it is '
            'fixed; fix its file by hand with the server stopped (no api route repairs it)'])
        with Store.open(self.root) as s:
            with self.assertRaises(HubError):
                s.update('inbox', 'M-002', {'read': True})
            s.update('questions', 'Q-003', {'title': 'a finding it already had does not block'})
        self.assertTrue(any(x.startswith('VALIDATE ERROR questions/Q-003 status') for x in lines))

    def test_validate_reads_beside_a_holder_and_makes_nothing(self):
        with Store.open(self.root):
            before = tree(self.root)
            self.assertEqual(storecheck.validate(self.root), [])
            self.assertEqual(tree(self.root), before)
        missing = os.path.join(self.tmp, 'no-store')
        with self.assertRaises(StoreReadError):
            storecheck.validate(missing)
        self.assertFalse(os.path.exists(missing))

    def test_with_no_repos_document_the_tags_are_noted_once_not_warned_each(self):
        os.remove(os.path.join(self.root, 'repos', 'kit.json'))     # Q-007 is tagged `kit`
        self.assertEqual([(f.level, f.rel, f.code) for f in storecheck.validate(self.root)],
                         [('note', '', 'repo-alias')])
        # with a repos/ document the tags are judged: one the store does not know is a warning
        write(os.path.join(self.root, 'repos', 'other.json'), {'name': 'other', 'added_at': AT})
        self.assertEqual([(f.level, f.rel, f.code) for f in storecheck.validate(self.root)],
                         [('warn', 'questions/Q-007', 'repo-alias')])

    def test_a_store_that_holds_the_text_of_a_number_is_warned_until_its_next_write(self):
        text = question(recommended='1', title='as text', options=[
            {'label': 'a', 'description': 'b'}, {'label': 'c', 'description': 'd'}])
        write(os.path.join(self.root, 'questions', 'Q-020.json'), text)
        findings = storecheck.validate(self.root)
        self.assertEqual(
            [x for x in storecheck.validate_lines(findings) if 'questions/Q-020' in x], [
                "VALIDATE WARN questions/Q-020 recommended: recommended is the text '1' in its "
                "file; the store reads it as the number 1 and its next write stores the number "
                "[schema-field]"])
        with Store.open(self.root) as s:     # the api's update goes through
            s.update('questions', 'Q-020', {'status': 'answered', 'answer': {'choice': 1}})
        self.assertEqual(on_disk(self.root, 'questions', 'Q-020')['recommended'], 1)
        self.assertEqual([f for f in storecheck.validate(self.root) if f.rel == 'questions/Q-020'],
                         [])


class KnownReposCase(Case):
    """validate judges a repo tag against the repo names the live server knows."""

    def store_with(self, *repos):
        return fill(os.path.join(self.tmp, 'tagged'),
                    {'questions': {'Q-%03d' % i: question(repo=r) for i, r in enumerate(repos, 1)}})

    def codes(self, findings):
        return [(f.rel, f.code) for f in findings]

    def test_check_passes_known_repos_so_a_known_tag_is_clean_and_an_unknown_one_is_flagged(self):
        docs = {'questions': {'Q-001': question(repo='Gamerepo'), 'Q-002': question(repo='ghost')}}
        for known in (['gamerepo'], lambda: ['gamerepo']):
            self.assertEqual(self.codes(storecheck.check(docs, known)),
                             [('questions/Q-002', 'repo-alias')])
        # no repos/ and no known repos: the one note that tags were not checked, as before
        self.assertEqual([(f.rel, f.code) for f in storecheck.check(docs)], [('', 'repo-alias')])

    def test_validate_asks_the_machine_sources_for_the_known_repos(self):
        root = self.store_with('gamerepo', 'ghost')
        sessions = os.path.join(self.home, 'pm', 's.json')
        write(sessions, json.dumps({'g': {'repo': 'Gamerepo'}}))
        machine_config(self.home, {'pm_start': {'sessions_file': sessions}}, case=self)
        with mock.patch('kitlib.pmstart._toplevel', lambda folder: None):   # no git
            found = storecheck.validate(root)
        self.assertEqual(self.codes(found), [('questions/Q-002', 'repo-alias')])

    def test_validate_with_no_machine_repos_gives_the_one_note_as_the_server_has_no_names(self):
        found = storecheck.validate(self.store_with('gamerepo', 'ghost'))
        self.assertEqual(self.codes(found), [('', 'repo-alias')])

    def test_validate_takes_known_repos_given_to_it(self):
        found = storecheck.validate(self.store_with('gamerepo', 'ghost'), known_repos=['gamerepo'])
        self.assertEqual(self.codes(found), [('questions/Q-002', 'repo-alias')])


if __name__ == '__main__':
    unittest.main()
