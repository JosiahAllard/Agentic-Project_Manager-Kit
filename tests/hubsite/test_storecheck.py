"""kitlib.hubsite.storecheck: the findings of a hub store folder as it is on disk. Every folder is
a scratch one under a temp folder."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.hubsite import schema, storecheck
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


def labelled(recommended, labels=('Paint it', 'Leave it: as found', 'Ask again'), **kw):
    """A question as the earlier hub's writer left it: `recommended` holds an option's label."""
    return question(recommended=recommended, title='which one', options=[
        {'description': 'what %s means' % x, 'label': x} for x in labels], **kw)


def refused(lines):
    """The documents validate_lines says the api will refuse every update of."""
    return [x.split(':')[0] for x in lines if 'the api will refuse' in x]


class RecommendedRuleCase(unittest.TestCase):
    """schema.whole_recommended, the rule the store and storecheck read a question's text
    `recommended` by: text standing for a number is read as that number, any other text stays
    and storecheck.check reports it."""

    def read(self, fields, coll='questions'):
        """(the number the store reads, the text it stood for, whether it was a label), or None
        when the document is given back as it is."""
        got, old, label = schema.whole_recommended(coll, fields)
        if old is None:
            self.assertIs(got, fields, 'a document that stands for nothing is given back as is')
            self.assertFalse(label)
            return None
        self.assertEqual(got, dict(fields, recommended=got['recommended']),
                         'nothing else in the document is changed')
        return got['recommended'], old, label

    def lines(self, docs):
        return storecheck.validate_lines(storecheck.check({'questions': docs}))

    def test_a_whole_number_written_as_text_is_read_as_the_number(self):
        text = question(recommended='1', title='as text', options=[
            {'label': 'a', 'description': 'b'}, {'label': 'c', 'description': 'd'}])
        self.assertEqual(self.read(text), (1, '1', False))

    def test_text_that_is_not_a_whole_number_stays_and_is_reported(self):
        base = question(recommended='1', title='as text', options=[
            {'label': 'a', 'description': 'b'}, {'label': 'c', 'description': 'd'}])
        kept = {'Q-021': 'one', 'Q-022': '1.0', 'Q-023': '-1', 'Q-024': ' 1', 'Q-025': ''}
        docs = {doc_id: dict(base, recommended=text) for doc_id, text in kept.items()}
        for doc_id, fields in docs.items():
            self.assertIsNone(self.read(fields), doc_id)
        # no other field is read as a number, and no other collection's `recommended` is
        self.assertIsNone(self.read(question(title='7')))
        todo = {'status': 'open', 'title': 'run it', 'from': 'pm', 'commands': ['python x.py'],
                'recommended': '1'}
        self.assertIsNone(self.read(todo, coll='todos'))
        lines = self.lines(docs)
        self.assertIn('VALIDATE ERROR questions/Q-021 recommended: recommended must be int, not '
                      'str [schema-field]', lines)
        self.assertEqual(refused(lines), ['VALIDATE questions/%s' % i for i in sorted(kept)])

    def test_a_label_is_read_as_its_options_position(self):
        self.assertEqual(self.read(labelled('Leave it: as found')),
                         (1, 'Leave it: as found', True))
        self.assertEqual(self.read(labelled('Paint it', labels=('Paint it', 'Leave it'))),
                         (0, 'Paint it', True))                                   # the first
        self.assertEqual(self.read(labelled('Ask again')), (2, 'Ask again', True))  # the last
        self.assertEqual(self.read(labelled('1', labels=('Paint it', '1'))),     # both say 1
                         (1, '1', True))

    def test_text_that_is_not_exactly_one_options_label_stays_and_is_reported(self):
        kept = {
            'Q-033': labelled('Sell it'),                                   # no option's label
            'Q-034': labelled('Paint it', labels=('Paint it', 'Leave it', 'Paint it')),   # two
            'Q-035': labelled('paint it'),                                  # another case
            'Q-036': labelled('Paint it '),                                 # a space more
            'Q-037': question(recommended='Paint it', options=['Paint it', 'Leave it']),
            'Q-038': labelled('0', labels=('Paint it', '0')),   # the number 0, or the option at 1
            # the number 2, or one of the two options that are labelled 2: it does not say
            'Q-040': labelled('2', labels=('2', '2', 'Paint it')),
            'Q-043': labelled('1', labels=('Paint it', '1', '1')),  # even with one of them at 1
        }
        for doc_id, fields in kept.items():
            self.assertIsNone(self.read(fields), doc_id)
        lines = self.lines(dict(kept, **{'Q-039': labelled('1', labels=('Paint it', '1'))}))
        self.assertIn("VALIDATE ERROR questions/Q-033 recommended: recommended must be an index "
                      "into options (3 option(s)), not 'Sell it' [options-shape]", lines)
        self.assertIn('VALIDATE ERROR questions/Q-033 recommended: recommended must be int, not '
                      'str [schema-field]', lines)
        self.assertEqual(refused(lines), ['VALIDATE questions/%s' % i for i in sorted(kept)])

    def test_digits_too_many_to_be_a_position_stay_as_text(self):
        endless = '9' * 5000                    # more digits than int() reads
        kept = {
            'Q-044': labelled('1234567890'),                                # ten digits
            'Q-045': labelled(endless),
            'Q-046': labelled('1234567890', labels=('Paint it', '1234567890')),  # a label too
        }
        for doc_id, fields in kept.items():
            self.assertIsNone(self.read(fields), doc_id)
        self.assertEqual(self.read(labelled('000000001')), (1, '000000001', False))   # nine
        lines = self.lines(dict(kept, **{'Q-047': labelled('000000001')}))
        self.assertIn('VALIDATE ERROR questions/Q-044 recommended: recommended must be int, not '
                      'str [schema-field]', lines)
        self.assertEqual(refused(lines), ['VALIDATE questions/%s' % i for i in sorted(kept)])

    def test_the_empty_text_is_never_an_options_label(self):
        fields = labelled('', labels=('', 'Paint it'))
        self.assertIsNone(self.read(fields))
        self.assertIn('VALIDATE ERROR questions/Q-048 recommended: recommended must be int, not '
                      'str [schema-field]', self.lines({'Q-048': fields}))

    def test_a_number_past_the_last_option_is_read_as_that_number_and_reported(self):
        self.assertEqual(self.read(labelled('7')), (7, '7', False))
        self.assertIn('VALIDATE ERROR questions/Q-049 recommended: recommended must be an index '
                      'into options (3 option(s)), not 7 [options-shape]',
                      self.lines({'Q-049': labelled('7')}))

    def test_an_answered_question_keeps_its_answer_as_it_is(self):
        answer = {'at': AT, 'choice': 0, 'label': 'Paint it', 'note': ''}
        fields = labelled('Paint it', status='answered', answer=answer)
        got, _old, _label = schema.whole_recommended('questions', fields)
        self.assertEqual(got, dict(fields, recommended=0))
        self.assertEqual(got['answer'], answer)


if __name__ == '__main__':
    unittest.main()
