"""A record's title and body (KIT-RECORD-TITLES) and the retired `priority: -1`
(KIT-BACKLOG-RETIRE-PRIORITY-MINUS-ONE), docs/backlog.md "Fields" and "Commands".

`add` keeps the summary to the title: `--body-file` writes the body at add time, and where the
finding was made and why it was not folded go into a `## Found` body section, not the summary. The
CLI refuses a summary over plan.SUMMARY_MAX; the Action Hub's add (no `summary_max`) does not.
`priority: -1` no longer marks a record awaiting the owner: `blocked_on: owner` is the only owner
hold, and a -1 is refused, naming `block --on owner`."""
import os
import tempfile
import unittest

from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog.errors import Refusal, Usage
from tests.backlog import test_cli
from tests.backlog.helpers import ADD_FLAGS, ADD_NONE, ADD_WHY, StoreCase, priced, rec


def body_file(test, text):
    fd, path = tempfile.mkstemp(suffix='.md')
    with os.fdopen(fd, 'w', encoding='utf-8', newline='') as fh:     # the bytes as given
        fh.write(text)
    test.addCleanup(os.remove, path)
    return path


class AddBody(StoreCase):
    remote = False

    def test_the_body_opens_the_record_and_the_finding_is_its_own_section(self):
        change = self.verb('add', 'NEW', 'a short title', ['docs/y.md'],
                           body='## What\n\nThe whole story, with the detail.\n', **ADD_NONE)
        new = self.get('NEW')
        self.assertEqual(new.get('summary'), 'a short title')
        self.assertTrue(new.body.startswith('## What\n\nThe whole story, with the detail.\n\n## Found\n'),
                        new.body)
        self.assertIn('Found on no branch (or on a branch since landed); not folded: %s' % ADD_WHY,
                      new.body)
        self.assertLess(new.body.index('## Found'), new.body.index('## Why this is here'))
        self.assertIn('  body:       your `--body-file`, `## Found`, `## Why this is here`',
                      change.lines)

    def test_with_no_body_the_record_still_says_where_it_was_found(self):
        self.verb('add', 'NEW', 'a short title', ['docs/y.md'], **ADD_NONE)
        new = self.get('NEW')
        self.assertEqual(new.get('summary'), 'a short title')
        self.assertTrue(new.body.startswith('## Found\n\nFound on no branch'), new.body)

    def test_a_summary_over_the_cap_is_refused_only_when_a_cap_is_given(self):
        long = 'x' * (pmod.SUMMARY_MAX + 1)
        with self.assertRaises(Usage) as cm:
            self.verb('add', 'NEW', long, ['docs/y.md'], summary_max=pmod.SUMMARY_MAX, **ADD_NONE)
        self.assertIn('--body-file', str(cm.exception))
        self.assertIn('%d characters' % len(long), str(cm.exception))
        self.assertIsNone(self.get('NEW'))
        self.verb('add', 'NEW', 'y' * pmod.SUMMARY_MAX, ['docs/y.md'],
                  summary_max=pmod.SUMMARY_MAX, **ADD_NONE)
        self.verb('add', 'HUB', long, ['docs/y.md'], **ADD_NONE)       # the Action Hub's add
        self.assertEqual(self.get('HUB').get('summary'), long)


class BodyFile(unittest.TestCase):
    """plan.read_body_file, which `add --body-file` reads through."""

    def test_an_empty_body_file_is_usage(self):
        for text in ('', '  \r\n\n', '﻿\n'):
            path = body_file(self, text)
            with self.assertRaises(Usage) as cm:
                pmod.read_body_file(path)
            self.assertIn('is empty', str(cm.exception))

    def test_a_missing_body_file_is_usage(self):
        path = os.path.join(tempfile.mkdtemp(), 'nope.md')
        self.addCleanup(os.rmdir, os.path.dirname(path))
        with self.assertRaises(Usage) as cm:
            pmod.read_body_file(path)
        self.assertIn('cannot read `--body-file', str(cm.exception))

    def test_a_body_file_drops_its_bom_and_crlf(self):
        self.assertEqual(pmod.read_body_file(body_file(self, '﻿one\r\ntwo\r\n')),
                         'one\ntwo\n')


class RetiredPriority(StoreCase):
    remote = False

    def test_the_schema_refuses_minus_one_naming_the_owner_block(self):
        why = rmod._priority('-1')
        self.assertIn('retired', why)
        self.assertIn('--on owner', why)
        for ok in ('1', '5', '-'):
            self.assertIsNone(rmod._priority(ok), ok)
        self.assertFalse(hasattr(rmod, 'OWNER_PRIORITY'))

    def test_set_minus_one_is_refused_naming_block_on_owner(self):
        self.seed(priced('FOO', priority='3', priority_by='agent'))
        with self.assertRaises(Usage) as cm:
            self.verb('set', 'FOO', [('priority', ['-1']), ('priority_by', ['owner'])],
                      role_checked=True)
        self.assertIn('block FOO --on owner', str(cm.exception))
        self.assertEqual(self.get('FOO').get('priority'), '3')

    def test_a_leftover_minus_one_is_refused_as_a_retired_hold_not_an_unpriced_record(self):
        # pricing it would lift the owner's hold by mistake, so the refusal names the block
        self.seed(rec('MIN', priority='-1', priority_by='-'))
        for kw in ({}, {'reason': 'the owner said go', 'may_collide': True}):
            with self.assertRaises(Refusal) as cm:
                self.verb('pull', 'MIN', **kw)
            msg = str(cm.exception)
            self.assertIn('retired owner hold', msg)
            self.assertIn('block MIN --on owner', msg)
            self.assertNotIn('Price it', msg)
            self.assertEqual((self.get('MIN').get('state'), self.get('MIN').get('priority')),
                             ('open', '-1'))

    def test_the_owner_hold_is_blocked_on_owner(self):
        self.seed(rec('FOO', state='blocked', blocked_on='owner', owner_ask='which one?'))
        held = self.get('FOO')
        self.assertTrue(rmod.is_owner_held(held))
        self.assertEqual(rmod.owner_hold_label(held), 'blocked_on: owner')


class Cli(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def test_add_takes_a_body_file(self):
        path = body_file(self, '﻿The detail the title leaves out.\r\n')
        self.ok('add', 'FOO', '--summary', 'a short title', '--scope', 'tools/x.py',
                '--body-file', path, *ADD_FLAGS)
        out = self.ok('show', 'FOO')
        self.assertIn('The detail the title leaves out.', out)
        self.assertIn('summary: a short title', out)
        self.assertNotIn('summary: a short title (found on', out)

    def test_add_refuses_a_long_summary_and_names_the_body_file(self):
        rc, out = self.run_tool('add', 'FOO', '--summary', 'x' * 121, '--scope', 'tools/x.py',
                                *ADD_FLAGS)
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE:', out)
        self.assertIn('--body-file', out)

    def test_set_minus_one_is_a_usage_error_naming_block_on_owner(self):
        self.ok('add', 'FOO', '--summary', 'a short title', '--scope', 'tools/x.py', *ADD_FLAGS)
        rc, out = self.run_tool('set', 'FOO', '--priority', '-1', '--priority-by', 'owner')
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE:', out)
        self.assertIn('block FOO --on owner', out)


if __name__ == '__main__':
    unittest.main()
