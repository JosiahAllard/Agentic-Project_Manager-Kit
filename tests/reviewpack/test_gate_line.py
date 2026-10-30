"""tools/review_pack.py's `gate` line (docs/review-pack.md): whether the branch tip has a full-gate
OK record for exactly that commit, the record a landing requires (docs/gate.md "Landing"). It is
printed only: the exit status is what the rest of the pack makes it."""
from kitlib.records import gate_ok
from tests.reviewpack.repo import AGENT, EMAIL, PackCase


class GateLineCase(PackCase):
    def feature(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('f.txt', '1')
        self.commit_all('change f')
        return self.git('rev-parse', 'feature')

    def record(self, tip, **keys):
        rec = gate_ok.build(self.root, tip, {}, {'version': 'test'}, 'test', gate_ok.utc(),
                            [{'name': 'unit'}])
        rec.update(keys)
        gate_ok.write(self.root, rec)
        return rec

    def gate_line(self, out):
        got = [l for l in out.splitlines() if l.startswith('gate     ')]
        self.assertEqual(len(got), 1, out)
        return got[0]


class GateLine(GateLineCase):
    def test_no_record_is_said_and_the_exit_status_is_unchanged(self):
        tip = self.feature()
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        line = self.gate_line(out)
        self.assertIn('tip %s: NO full-gate OK record' % tip[:10], line)
        self.assertIn('no gate-OK record for %s' % tip, line)
        self.assertIn('landing will refuse', line)

    def test_a_default_mode_record_is_not_enough(self):
        tip = self.feature()
        self.record(tip, full=False)
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('without --full', self.gate_line(out))

    def test_a_full_record_of_the_tip_is_said(self):
        tip = self.feature()
        rec = self.record(tip, full=True)
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.gate_line(out), 'gate     tip %s: full gate OK, ended %s'
                         % (tip[:10], rec['ended_utc']))

    def test_a_pack_that_fails_still_fails_with_a_full_record(self):
        self.git('checkout', '-q', '-b', 'feature')
        self.write('CLAUDE.md', 'v1')
        self.commit_all('agent edits claude, no trailer', author=AGENT)
        tip = self.git('rev-parse', 'feature')
        self.record(tip, full=True)
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 1, out)
        self.assertIn('full gate OK', self.gate_line(out))


class GateOnceLine(GateLineCase):
    """A base that sets "landing".gate_once takes a default-mode record (docs/gate.md "Gate once
    at landing"): the line says so instead of "landing will refuse"."""
    CONFIG = {'owner': {'email': EMAIL}, 'landing': {'gate_once': True}}

    def test_a_default_mode_record_is_said_as_enough(self):
        tip = self.feature()
        rec = self.record(tip, full=False, mode='select')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        line = self.gate_line(out)
        self.assertTrue(line.startswith('gate     tip %s: scoped gate OK (the base sets '
                                        '"landing".gate_once), ended %s'
                                        % (tip[:10], rec['ended_utc'])), line)
        self.assertNotIn('landing will refuse', line)

    def test_a_docs_record_is_still_not_enough(self):
        tip = self.feature()
        self.record(tip, full=False, mode='docs')
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertIn('landing will refuse', self.gate_line(out))

    def test_a_full_record_is_said_as_before(self):
        tip = self.feature()
        rec = self.record(tip, full=True)
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.gate_line(out), 'gate     tip %s: full gate OK, ended %s'
                         % (tip[:10], rec['ended_utc']))
