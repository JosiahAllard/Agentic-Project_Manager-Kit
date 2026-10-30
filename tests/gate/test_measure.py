"""kitlib.gate.measure: a proposed kinds table from run history; the live table is never changed."""
import copy
import unittest

from kitlib.gate import measure

TABLE = {'engine-test': {'cpu': 'medium', 'gpu': 'none', 'warn_min': 30},
         'cpu-eval': {'cpu': 'medium', 'gpu': 'none', 'warn_min': 10}}


def run(kind, held, peak=None, how='done', mem=100, cpu_s=None):
    # Every real history line carries cpu_s (kitlib/lock/store.py HISTORY_KEYS); the default is
    # one cpu thread busy for the whole run.
    return {'kind': kind, 'how': how, 'held_s': held, 'peak_cpu_cores': peak, 'peak_mem_mb': mem,
            'cpu_s': held if cpu_s is None else cpu_s}


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
        hist = [run('cpu-eval', 12, peak=2) for _ in range(3)]     # 12 s: long enough to be a cpu sample
        new, _ = measure.propose(TABLE, hist, medium_cores=8)
        self.assertEqual(new['cpu-eval']['cpu'], 'medium')
        self.assertEqual(new['cpu-eval']['warn_min'], 1)

    def test_too_few_finished_runs_keep_the_entry(self):
        hist = [run('engine-test', 999, peak=8), run('engine-test', 999, peak=8),
                run('engine-test', 999, peak=8, how='cancelled')]
        new, notes = measure.propose(TABLE, hist, medium_cores=8)
        self.assertEqual(new['engine-test'], TABLE['engine-test'])
        self.assertIn('engine-test: kept (2 finished run(s); 3 needed)', notes)

    def test_runs_under_a_kinds_retired_name_count_as_that_kind(self):
        """Red before: a history line under the old name (from before the rename) was no run of
        the kind, so two old and one new run were too few."""
        hist = [run('old-test', 600, peak=8), run('old-test', 600, peak=8),
                run('engine-test', 600, peak=8)]
        aliases = {'old-test': {'to': 'engine-test', 'why': 'renamed'}}
        new, notes = measure.propose(TABLE, hist, medium_cores=8, aliases=aliases)
        self.assertEqual(new['engine-test']['_measured']['runs'], 3)
        new, _ = measure.propose(TABLE, hist, medium_cores=8)
        self.assertEqual(new['engine-test'], TABLE['engine-test'])

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
        light = [booked('engine-test', 60, 0.2, cores=6) for _ in range(3)]
        self.assertEqual(self.prop(light)[0]['engine-test']['cpu'], 1)
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
        new, notes = self.prop(hist)
        self.assertEqual(new['engine-test']['cpu'], 2)
        self.assertIn('engine-test: kept (0 cpu sample(s) of 3 finished run(s); 3 needed; skipped: '
                      '3 without cpu figures (none, or a peak of 0)); cpu medium -> 2 '
                      '(its size on this layout)', notes)

    def test_every_kind_in_the_proposal_is_a_whole_number(self):
        hist = [booked('engine-test', 60, 2.5, cores=6) for _ in range(3)]
        new, _ = self.prop(hist)
        for kind, e in new.items():
            self.assertIs(type(e['cpu']), int, kind)
            self.assertTrue(1 <= e['cpu'] <= 6, kind)


SIZED = {'engine-test': {'cpu': 'medium', 'gpu': 0, 'warn_min': 30},
         'cpu-eval': {'cpu': 'heavy', 'gpu': 0, 'warn_min': 120}}
IDLE = "under 0.1 cpu thread busy (work outside the run's job, or idle)"
SHORT = 'held under 10 s (no peak window)'
PARTIAL = 'partial (docker exec: use partly outside the job)'
NO_FIGURES = 'without cpu figures (none, or a peak of 0)'


def line(kind, held, cpu_s, peak, cores=6, partial=False):
    """A history line as the lock writes it: the run held `cores` slots for `held` seconds and
    its job used `cpu_s` cpu seconds, `peak` busy slots in its busiest sample window."""
    d = run(kind, held, peak=peak, cpu_s=cpu_s)
    d.update(cores=list(range(cores)), partial=partial)
    return d


class OutOfJob(unittest.TestCase):
    """The lock measures a run's job object only. A run whose work happens outside it (a
    container), or that ended before a sample window closed, shows a peak of about 0 whatever it
    did: such a line is not a cpu sample, and a kind without enough cpu samples is kept whole,
    with a note that says why."""

    def prop(self, hist, table=SIZED, **kw):
        kw.setdefault('agent_cap', 6)
        return measure.propose(table, hist, medium_cores=2, **kw)

    def note(self, notes, kind):
        return [n for n in notes if n.startswith(kind + ':')][0]

    def test_runs_whose_work_is_outside_the_job_keep_the_whole_entry(self):
        hist = [line('cpu-eval', h, c, p) for h, c, p in (
            (1582, 0.6, 0.0), (1786, 0.7, 0.0), (2053, 0.7, 0.0), (2014, 1.0, 0.01),
            (1662, 0.6, 0.0))]
        new, notes = self.prop(hist)
        self.assertEqual(new['cpu-eval'], {'cpu': 6, 'gpu': 0, 'warn_min': 120})
        self.assertEqual(self.note(notes, 'cpu-eval'),
                         'cpu-eval: kept (0 cpu sample(s) of 5 finished run(s); 3 needed; '
                         'skipped: 4 %s, 1 %s); cpu heavy -> 6 (its size on this layout)'
                         % (NO_FIGURES, IDLE))

    def test_a_run_too_short_for_a_peak_window_is_not_a_sample(self):
        # 0.4 cpu seconds in 1 second is work in the job, and the peak is still 0.
        new, notes = self.prop([line('engine-test', 1, 0.4, 0.0) for _ in range(5)])
        self.assertEqual(new['engine-test'], {'cpu': 2, 'gpu': 0, 'warn_min': 30})
        self.assertEqual(self.note(notes, 'engine-test'),
                         'engine-test: kept (0 cpu sample(s) of 5 finished run(s); 3 needed; '
                         'skipped: 5 %s); cpu medium -> 2 (its size on this layout)' % SHORT)

    def test_a_partial_run_is_not_a_sample(self):
        hist = [line('engine-test', 60, 300, 2.5, partial=True) for _ in range(3)]
        new, notes = self.prop(hist)
        self.assertEqual(new['engine-test'], {'cpu': 2, 'gpu': 0, 'warn_min': 30})
        self.assertIn('skipped: 3 %s)' % PARTIAL, self.note(notes, 'engine-test'))

    def test_a_line_without_cpu_figures_is_not_a_sample(self):
        no_key = [line('engine-test', 60, 300, 2.5) for _ in range(3)]
        for d in no_key:
            del d['cpu_s']
        new, notes = self.prop(no_key)
        self.assertEqual(new['engine-test'], {'cpu': 2, 'gpu': 0, 'warn_min': 30})
        self.assertIn('skipped: 3 %s)' % NO_FIGURES, self.note(notes, 'engine-test'))
        for bad in (None, True, -1, 'x'):
            hist = [dict(line('engine-test', 60, 300, 2.5), cpu_s=bad) for _ in range(3)]
            self.assertNotIn('_measured', self.prop(hist)[0]['engine-test'], bad)
        no_peak = [line('engine-test', 60, 300, None) for _ in range(3)]
        self.assertIn('skipped: 3 %s)' % NO_FIGURES,
                      self.note(self.prop(no_peak)[1], 'engine-test'))

    def test_a_kind_is_sized_from_its_cpu_samples_only_and_says_what_it_skipped(self):
        real = [line('engine-test', 60, 300, p) for p in (2.1, 2.5, 2.9)]
        blind = [line('engine-test', 600, 0.5, 0.01) for _ in range(20)]
        new, notes = self.prop(real + blind)
        e = new['engine-test']
        self.assertEqual(e['cpu'], 3)                  # ceil(2.9), not the 1 of all 23 lines
        self.assertEqual(e['warn_min'], 15)            # held time: every finished run, as before
        self.assertEqual(e['_measured']['runs'], 23)
        self.assertEqual(e['_measured']['cpu_samples'], 3)
        self.assertEqual(e['_measured']['cpu_skipped'], {'idle': 20})
        self.assertEqual(e['_measured']['peak_cpu_slots_p90'], 2.9)
        self.assertEqual(self.note(notes, 'engine-test'),
                         'engine-test: warn_min 30 -> 15; cpu medium -> 3 '
                         '(23 runs; cpu from 3, skipped: 20 %s)' % IDLE)

    def test_nothing_skipped_leaves_the_note_and_the_record_plain(self):
        new, notes = self.prop([line('engine-test', 60, 300, p) for p in (2.1, 2.5, 2.9)])
        self.assertEqual(self.note(notes, 'engine-test'),
                         'engine-test: warn_min 30 -> 2; cpu medium -> 3 (3 runs)')
        self.assertEqual(new['engine-test']['_measured']['cpu_samples'], 3)
        self.assertNotIn('cpu_skipped', new['engine-test']['_measured'])

    def test_too_few_cpu_samples_keep_the_entry_however_many_runs_finished(self):
        real = [line('engine-test', 60, 300, p) for p in (2.1, 2.9)]
        blind = [line('engine-test', 600, 0.5, 0.01) for _ in range(20)]
        new, notes = self.prop(real + blind)
        self.assertEqual(new['engine-test'], {'cpu': 2, 'gpu': 0, 'warn_min': 30})
        self.assertEqual(self.note(notes, 'engine-test'),
                         'engine-test: kept (2 cpu sample(s) of 22 finished run(s); 3 needed; '
                         'skipped: 20 %s); cpu medium -> 2 (its size on this layout)' % IDLE)

    def test_the_minimum_is_the_callers_min_runs(self):
        real = [line('engine-test', 60, 300, 2.9) for _ in range(4)]
        blind = [line('engine-test', 600, 0.5, 0.01) for _ in range(20)]
        new, notes = self.prop(real + blind, min_runs=5)
        self.assertNotIn('_measured', new['engine-test'])
        self.assertIn('kept (4 cpu sample(s) of 24 finished run(s); 5 needed;',
                      self.note(notes, 'engine-test'))

    def test_the_edges_of_the_rule(self):
        def sampled(held, cpu_s):
            hist = [line('engine-test', held, cpu_s, 0.5) for _ in range(3)]
            return '_measured' in self.prop(hist)[0]['engine-test']
        self.assertTrue(sampled(10, 1.0))              # ten seconds held, a tenth of a thread
        self.assertFalse(sampled(9, 9.0))              # too short, however busy
        self.assertTrue(sampled(100, 10.0))
        self.assertFalse(sampled(100, 9.9))
        # The edge is exact on the figures as written, not on their binary products.
        self.assertTrue(sampled(12, 1.2))
        self.assertFalse(sampled(12, 1.1))
        self.assertTrue(sampled(57, 5.7))
        self.assertTrue(sampled(10.3, 1.03))

    def test_a_peak_of_zero_is_not_a_figure(self):
        # A busy line whose peak was never measured must not size a kind to 1.
        hist = [line('engine-test', 60, 300, 0.0) for _ in range(3)]
        new, notes = self.prop(hist)
        self.assertEqual(new['engine-test'], {'cpu': 2, 'gpu': 0, 'warn_min': 30})
        self.assertIn('skipped: 3 %s)' % NO_FIGURES, self.note(notes, 'engine-test'))
        self.assertEqual(measure.skip_reason(line('engine-test', 60, 300, 0.0)), 'no_figures')
        self.assertIsNone(measure.skip_reason(line('engine-test', 60, 300, 0.01)))

    def test_a_line_without_a_held_time_is_short_not_an_error(self):
        for bad in (None, 'x', True, -1):
            d = dict(line('engine-test', 60, 300, 2.5), held_s=bad)
            self.assertEqual(measure.skip_reason(d), 'short', bad)
        d = line('engine-test', 60, 300, 2.5)
        del d['held_s']
        self.assertEqual(measure.skip_reason(d), 'short')

    def test_the_first_reason_wins_when_two_apply(self):
        why = measure.skip_reason
        self.assertEqual(why(line('k', 5, 300, None)), 'short')            # short and no figures
        self.assertEqual(why(line('k', 5, 0.1, 2.5)), 'short')             # short and idle
        self.assertEqual(why(line('k', 60, 0.5, None)), 'no_figures')      # no figures and idle
        self.assertEqual(why(line('k', 60, 0.5, 0.0)), 'no_figures')
        self.assertEqual(why(dict(line('k', 60, 0.5, 2.5), cpu_s=None)), 'no_figures')
        self.assertEqual(why(line('k', 5, 0.1, None, partial=True)), 'partial')

    def test_the_bookings_are_those_of_the_cpu_samples(self):
        # Skipped runs booked 2 slots, the samples 6: a peak of 1.9 fills a third of a sample's
        # booking, so the kind is its peak rounded up, not the cap of a filled booking.
        blind = [line('engine-test', 600, 0.5, 0.01, cores=2) for _ in range(3)]
        real = [line('engine-test', 60, 300, 1.9, cores=6) for _ in range(3)]
        self.assertEqual(self.prop(blind + real)[0]['engine-test']['cpu'], 2)

    def test_each_skipped_run_is_counted_once_under_its_first_reason(self):
        hist = ([line('cpu-eval', 1, 0.0, 0.0, partial=True) for _ in range(25)]
                + [line('cpu-eval', 1, 0.4, 0.0) for _ in range(40)]
                + [line('cpu-eval', 300, 0.5, 0.01) for _ in range(18)]
                + [line('cpu-eval', 60, 300, None)])
        new, notes = self.prop(hist)
        self.assertEqual(new['cpu-eval'], {'cpu': 6, 'gpu': 0, 'warn_min': 120})
        self.assertEqual(self.note(notes, 'cpu-eval'),
                         'cpu-eval: kept (0 cpu sample(s) of 84 finished run(s); 3 needed; '
                         'skipped: 25 %s, 40 %s, 1 %s, 18 %s); cpu heavy -> 6 '
                         '(its size on this layout)' % (PARTIAL, SHORT, NO_FIGURES, IDLE))

    def test_runs_that_work_in_the_job_are_sized_as_before(self):
        # A daily-run shape: long, mostly idle on average, full in its busiest window.
        hist = [line('cpu-eval', h, c, p) for h, c, p in (
            (2270, 5185.0, 5.94), (2298, 3593.1, 5.93), (2418, 3725.6, 5.94),
            (2146, 1462.4, 3.5), (93, 700.9, 4.76), (2419, 3164.4, 5.43))]
        new, notes = self.prop(hist)
        self.assertEqual(new['cpu-eval']['cpu'], 6)
        self.assertEqual(new['cpu-eval']['_measured']['cpu_samples'], 6)
        self.assertEqual(self.note(notes, 'cpu-eval'),
                         'cpu-eval: warn_min 120 -> 61; cpu heavy -> 6 (6 runs)')

    def test_the_rule_before_whole_core_sizes_skips_the_same_lines(self):
        hist = [line('cpu-eval', 1800, 0.7, 0.01) for _ in range(5)]
        new, notes = self.prop(hist, agent_cap=None)
        self.assertEqual(new['cpu-eval'], SIZED['cpu-eval'])
        self.assertEqual(self.note(notes, 'cpu-eval'),
                         'cpu-eval: kept (0 cpu sample(s) of 5 finished run(s); 3 needed; '
                         'skipped: 5 %s)' % IDLE)

    def test_a_wrapper_that_is_itself_busy_still_counts(self):
        # The limit of the rule: a wrapper that keeps a tenth of a thread busy while the work
        # runs outside the job cannot be told from a light kind, and is sized by its own peak.
        hist = [line('cpu-eval', 600, 90, 0.05) for _ in range(3)]
        self.assertEqual(self.prop(hist)[0]['cpu-eval']['cpu'], 1)


if __name__ == '__main__':
    unittest.main()
