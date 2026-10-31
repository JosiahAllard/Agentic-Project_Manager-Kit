"""A plain branch gate says when its tip must end on --full (docs/gate.md "The final gate";
runner.full_hint, land.full_needed): a FULL NEEDED line with the reason the landing's tip check
(Lander.diff_refusal, select_refusal's own checks of the landing diff) would refuse its record
for, or a changed strict path; nothing extra when the record lands. --full and --only add no line,
--plan says it, and a hint that cannot be told is a NOTE that never fails the gate."""
import json

from kitlib import gate as G
from kitlib.gate import runner
from tests.gate import test_land, test_land_tip_gated as tg

BRANCH = tg.BRANCH
NEEDED = 'FULL NEEDED: '


def hints(out):
    return [l for l in out.lines if l.startswith(NEEDED)]


class HintCase(tg.ScopedCase):
    def plain(self, files):
        """The branch with `files` gated in the default mode: (worktree, tip, outcome)."""
        wt, tip = self.branch('feature', files)
        return wt, tip, self.gate(wt)

    def needs(self, out, *says):
        found = hints(out)
        self.assertEqual(len(found), 1, out.lines)
        for s in says:
            self.assertIn(s, found[0])
        self.assertIn('--full (docs/gate.md "The final gate")', found[0])
        self.assertEqual(out.facts['full_hint'], found[0])
        # just before the source line (detect.label), which stays last
        self.assertEqual(out.lines.index(found[0]), len(out.lines) - 2, out.lines)

    def quiet(self, out):
        self.assertEqual(hints(out), [], out.lines)
        self.assertFalse([l for l in out.lines if 'needs a final --full' in l], out.lines)
        self.assertIsNone(out.facts['full_hint'])

    def lands(self, tip):
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)

    def config_change(self):
        wt = self.repo.worktree('feature')
        steps = self.GATE['steps'] + [tg._step('lint')]
        kit = dict(self.KIT, gate=dict(self.GATE, steps=steps))
        self.repo.write('.kit.json', json.dumps(kit, indent=1), root=wt)
        self.repo.git('add', '.kit.json', cwd=wt)
        self.repo.git('-c', 'user.email=%s' % self.OWNER, 'commit', '-q', '-m', 'gate', cwd=wt)
        return wt, self.repo.git('rev-parse', 'HEAD', cwd=wt)


class Shared(HintCase):
    """"landing".shared_paths: the hint and the landing's refusal agree."""

    def test_a_shared_path_needs_full_and_its_record_is_refused(self):
        wt, tip, out = self.plain({'api/x.py': 'x = 1\n'})
        self.needs(out, 'the tip changes shared path api/x.py ("landing".shared_paths)')
        self.refused(wt, tip, 'the tip changes shared path api/x.py')

    def test_a_strict_path_needs_full(self):
        wt, tip, out = self.plain({tg.STRICT_FILE: 'x = 1\n'})
        self.needs(out, 'the tip changes strict path %s' % tg.STRICT_FILE)
        self.refused(wt, tip, 'the tip changes strict path %s' % tg.STRICT_FILE)

    def test_a_gate_config_change_needs_full(self):
        wt, tip = self.config_change()
        out = self.gate(wt)
        self.needs(out, "the tip changes the gate's config (.kit.json)")
        self.refused(wt, tip, "the tip changes the gate's config (.kit.json)")

    def test_a_plain_path_says_nothing_extra_and_lands(self):
        _, tip, out = self.plain({'a.py': 'x = 1\n'})
        self.quiet(out)
        self.lands(tip)

    def test_full_and_only_add_no_line(self):
        wt, _ = self.branch('feature', {'api/x.py': 'x = 1\n'})
        self.quiet(self.gate(wt, full=True))
        self.quiet(self.gate(wt, only=['unit']))

    def test_plan_says_it(self):
        wt, _ = self.branch('feature', {'api/x.py': 'x = 1\n'})
        out = runner.branch_gate(wt, plan=True)
        self.assertEqual(out.code, G.OK, out.lines)
        self.needs(out, 'shared path api/x.py')


class Once(HintCase):
    """"landing".gate_once: a shared path lands on the scoped gate; a strict one needs --full."""
    KIT = dict(tg.ScopedCase.KIT, landing={'shared_paths': tg.SHARED, 'gate_once': True})

    def test_a_shared_path_says_nothing_extra_and_lands(self):
        _, tip, out = self.plain({'api/x.py': 'x = 1\n'})
        self.quiet(out)
        self.lands(tip)

    def test_a_strict_path_needs_full(self):
        wt, tip, out = self.plain({tg.STRICT_FILE: 'x = 1\n'})
        self.needs(out, 'strict path %s' % tg.STRICT_FILE, '"landing".gate_once')
        self.refused(wt, tip, 'strict path %s' % tg.STRICT_FILE)


class NothingSet(HintCase):
    """A base that takes no record that is not --full: every plain gate says so."""
    KIT = dict(test_land.LandCase.KIT)

    def test_any_change_needs_full(self):
        wt, tip, out = self.plain({'a.py': 'x = 1\n'})
        self.needs(out, 'sets no "gate".smoke_steps or "gate".docs_only, so only a --full record')
        self.refused(wt, tip, 'sets no "gate".smoke_steps')


class SmokeOnly(HintCase):
    """"gate".smoke_steps and "gate".docs_only, with no "landing" setting: the record check takes
    a strict tip's smoke record, and the procedure still ends a strict branch on --full."""
    GATE = tg.SELECT_GATE
    KIT = dict(test_land.LandCase.KIT, owner={'email': tg.ScopedCase.OWNER})

    def test_a_strict_path_needs_full_by_the_procedure(self):
        _, _, out = self.plain({tg.STRICT_FILE: 'x = 1\n'})
        self.needs(out, 'the tip changes strict path %s, and a strict branch ends on --full'
                   % tg.STRICT_FILE)

    def test_a_plain_path_says_nothing_extra_and_lands(self):
        _, tip, out = self.plain({'a.py': 'x = 1\n'})
        self.quiet(out)
        self.lands(tip)

    def test_a_docs_only_change_says_nothing_extra_and_lands(self):
        _, tip, out = self.plain({'docs/guide.md': 'words\n'})
        self.quiet(out)
        self.lands(tip)

    def test_a_gate_config_change_needs_full(self):
        wt, tip = self.config_change()
        out = self.gate(wt)
        self.needs(out, "the tip changes the gate's config (.kit.json)")
        self.refused(wt, tip, "the tip changes the gate's config (.kit.json)")

    def test_a_strict_list_that_cannot_be_read_is_a_note_and_the_gate_passes(self):
        self.repo.write('kitlib/gate/strict_defaults.py', 'STRICT_DEFAULTS = list()\n')
        self.main = self.repo.commit('unreadable strict list', 'kitlib/gate/strict_defaults.py')
        self.repo.git('push', '-q', 'origin', 'main')
        _, _, out = self.plain({'a.py': 'x = 1\n'})      # self.gate: the gate is OK
        self.assertEqual(hints(out), [], out.lines)
        note = [l for l in out.lines if l.startswith('NOTE: cannot tell whether this tip needs')]
        self.assertEqual(len(note), 1, out.lines)
        self.assertIn('cannot read the strict paths from main at %s' % self.main[:10], note[0])


class DocsOnlyBase(HintCase):
    """A base with "gate".docs_only alone: a change that is not docs-only needs --full. This pins
    select_refusal's check `not docs-only, and no smoke_steps` (moved to diff_refusal), which no
    other landing test reaches."""
    GATE = {'docs_only': {'globs': ['*.md'], 'steps': ['scrub']},
            'steps': [tg._step('unit'), tg._step('scrub', always=False, globs=['*.md'])]}
    KIT = dict(test_land.LandCase.KIT, owner={'email': tg.ScopedCase.OWNER})

    def test_a_change_that_is_not_docs_only_needs_full_and_its_record_is_refused(self):
        wt, tip, out = self.plain({'a.py': 'x = 1\n'})
        says = 'the change is not docs-only, and the base %s sets no "gate".smoke_steps' % (
            self.main[:10])
        self.needs(out, says)
        self.refused(wt, tip, 'without --full', says)

    def test_a_docs_only_change_says_nothing_extra_and_lands(self):
        _, tip, out = self.plain({'docs/guide.md': 'words\n'})
        self.quiet(out)
        self.lands(tip)
