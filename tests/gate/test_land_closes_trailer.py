"""A landing closes the records a branch commit names in a `Closes:` trailer, whoever pulled them:
a record pulled from the main checkout holds `pulled_by: main`,
which no landing matches. kitlib.backlog.at_landing.close_landed with the tip and base that
kitlib.gate.land.Lander.close_pulled passes, on a ref store in a scratch repository."""
import json
import os
import unittest
from unittest import mock

import kitlib.config as kconfig
from kitlib import gate as G
from kitlib import pmdata
from kitlib.backlog import at_landing, offer
from kitlib.backlog import store as smod
from kitlib.gate import land
from kitlib.git import refs

from tests.backlog.helpers import priced
from tests.backlog.test_pmdata_commit import commit_beside, index_move_fails
from tests.gate import test_land_store as tls
from tests.gate.test_land_store import BRANCH, StoreLandCase, pulled

TRAILERS = 'Co-Authored-By: Test Model <noreply@anthropic.com>'


class ClosesByTrailer(StoreLandCase):

    def branch_with(self, message, name='feature', path='a.py'):
        """kit/<name> with one commit, adding `path`, whose message is `message`: its tip."""
        wt = self.repo.worktree(name)
        self.repo.write(path, 'x = 1\n', root=wt)
        return self.repo.commit(message, path, cwd=wt)

    def land(self, tip):
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        return out

    def warnings(self, out):
        return [l for l in out.lines if l.startswith('WARNING')]

    def notes(self, out):
        # The Closes-trailer NOTEs; not the landing scope check's (tests/gate/test_land_scope.py).
        return [l for l in out.lines if l.startswith('NOTE')
                and 'its paths were not checked against a scope' not in l]

    def test_a_record_a_trailer_names_is_closed_though_main_pulled_it(self):
        self.seed(pulled('D2', by='main'), pulled('KEEP', by='main'), pulled('FOO'))
        out = self.land(self.branch_with('fix it\n\nCloses: D2\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['KEEP'])
        self.assertEqual(sorted(self.subjects()[:2]),
                         ['closes D2: landed %s' % out.merge, 'closes FOO: landed %s' % out.merge])
        closed = [l for l in out.lines if l.startswith('closed ')]
        self.assertEqual(len(closed), 2, out.lines)
        d2 = [l for l in closed if l.startswith('closed D2 ')]
        self.assertEqual(len(d2), 1, out.lines)
        self.assertIn('named by a Closes trailer of %s; it was pulled by main' % BRANCH, d2[0])
        self.assertEqual(self.warnings(out), [])

    def test_one_trailer_may_name_several_records(self):
        # BBB's holder is a live branch: closed, and its builder must be told. DDD's holder has
        # no branch left (a dead pull) and AAA's is main: closed with nothing to tell.
        self.repo.git('branch', 'kit/other', self.main)
        self.seed(pulled('AAA', by='main'), pulled('BBB', by='kit/other'),
                  pulled('DDD', by='kit/gone'), pulled('CCC', by='main'))
        out = self.land(self.branch_with('fix\n\nCloses: AAA, BBB (both done), DDD\n%s\n'
                                         % TRAILERS))
        self.assertEqual(self.stems(), ['CCC'])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('BBB', warn[0])
        self.assertIn('kit/other holds it pulled', warn[0])
        self.assertIn('tell the builder of kit/other', warn[0])
        self.assertEqual(self.notes(out), [])

    def test_a_live_holder_that_git_cannot_probe_is_warned_of_too(self):
        self.seed(pulled('BBB', by='kit/other'))
        tip = self.branch_with('fix\n\nCloses: BBB\n%s\n' % TRAILERS)
        with mock.patch.object(offer, 'live_branches', return_value=(None, 'probe failed')):
            out = self.land(tip)
        self.assertEqual(self.stems(), [])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('kit/other holds it pulled', warn[0])
        self.assertIn('probe failed', warn[0])

    def test_a_wrapped_trailer_value_is_read_unfolded(self):
        self.seed(pulled('AAA', by='main'))
        tip = self.branch_with('fix\n\nCloses: AAA (done\n  in two parts)\n%s\n' % TRAILERS)
        # folded, the continuation line would read as the id `in`
        self.assertEqual(at_landing.closes_trailers(self.repo.root, tip, self.main), (['AAA'], None))
        out = self.land(tip)
        self.assertEqual(self.stems(), [])
        self.assertEqual(self.notes(out), [])
        self.assertEqual(self.warnings(out), [])

    def test_an_owner_held_record_main_pulled_is_a_warning_and_stays(self):
        self.seed(pulled('HELD', by='main', priority='-1', priority_by='-',
                         owner_ask='may this ship?'))
        out = self.land(self.branch_with('fix\n\nCloses: HELD\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['HELD'])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('owner-held', warn[0])
        self.assertIn('which a Closes trailer of its commits names', warn[0])
        # the same command (and owner's flag) as a held record the branch pulled itself:
        # test_land_store.CloseEdges checks that flag
        self.assertIn('close HELD --reason "landed %s"' % out.merge, warn[0])

    def test_a_batch_reads_the_trailers_of_each_branch(self):
        self.seed(pulled('ONE', by='main'), pulled('TWO', by='main', scope=['b.py']),
                  pulled('KEEP', by='main'))
        tip_a = self.branch_with('a\n\nCloses: ONE\n%s\n' % TRAILERS)
        tip_b = self.branch_with('b\n\nCloses: TWO\n%s\n' % TRAILERS, name='other', path='b.py')
        out = self.lander([BRANCH, 'kit/other'], self.main, [tip_a, tip_b]).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.stems(), ['KEEP'])
        closed = sorted(l.split()[1] for l in out.lines if l.startswith('closed '))
        self.assertEqual(closed, ['ONE', 'TWO'], out.lines)
        self.assertTrue(any('named by a Closes trailer of kit/other' in l for l in out.lines),
                        out.lines)
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.notes(out), [])

    def test_a_stacked_branch_of_a_batch_does_not_read_the_closes_lines_merged_before_it(self):
        # kit/other starts at a1, kit/feature's `Closes: ONE` commit, and lands after it: its
        # messages are read down to kit/feature's merge, so ONE, closed already, is not read
        # again as an id no store holds (a NOTE).
        self.seed(pulled('ONE', by='main', scope=['a.py', 'a2.py']), pulled('KEEP', by='main'))
        wt = self.repo.worktree('feature')
        self.repo.write('a.py', 'x = 1\n', root=wt)
        a1 = self.repo.commit('a1\n\nCloses: ONE\n%s\n' % TRAILERS, 'a.py', cwd=wt)
        self.repo.write('a2.py', 'x = 2\n', root=wt)
        tip_a = self.repo.commit('a2\n\n%s\n' % TRAILERS, 'a2.py', cwd=wt)
        other = os.path.join(self.repo.root, '.claude', 'worktrees', 'other')
        self.repo.git('worktree', 'add', '-q', other, '-b', 'kit/other', a1)
        self.repo.write('b.py', 'y = 1\n', root=other)
        tip_b = self.repo.commit('b\n\n%s\n' % TRAILERS, 'b.py', cwd=other)
        out = self.lander([BRANCH, 'kit/other'], self.main, [tip_a, tip_b]).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.stems(), ['KEEP'])
        closed = [l for l in out.lines if l.startswith('closed ')]
        self.assertEqual(len(closed), 1, out.lines)
        self.assertIn('named by a Closes trailer of %s' % BRANCH, closed[0])
        self.assertEqual(self.notes(out), [])
        self.assertEqual(self.warnings(out), [])

    def test_a_holder_landing_in_the_same_batch_is_no_builder_to_tell(self):
        self.seed(pulled('TWO', by='kit/other', scope=['a.py', 'b.py']), pulled('KEEP', by='main'))
        tip_a = self.branch_with('a\n\nCloses: TWO\n%s\n' % TRAILERS)
        tip_b = self.branch_with('b\n\n%s\n' % TRAILERS, name='other', path='b.py')
        out = self.lander([BRANCH, 'kit/other'], self.main, [tip_a, tip_b]).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.stems(), ['KEEP'])
        self.assertTrue(any(l.startswith('closed TWO ') for l in out.lines), out.lines)
        self.assertEqual(self.warnings(out), [])

    def close_naming(self, tip):
        cfg = kconfig.load_at(self.repo.root, self.main)
        return at_landing.close_landed(self.repo.root, cfg, BRANCH, 'f' * 40, tip=tip,
                                       base=self.main)

    def test_an_unknown_id_is_a_note_only_when_every_store_was_read(self):
        self.seed(pulled('KEEP', by='main'))
        tip = self.branch_with('fix\n\nCloses: NOPE\n%s\n' % TRAILERS)
        read = self.close_naming(tip)
        self.assertEqual(len(read), 1, read)
        self.assertTrue(read[0].startswith('NOTE'), read)
        self.assertIn('NOPE', read[0])
        # The store could not be read, so NOPE may be in it: no NOTE, the WARNING says so.
        with mock.patch.object(refs, 'ls_remote', return_value=(None, 'boom')):
            unread = self.close_naming(tip)
        self.assertEqual(len(unread), 1, unread)
        self.assertTrue(unread[0].startswith('WARNING: %s landed' % BRANCH), unread)
        self.assertIn('boom', unread[0])
        self.assertEqual(self.stems(), ['KEEP'])

    def test_an_unknown_id_is_no_note_when_one_store_was_read_and_another_was_not(self):
        # The ref store is read (seen.read is 1), and the PM data store cannot be opened: a PM
        # data record names a branch no worktree has checked out. NOPE may be in that store.
        self.seed(pulled('KEEP', by='main'))
        tree = self.repo.git('rev-parse', self.main + '^{tree}')
        root = self.repo.git('commit-tree', tree, '-m', 'PM data root')
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', root)
        lines = self.close_naming(self.branch_with('fix\n\nCloses: NOPE\n%s\n' % TRAILERS))
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('WARNING: %s landed, and the PM data branch'
                                            % BRANCH), lines)
        self.assertIn('no worktree has the PM data branch pmdata checked out', lines[0])
        self.assertEqual(self.stems(), ['KEEP'])

    def test_a_trailer_naming_a_record_that_is_not_pulled_is_a_warning_with_the_command(self):
        self.seed(priced('OPEN'))
        out = self.land(self.branch_with('fix\n\nCloses: OPEN\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['OPEN'])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('names OPEN', warn[0])
        self.assertIn('not pulled', warn[0])
        self.assertIn('close OPEN --reason "landed %s"' % out.merge, warn[0])

    def test_ids_no_store_holds_are_one_note_line(self):
        # A typo, or a case that differs from the record's file name (the store's lookup key).
        before = self.seed(priced('BAZ'), pulled('D2', by='main'))
        out = self.land(self.branch_with('fix\n\nCloses: NOPE, d2\nCloses: D2\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['BAZ'])
        self.assertNotEqual(self.remote_store(), before)
        self.assertEqual(self.warnings(out), [])
        notes = self.notes(out)
        self.assertEqual(len(notes), 1, out.lines)
        self.assertIn('NOPE, d2', notes[0])
        self.assertIn('no backlog store', notes[0])

    def test_a_closes_line_above_a_separate_attribution_paragraph_closes(self):
        # tools/commit.py's shape: the attribution is its own last paragraph, so the Closes line
        # is not in git's trailer block; it still closes the record.
        self.seed(pulled('D2', by='main'), pulled('KEEP', by='main'))
        out = self.land(self.branch_with('fix\n\nCloses: D2\n\n%s\nClaude-Session: test-session\n'
                                         % TRAILERS))
        self.assertEqual(self.stems(), ['KEEP'])
        self.assertTrue(any(l.startswith('closed D2 ') for l in out.lines), out.lines)
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.notes(out), [])

    def test_prose_that_mentions_closes_closes_nothing(self):
        before = self.seed(pulled('D2', by='main'), pulled('D3', by='main'),
                           pulled('D4', by='main'), pulled('D5', by='main'))
        out = self.land(self.branch_with(
            'fix\n\nThis closes D2 at last, and the old code said Closes: D3 too.\n'
            'closes: D4\n  Closes: D5\n\n%s\n' % TRAILERS))
        self.assertEqual(self.remote_store(), before)
        self.assertEqual(self.stems(), ['D2', 'D3', 'D4', 'D5'])
        self.assertEqual(self.warnings(out), [])
        self.assertEqual(self.notes(out), [])

    def test_a_trailer_already_on_main_is_not_read(self):
        self.seed(pulled('OLD', by='kit/other'))
        self.repo.write('b.py', 'y = 1\n')
        self.main = self.repo.commit('earlier\n\nCloses: OLD\n%s\n' % TRAILERS, 'b.py')
        self.repo.git('push', '-q', 'origin', 'main')
        out = self.land(self.branch_with('fix\n\n%s\n' % TRAILERS))
        self.assertEqual(self.stems(), ['OLD'])
        self.assertEqual(self.warnings(out), [])

    def test_unreadable_trailers_are_a_warning_and_the_branchs_own_pulls_still_close(self):
        self.seed(pulled('FOO'), pulled('D2', by='main'))
        tip = self.branch_with('fix\n\nCloses: D2\n%s\n' % TRAILERS)
        with mock.patch.object(at_landing, 'closes_trailers', return_value=(None, 'boom')):
            out = self.land(tip)
        self.assertEqual(self.stems(), ['D2'])
        warn = self.warnings(out)
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('Closes trailers', warn[0])
        self.assertIn('boom', warn[0])
        # whole messages: git's trailer parser misses a Closes line above commit.py's attribution
        self.assertIn('git log --format=%%B %s --not ' % tip, warn[0])


class ClosesByTrailerInThePmDataStore(StoreLandCase):
    """The PM data branch's folder store (test_land_store.PmDataClose's set-up) takes the
    trailer's ids too, while the configured store has none of them: no NOTE."""
    setUp = tls.PmDataClose.setUp
    pm_worktree = tls.PmDataClose.pm_worktree
    record = tls.PmDataClose.record
    pm_root = tls.PmDataClose.pm_root
    branch_with = ClosesByTrailer.branch_with

    def test_a_record_a_trailer_names_is_closed_in_the_pm_data_store(self):
        wt = self.seeded(pulled('D2', by='main'), pulled('KEEP', by='main'))
        tip = self.branch_with('fix\n\nCloses: D2\n%s\n' % TRAILERS)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse(os.path.exists(self.record(wt, 'D2')), out.lines)
        self.assertTrue(os.path.exists(self.record(wt, 'KEEP')))
        closed = [l for l in out.lines if l.startswith('closed ')]
        self.assertEqual(len(closed), 1, out.lines)
        self.assertIn('closed D2 in the backlog store kit/backlog in ', closed[0])
        self.assertIn('named by a Closes trailer of %s; it was pulled by main' % BRANCH, closed[0])
        self.assertFalse([l for l in out.lines if l.startswith(('WARNING', 'NOTE'))], out.lines)

    # A close in this store is committed on the PM data branch (store.MainBackend._commit), and
    # each way that commit ends has its own WARNING: they hold for a record a trailer names too.

    def seeded(self, *records):
        """The PM data worktree with `records` committed on its branch."""
        wt = self.pm_worktree(*records)
        self.repo.commit('seed the PM data store', 'kit/backlog', '.kit.json', cwd=wt)
        return wt

    def land_closing(self, rid):
        tip = self.branch_with('fix\n\nCloses: %s\n%s\n' % (rid, TRAILERS))
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        return out, [l for l in out.lines if l.startswith('WARNING')]

    def locked(self, path):
        with open(path, 'w') as f:
            f.write('')
        self.addCleanup(os.remove, path)

    def another_commit(self, wt):
        """Another session's commit of an added line in D2.md, made without the index lock the
        close holds (commit_beside; a `git commit` there would fail), so the index is left as it
        was: the mover's own repair, run here once the landing is over, resets it."""
        with open(self.record(wt, 'D2'), encoding='utf-8') as f:
            text = f.read() + '\nanother session wrote this\n'
        self.addCleanup(self.repo.git, 'reset', '-q', '--', 'kit/backlog/D2.md', cwd=wt)
        return commit_beside(wt, {'kit/backlog/D2.md': text}, 'another session')

    def test_an_overtaken_close_of_a_record_a_trailer_names_gives_the_close_command(self):
        """D2 is pulled by main and another commit changes its file under the close: the files
        are put back, D2 is still pulled, and the trailer closes it whoever holds it, so the
        WARNING names the holder and the close command; it never says D2 is `no longer pulled
        by` the landed branch, which never held it."""
        wt = self.seeded(pulled('D2', by='main'))
        real, theirs = smod.MainBackend._write_files, []

        def another_commit_then_write(backend, writes, before, paths, branch):
            if branch and not theirs:
                theirs.append(self.another_commit(wt))
            real(backend, writes, before, paths, branch)
        with mock.patch.object(smod.MainBackend, '_write_files', another_commit_then_write):
            out, warn = self.land_closing('D2')
        self.assertEqual(len(theirs), 1)
        self.assertTrue(os.path.exists(self.record(wt, 'D2')))
        self.assertEqual(len(warn), 1, out.lines)
        self.assertIn('was refused and its files were put back (ERROR: ', warn[0])
        self.assertIn('D2 is still pulled by main and was not closed; close it with: ', warn[0])
        self.assertTrue(warn[0].endswith(' close D2 --reason "landed %s"' % out.merge), warn[0])
        self.assertNotIn('no longer pulled by', warn[0])

    def test_the_code_repositorys_main_needs_no_telling_when_the_pm_data_main_differs(self):
        """The PM data worktree's own configured main is its data branch; a record the code
        repository's main checkout pulled (pulled_by: main) is still closed with no holder to
        tell."""
        wt = self.pm_worktree(pulled('D2', by='main'), pulled('KEEP', by='main'))
        with open(os.path.join(wt, '.kit.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        cfg.setdefault('repo', {})['main_branch'] = 'pmdata'
        self.repo.write('.kit.json', json.dumps(cfg, indent=1) + '\n', root=wt)
        self.assertEqual(kconfig.load(wt).main_branch, 'pmdata')
        self.repo.commit('seed the PM data store', 'kit/backlog', '.kit.json', cwd=wt)
        out, warn = self.land_closing('D2')
        self.assertFalse(os.path.exists(self.record(wt, 'D2')), out.lines)
        self.assertTrue(any(l.startswith('closed D2 ') for l in out.lines), out.lines)
        self.assertEqual(warn, [])

    def test_an_overtaken_close_that_left_a_file_gives_the_compare_not_the_close(self):
        """D2, which main pulled and the trailer names: another commit changes its file under the
        close, and once the close deleted it a hand writes it again, so the put-back leaves it
        (Overtaken.left): the WARNING gives the compare with HEAD and no close command."""
        wt = self.seeded(pulled('D2', by='main'))
        real, theirs = smod.MainBackend._write_files, []

        def another_commit_then_write(backend, writes, before, paths, branch):
            first = branch and not theirs
            if first:
                theirs.append(self.another_commit(wt))
            real(backend, writes, before, paths, branch)
            if first:
                with open(self.record(wt, 'D2'), 'w', encoding='utf-8', newline='\n') as f:
                    f.write('written by hand\n')
        with mock.patch.object(smod.MainBackend, '_write_files', another_commit_then_write):
            out, warn = self.land_closing('D2')
        self.assertEqual(len(theirs), 1)
        with open(self.record(wt, 'D2'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'written by hand\n')
        self.assertEqual(len(warn), 1, out.lines)
        self.assertTrue(warn[0].endswith(
            '; do not commit it over that commit: compare each path left with HEAD first: '
            'git -C "%s" diff HEAD -- "kit/backlog/D2.md"' % self.pm_root(wt)), warn[0])
        self.assertNotIn('close it with', warn[0])

    def test_a_trailer_close_that_was_not_committed_still_tells_the_live_holder(self):
        self.repo.git('branch', 'kit/other', self.main)
        wt = self.seeded(pulled('BBB', by='kit/other'))
        self.locked(os.path.join(self.repo.common_dir(), 'refs', 'heads', 'pmdata.lock'))
        with mock.patch.object(smod, 'ATTEMPTS', 1):
            out, warn = self.land_closing('BBB')
        self.assertFalse(os.path.exists(self.record(wt, 'BBB')))
        self.assertEqual(len(warn), 2, out.lines)
        self.assertIn('BBB was closed in the backlog store kit/backlog in ', warn[0])
        self.assertIn('but not committed', warn[0])
        self.assertIn('its Closes trailer closed BBB while kit/other holds it pulled', warn[1])
        self.assertIn('tell the builder of kit/other', warn[1])

    def test_a_trailer_close_committed_with_a_stale_index_still_tells_the_live_holder(self):
        self.repo.git('branch', 'kit/other', self.main)
        wt = self.seeded(pulled('BBB', by='kit/other'))
        with index_move_fails(tls.PmDataClose.pm_index(self, wt)):
            out, warn = self.land_closing('BBB')
        self.assertEqual(len(warn), 2, out.lines)
        self.assertIn('BBB was closed in the backlog store kit/backlog in ', warn[0])
        self.assertIn('repair the index with: ', warn[0])
        self.assertIn('its Closes trailer closed BBB while kit/other holds it pulled', warn[1])
        self.assertIn('tell the builder of kit/other', warn[1])


class TrailerValues(unittest.TestCase):

    def test_ids_are_the_first_word_of_each_comma_separated_value(self):
        self.assertEqual(at_landing.closes_ids(['EX-43', 'A, B (both)', 'kit.x_y', '#12, (C)',
                                                '  ', 'E done']),
                         ['EX-43', 'A', 'B', 'kit.x_y', 'E'])

    def test_closes_lines_are_read_anywhere_in_the_message_and_unfolded(self):
        message = ('subject Closes: NO1\n\nCloses: EX-1\n\tand more\nprose closes: NO2\n'
                   'closes: NO3\n  Closes: NO4\nCLOSES: NO5\nCloses:EX-2\n\n'
                   'Co-Authored-By: someone\n')
        self.assertEqual(at_landing.closes_values(message), ['EX-1 and more', 'EX-2'])
        self.assertEqual(at_landing.closes_values(''), [])



class BatchBase(unittest.TestCase):
    """Lander.close_pulled: the base each batch branch's Closes and Part-of lines are read down
    to, and the WARNING a batch not merged as a stack gets (KIT-LAND-BATCH-PARTOF-BASE)."""

    def close(self, branches, merges):
        tips = [c * 40 for c in 'abc'[:len(branches)]]
        lander = land.Lander('.', branches, 'm' * 40, tips)
        lander.expect_main, lander.merges = 'm' * 40, list(merges)
        lander.merge = merges[-1] if merges else None
        with mock.patch.object(at_landing, 'close_landed', return_value=['closed']) as cl:
            lines = lander.close_pulled()
        return lines, [c.kwargs['base'] for c in cl.call_args_list]

    def test_a_stacked_batch_reads_down_to_the_previous_merge_and_warns_nothing(self):
        lines, bases = self.close(['kit/one', 'kit/two'], ['1' * 40, '2' * 40])
        self.assertEqual(bases, ['m' * 40, '1' * 40])
        self.assertEqual(lines, ['closed', 'closed'])

    def test_one_branch_warns_nothing(self):
        lines, bases = self.close(['kit/one'], ['1' * 40])
        self.assertEqual((lines, bases), (['closed'], ['m' * 40]))

    def test_a_batch_not_merged_as_a_stack_warns_for_each_branch_after_the_first(self):
        lines, bases = self.close(['kit/one', 'kit/two', 'kit/three'], ['1' * 40])
        self.assertEqual(bases, ['m' * 40] * 3)             # unchanged: the base for each
        warn = [l for l in lines if l.startswith('WARNING')]
        self.assertEqual(len(warn), 2, lines)
        self.assertEqual(lines[0], 'closed')                # the first branch: no warning
        for w, b, t in zip(warn, ['kit/two', 'kit/three'], ['b' * 40, 'c' * 40]):
            self.assertIn('not merged as a stack (1 merge(s) for 3 branches)', w)
            self.assertIn("so %s's Closes and Part-of lines" % b, w)
            self.assertTrue(w.endswith('git log --format=%%B %s --not %s' % (t, 'm' * 40)), w)
        self.assertEqual(lines[1:3], [warn[0], 'closed'])   # each before its branch's lines


if __name__ == '__main__':
    unittest.main()
