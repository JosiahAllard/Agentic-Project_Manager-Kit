"""kitlib.gate.measure: a proposed kinds table from run history; the live table is never changed."""
import copy
import unittest

from kitlib.gate import measure

TABLE = {'engine-test': {'cpu': 'medium', 'gpu': 'none', 'warn_min': 30},
         'cpu-eval': {'cpu': 'medium', 'gpu': 'none', 'warn_min': 10}}


def run(kind, held, peak=None, how='done', mem=100):
    return {'kind': kind, 'how': how, 'held_s': held, 'peak_cpu_cores': peak, 'peak_mem_mb': mem}


class Propose(unittest.TestCase):
    def test_percentile_is_nearest_rank(self):
        self.assertEqual(measure.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9), 9)
        self.assertEqual(measure.percentile([5], 0.9), 5)
        self.assertEqual(measure.percentile([3, 1, 2], 0.5), 2)

    def test_measured_kind_gets_warn_and_cpu_from_p90(self):
        hist = [run('engine-test', h, peak=p) for h, p in ((60, 1), (120, 2), (600, 7.5))]
        new, notes = measure.propose(TABLE, hist, medium_cores=8)
        e = new['engine-test']
        self.assertEqual(e['warn_min'], 15)            # ceil(600 * 1.5 / 60)
        self.assertEqual(e['cpu'], 'heavy')            # p90 peak 7.5 >= 0.9 * 8
        self.assertEqual(e['gpu'], 'none')             # kept: history does not measure it
        self.assertEqual(e['_measured']['runs'], 3)
        self.assertEqual(e['_measured']['peak_mem_mb_max'], 100)
        self.assertIn('engine-test: warn_min 30 -> 15; cpu medium -> heavy (3 runs)', notes)

    def test_unsaturated_runs_stay_medium_and_warn_is_at_least_one(self):
        hist = [run('cpu-eval', 1, peak=2) for _ in range(3)]
        new, _ = measure.propose(TABLE, hist, medium_cores=8)
        self.assertEqual(new['cpu-eval']['cpu'], 'medium')
        self.assertEqual(new['cpu-eval']['warn_min'], 1)

    def test_too_few_finished_runs_keep_the_entry(self):
        hist = [run('engine-test', 999, peak=8), run('engine-test', 999, peak=8),
                run('engine-test', 999, peak=8, how='cancelled')]
        new, notes = measure.propose(TABLE, hist, medium_cores=8)
        self.assertEqual(new['engine-test'], TABLE['engine-test'])
        self.assertIn('engine-test: kept (2 finished run(s); 3 needed)', notes)

    def test_the_input_table_is_not_changed(self):
        before = copy.deepcopy(TABLE)
        measure.propose(TABLE, [run('engine-test', 600, peak=8)] * 3, medium_cores=8)
        self.assertEqual(TABLE, before)

    def test_document_has_the_live_file_shape(self):
        doc = measure.document({'k': {}}, 'note')
        self.assertEqual(doc, {'_note': 'note', 'kinds': {'k': {}}})

    def test_document_carries_the_live_files_aliases(self):
        aliases = {'old': {'to': 'k', 'why': 'w'}}
        doc = measure.document({'k': {}}, 'note', aliases)
        self.assertEqual(doc['aliases'], aliases)
        self.assertIsNot(doc['aliases'], aliases)



def booked(kind, held, peak, cores=None, need=None):
    d = run(kind, held, peak=peak)
    if cores is not None:
        d['cores'] = list(range(cores))
    if need is not None:
        d['need'] = {'cpu': need, 'gpu': 0}
    return d


class IntegerSizes(unittest.TestCase):
    """With the layout's agent_cap given (gate.py --measure gives it), every kind in the
    proposal gets a whole number of cores 1..agent_cap. Runs are pinned to their booked cores, so
    a kind whose runs fill what they booked cannot show more: it is proposed the agent cap."""

    def prop(self, hist, table=TABLE, **kw):
        return measure.propose(table, hist, medium_cores=2, agent_cap=6, **kw)

    def test_an_unsaturated_kind_gets_its_p90_peak_rounded_up(self):
        hist = [booked('engine-test', 60, p, cores=6) for p in (2.1, 2.5, 2.9)]
        new, notes = self.prop(hist)
        self.assertEqual(new['engine-test']['cpu'], 3)
        self.assertIs(type(new['engine-test']['cpu']), int)
        self.assertIn('engine-test: warn_min 30 -> 2; cpu medium -> 3 (3 runs)', notes)
        self.assertEqual(new['engine-test']['_measured']['agent_cap'], 6)

    def test_a_kind_that_fills_its_booking_gets_the_agent_cap(self):
        hist = [booked('engine-test', 60, 1.95, cores=2) for _ in range(3)]
        self.assertEqual(self.prop(hist)[0]['engine-test']['cpu'], 6)

    def test_the_booking_is_read_from_need_or_the_entry_when_cores_are_missing(self):
        by_need = [booked('engine-test', 60, 2.9, need=3) for _ in range(3)]
        self.assertEqual(self.prop(by_need)[0]['engine-test']['cpu'], 6)   # 2.9 of 3 booked
        by_entry = [booked('engine-test', 60, 1.9) for _ in range(3)]       # medium: 2 here
        self.assertEqual(self.prop(by_entry)[0]['engine-test']['cpu'], 6)
        sized = {'engine-test': {'cpu': 4, 'gpu': 0, 'warn_min': 30}}
        self.assertEqual(self.prop(by_entry, table=sized)[0]['engine-test']['cpu'], 2)

    def test_at_least_one_core_and_never_past_the_cap(self):
        idle = [booked('engine-test', 60, 0.0, cores=6) for _ in range(3)]
        self.assertEqual(self.prop(idle)[0]['engine-test']['cpu'], 1)
        wide = {'engine-test': {'cpu': 9, 'gpu': 0, 'warn_min': 30}}
        full = [booked('engine-test', 60, 5.9, cores=6) for _ in range(3)]
        self.assertEqual(self.prop(full, table=wide)[0]['engine-test']['cpu'], 6)

    def test_a_kept_kind_is_written_as_its_size_here_and_says_so(self):
        new, notes = self.prop([])
        self.assertEqual(new['engine-test']['cpu'], 2)          # medium on this layout
        self.assertEqual(new['engine-test']['warn_min'], 30)
        self.assertNotIn('_measured', new['engine-test'])
        self.assertIn('engine-test: kept (0 finished run(s); 3 needed); cpu medium -> 2 '
                      '(its size on this layout)', notes)
        heavy = {'k': {'cpu': 'heavy', 'gpu': 0, 'warn_min': 30}}
        self.assertEqual(self.prop([], table=heavy)[0]['k']['cpu'], 6)

    def test_runs_without_a_peak_keep_the_size_as_an_integer(self):
        hist = [booked('engine-test', 60, None, cores=2) for _ in range(3)]
        self.assertEqual(self.prop(hist)[0]['engine-test']['cpu'], 2)

    def test_every_kind_in_the_proposal_is_a_whole_number(self):
        hist = [booked('engine-test', 60, 2.5, cores=6) for _ in range(3)]
        new, _ = self.prop(hist)
        for kind, e in new.items():
            self.assertIs(type(e['cpu']), int, kind)
            self.assertTrue(1 <= e['cpu'] <= 6, kind)


if __name__ == '__main__':
    unittest.main()
