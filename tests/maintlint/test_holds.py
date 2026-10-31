"""kitlib.maintlint.holds: the keyword hold check carried over from the retired backlog_lint tool, read on an
in-memory snapshot."""
import unittest

from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.maintlint import holds

from tests.backlog.helpers import priced, rec

CMD = 'python "kit/tools/backlog.py" -C "wt"'


def snap(*records):
    texts = {}
    for r in records:
        texts.update(r)
    parsed = [rmod.parse('backlog/%s.md' % stem, text, stem) for stem, text in texts.items()]
    return smod.Snapshot(parsed, 'a' * 40, 'test', rmod.Rules('upper', ('L0', 'L1')))


def one(**over):
    return snap(priced('AAA', **over)).by_id['AAA']


class KeywordHit(unittest.TestCase):

    HITS = [('owner', 'waits on the owner go'), ('owner', "waits on the owner's go"),
            ('owner', 'needs an owner run first'), ('owner', 'an owner-run check'),
            ('owner', 'owner present at the desk'), ('live-client', 'needs a live client'),
            ('live-client', 'the live-client test'), ('live-client', 'two live runs')]
    MISSES = ['the owner goes home', 'a lively client', 'owner ran it', 'live running total',
              'ownership go']

    def test_every_pattern_hits(self):
        for member, text in self.HITS:
            with self.subTest(text=text):
                got, _hit = holds.keyword_hit(one(summary=text))
                self.assertEqual(got, member)

    def test_near_misses_do_not_hit(self):
        for text in self.MISSES:
            with self.subTest(text=text):
                self.assertEqual(holds.keyword_hit(one(summary=text)), (None, None))

    def test_requires_and_expect_are_read_and_the_body_is_not(self):
        self.assertEqual(holds.keyword_hit(one(requires='an owner run'))[0], 'owner')
        self.assertEqual(holds.keyword_hit(one(expect='a live client'))[0], 'live-client')
        r = snap(priced('AAA', body='Quote: this needs the owner go.')).by_id['AAA']
        self.assertEqual(holds.keyword_hit(r), (None, None))

    def test_owner_is_tried_before_live_client(self):
        self.assertEqual(holds.keyword_hit(one(summary='a live client, then the owner go'))[0],
                         'owner')


class Findings(unittest.TestCase):

    def test_an_offerable_record_with_a_hit_is_a_finding_with_its_block_command(self):
        got = holds.findings(snap(priced('AAA', summary='needs the owner go'),
                                  rec('BBB', summary='needs a live client'),
                                  priced('CCC')), CMD)
        self.assertEqual([(f.check, f.key) for f in got],
                         [('keyword-hold', 'AAA'), ('keyword-hold', 'BBB')])
        self.assertEqual(got[0].fix, CMD + ' block AAA --on owner --reason "<what it waits on and'
                         ' until when, citing the ruling or question>"')
        self.assertIn('--on live-client', got[1].fix)

    def test_a_held_record_is_not_a_finding(self):
        got = holds.findings(snap(rec('AAA', summary='needs the owner go', state='blocked',
                                      blocked_on='owner'),
                                  rec('BBB', summary='needs a live client', state='parked')), CMD)
        self.assertEqual(got, [])

    def test_a_uses_line_answers_a_live_client_hit(self):
        got = holds.findings(snap(priced('AAA', summary='two live runs', uses='dev-clone')), CMD,
                             {'dev-clone': 1})
        self.assertEqual(got, [])

    def test_a_uses_line_does_not_answer_an_owner_hit(self):
        got = holds.findings(snap(priced('AAA', summary='the owner go', uses='dev-clone')), CMD,
                             {'dev-clone': 1})
        self.assertEqual([f.key for f in got], ['AAA'])
        self.assertNotIn('--uses', got[0].fix)

    def test_the_fix_names_uses_when_the_repository_declares_resources(self):
        records = snap(priced('AAA', summary='needs a live client'))
        got = holds.findings(records, CMD, {'gpu': 1, 'dev-clone': 1})
        self.assertIn('--on live-client', got[0].fix)
        self.assertIn(CMD + ' set AAA --uses <dev-clone|gpu>', got[0].fix)
        self.assertNotIn('--uses', holds.findings(records, CMD)[0].fix)


if __name__ == '__main__':
    unittest.main()
