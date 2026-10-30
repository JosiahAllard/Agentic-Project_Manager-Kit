"""kitlib.coordstate.render: the state note joins the event log with the land result records and
a submission's output file (docs/coord-state.md "The state note"). The landing feed's WARNING is
read from the output's outcome lines (after the last `result record:` line), never from the
record; a landing is to report until a `reported` event names it."""
import os
import tempfile
import unittest

from kitlib.coordstate import log, render
from kitlib.gate import submit
from kitlib.hubsite import landings
from tests.gitfixture import rmtree

TIP = 'a' * 40
TIP2 = 'b' * 40


def submitted(branch, tip=TIP, n=0, output=None, utc='2026-10-30T10:00:00Z', batch=()):
    return {'kind': 'land_submit', 'branches': [branch] + list(batch),
            'tips': [tip] + [TIP2] * len(batch), 'reviewed_by': 'analyst', 'output': output,
            'utc': utc, '_n': n}


def record(branch, verdict, tip=TIP, summary='done'):
    return {'branch': branch, 'kind': 'land', 'tip': tip, 'verdict': verdict, 'summary': summary,
            'started_at': '2026-10-30T10:00:01'}


class Outputs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-coordstate-')
        self.addCleanup(rmtree, self.dir)

    def output(self, *lines):
        path = os.path.join(self.dir, 'b.%d.land.log' % len(os.listdir(self.dir)))
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
        return path


class FeedState(Outputs):
    def test_the_feed_warning_is_in_the_outcome_block(self):
        warning = landings._warning(['kit/b'], 'no hub running')
        path = self.output('merged gate: pass', 'result record: X', warning,
                           'LANDED kit/b on main at abc (pushed and verified on origin)')
        self.assertEqual(render.feed_state(submit.outcome_block(path)), warning)

    def test_landed_with_no_warning_is_on_the_feed(self):
        path = self.output('result record: X', 'LANDED kit/b on main at abc (pushed)')
        self.assertEqual(render.feed_state(submit.outcome_block(path)), render.POSTED)

    def test_a_warning_before_the_record_line_is_not_the_feeds(self):
        warning = landings._warning(['kit/b'], 'old')
        path = self.output(warning, 'result record: X', 'LANDED kit/b on main at abc')
        self.assertEqual(render.feed_state(submit.outcome_block(path)), render.POSTED)

    def test_between_the_record_and_the_post_it_is_pending(self):
        path = self.output('result record: X')
        self.assertEqual(render.feed_state(submit.outcome_block(path)), render.PENDING)

    def test_an_unreadable_output_is_unknown(self):
        lines = submit.outcome_block(os.path.join(self.dir, 'gone.land.log'))
        self.assertEqual(render.feed_state(lines), render.UNKNOWN)


class Groups(Outputs):
    def build(self, events, records):
        def read(root, branch, kind):
            self.assertEqual(kind, 'land')
            rec = records.get(branch)
            return (rec, None) if rec else (None, 'none')
        return render.build('unused-root', events, read=read)[0]

    def group_of(self, events, records):
        groups = self.build(events, records)
        return {i['branch']: g for g, items in groups.items() for i in items}

    def test_each_outcome_lands_in_its_group(self):
        posted = self.output('result record: X', 'LANDED b1 on main')
        warned = self.output('result record: X', landings._warning(['b2'], 'no hub'), 'LANDED b2')
        events = [submitted('b1', output=posted, n=0), submitted('b2', output=warned, n=1),
                  submitted('b3', n=2, output=posted), submitted('b4', n=3, output=posted),
                  submitted('b5', n=4, output=None), submitted('b6', n=5, output=posted),
                  submitted('b7', n=6, output=self.output('result record: X'))]
        records = {'b1': record('b1', 'pass'), 'b2': record('b2', 'pass'),
                   'b3': record('b3', 'fail'), 'b4': record('b4', 'running'),
                   'b5': record('b5', 'pass'), 'b6': record('b6', 'pass', tip=TIP2),
                   'b7': record('b7', 'pass')}
        self.assertEqual(self.group_of(events, records), {
            'b1': render.LANDED_POSTED, 'b2': render.TO_REPORT, 'b3': render.TO_REPORT,
            'b4': render.IN_FLIGHT, 'b5': render.TO_REPORT, 'b6': render.IN_FLIGHT,
            'b7': render.IN_FLIGHT})

    def test_no_record_yet_is_in_flight(self):
        self.assertEqual(self.group_of([submitted('b1')], {}), {'b1': render.IN_FLIGHT})

    def test_reported_clears_a_landing_and_a_resubmission_brings_it_back(self):
        events = [submitted('b1', n=0), {'kind': 'reported', 'branch': 'b1', 'utc': 'u', '_n': 1}]
        records = {'b1': record('b1', 'fail')}
        self.assertEqual(self.group_of(events, records), {'b1': render.REPORTED})
        events.append(submitted('b1', tip=TIP2, n=2))
        records = {'b1': record('b1', 'fail', tip=TIP2)}
        self.assertEqual(self.group_of(events, records), {'b1': render.TO_REPORT})

    def test_a_mark_counts_only_on_a_final_record(self):
        events = [submitted('b1', n=0), {'kind': 'reported', 'branch': 'b1', 'utc': 'u', '_n': 1}]
        self.assertEqual(self.group_of(events, {}), {'b1': render.IN_FLIGHT})
        self.assertEqual(self.group_of(events, {'b1': record('b1', 'running')}),
                         {'b1': render.IN_FLIGHT})

    def test_reported_at_another_tip_clears_nothing(self):
        events = [submitted('b1', n=0), {'kind': 'reported', 'branch': 'b1', 'tip': 'bbbb', '_n': 1}]
        self.assertEqual(self.group_of(events, {'b1': record('b1', 'fail')}),
                         {'b1': render.TO_REPORT})

    def test_a_batch_gives_each_branch_its_item(self):
        groups = self.build([submitted('b1', batch=['b2'])],
                            {'b1': record('b1', 'fail'), 'b2': record('b2', 'fail', tip=TIP2)})
        self.assertEqual(sorted(i['branch'] for i in groups[render.TO_REPORT]), ['b1', 'b2'])


class Text(unittest.TestCase):
    def test_the_note_names_its_sections_and_stays_under_the_cap(self):
        backlog = [{'kind': 'backlog', 'verb': 'set', 'id': 'R%04d' % i, 'utc': 'u', '_n': i}
                   for i in range(400)]
        groups = {render.TO_REPORT: [{'branch': 'b1', 'tip': TIP, 'utc': 'u', 'say': 'fail: x',
                                      'batch': [], 'events': [0], 'reported': None}]}
        note = render.text('coord-x', '/x/events-coord-x.jsonl', groups, backlog, [], None,
                           'my judgment\n')
        self.assertLessEqual(len(note.encode('utf-8')), render.CAP)
        for part in ('# Coordinator state: coord-x', 'my judgment', 'Landings to report to the PM (1)',
                     '- b1 at aaaaaaaaaa', 'Verdicts queued by coord-x (0)', 'Backlog writes'):
            self.assertIn(part, note)
        self.assertIn('R0399', note)            # the newest backlog write is kept

    def test_an_unreadable_queue_is_said(self):
        note = render.text('s', 'e', {}, [], None, 'bad file', '')
        self.assertIn('the verdict queue cannot be read: bad file', note)

    def test_an_unknown_main_is_said_under_the_entries(self):
        note = render.text('s', 'e', {}, [], [], 'main as fetched cannot be read (x)', '')
        self.assertIn('Verdicts queued by s (0)\nmain as fetched cannot be read (x)', note)

    def test_keep_is_what_the_note_shows(self):
        old = [{'kind': 'backlog', 'id': str(i), '_n': i} for i in range(30)]
        groups = {render.IN_FLIGHT: [{'events': [100, 101]}],
                  render.REPORTED: [{'events': [200 + i]} for i in range(9)]}
        ns = render.keep(groups, old)
        self.assertEqual(ns, {100, 101} | set(range(204, 209)) | set(range(15, 30)))


class LogConstants(unittest.TestCase):
    def test_compaction_starts_well_below_the_cap(self):
        self.assertLess(log.COMPACT_AT * 2, log.MAX_BYTES)


if __name__ == '__main__':
    unittest.main()
