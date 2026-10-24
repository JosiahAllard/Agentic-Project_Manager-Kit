"""The built-in scrub (kitlib/scrub) over every file `git ls-files` lists, tests/ included. It
needs no machine config, so a clean clone checks it too; when the machine config sets
`machine_path_folders`, a drive path into one of them is a `drive` hit as well
(configured_folders, read as tests/shipped.py reads the install's names).

A hit that no ALLOWED entry covers counts against its (class, file) baseline in the PENDING_*
blocks below. Each block is today's hits that one later piece of work removes; that work lowers
its own block, and the block is done when it is empty. More hits than the baseline fails (a new
file with hits has baseline 0), so owner-specific text cannot come back meanwhile. Fewer hits
prints "lower the baseline" and does not fail, so a branch that removes a hit in passing stays
green. An ALLOWED entry that covers nothing is printed as stale.

The fixtures in TheChecks are built from pieces, so this file holds none of the shapes itself.
"""
import unittest

from kitlib import forbidden, scrub
from tests.scrub.test_scrub import FIXTURE_FOLDERS, read_text, tracked_files

# (path glob, class, text the line contains, why): permanent exceptions.
# Where an owner question or ruling id's shape (`Q` or `R`, a dash, digits) is a data format the
# code under test parses, not a citation: the hub's own ids, and the ledger and backlog fixtures.
ID_SHAPE_FOLDERS = (
    ('kitlib/hubsite/*', "the hub's store and API: question ids are its data"),
    ('kitlib/hubmcp/*', "the hub's MCP tools: question ids are its data"),
    ('tests/hubsite/*', "tests of the hub's store and API: question ids are its data"),
    ('tests/hubmcp/*', "tests of the hub's MCP tools: question ids are its data"),
    ('tests/hubvalidate/*', 'tests of the hub id validator, which checks the id shape'),
    ('tests/guard/test_rules_hub*.py', "tests of the guard's hub rules, which match the id shape"),
    ('tools/hub.py', "the hub CLI's help and examples show question ids"),
    ('docs/action-hub.md', "the hub's doc: question ids are its data"),
) + tuple((folder + '*', why) for folder, why in FIXTURE_FOLDERS)

ALLOWED = tuple(('%s' % glob, 'record-id', '', why) for glob, why in ID_SHAPE_FOLDERS) + (
    ('kitlib/git/fake/wtcmds.py', 'pinned-hash', 'abc1234', 'an invented hash in the fake git'),
    ('tests/launcher/test_current_account.py', 'record-id', "HOME + '\\\\.",
     'an invented config folder upper-cased to prove Windows case folding'),
    ('tests/guard/test_config_move.py', 'record-id', "os.path.dirname(self.w.ledger), '",
     'an invented file name for the replaced ledger'),
    ('tests/scrub/test_no_cites.py', 'record-id', "'docs/near.md': 'PR-",
     'longer tokens that end like a cite, to prove the cite pattern anchors on a word boundary; '
     'an EX- prefix would change what the anchor sees'),
    ('.kit.json', 'record-id', '"gate_time": {"prefix": "KIT' '-GATE-SLOW"',
     'the maint_lint record prefix set in the repo config (owner-approved exception)'),
)

# Today's hits, by the work that removes them.
# Invented neutral fixtures replace the owner's real records and examples, and invented ids take
# the EX- prefix (scrub.MADE_UP_ID_PREFIX).
PENDING_FIXTURES = {
}
# Record ids in comments and docs become the reason written out; provenance pointers at the
# replaced kit and pinned commit hashes go.
PENDING_HISTORY = {
    ('pinned-hash', 'docs/commit.md'): 1,
    ('pinned-hash', 'docs/pm-watch.md'): 1,
    ('pinned-hash', 'docs/review-pack.md'): 1,
    ('pinned-hash', 'docs/sync-base.md'): 1,
    ('pinned-hash', 'docs/test-tools.md'): 2,
    ('pinned-hash', 'kitlib/pmwatch/__init__.py'): 1,
    ('pinned-hash', 'kitlib/proc/shield.py'): 1,
    ('pinned-hash', 'kitlib/pytests/__init__.py'): 1,
    ('provenance', 'docs/commit.md'): 2,
    ('provenance', 'docs/doc-tools.md'): 2,
    ('provenance', 'docs/gate.md'): 14,
    ('provenance', 'docs/go-live.md'): 4,
    ('provenance', 'docs/install.md'): 1,
    ('provenance', 'docs/landed.md'): 2,
    ('provenance', 'docs/launcher.md'): 3,
    ('provenance', 'docs/ledger.md'): 1,
    ('provenance', 'docs/lock-release.md'): 1,
    ('provenance', 'docs/machine-setup.md'): 5,
    ('provenance', 'docs/new-worktree.md'): 2,
    ('provenance', 'docs/pending.md'): 1,
    ('provenance', 'docs/pm-watch.md'): 1,
    ('provenance', 'docs/preflight.md'): 3,
    ('provenance', 'docs/repo-checks.md'): 1,
    ('provenance', 'docs/review-pack.md'): 1,
    ('provenance', 'docs/sync-base.md'): 1,
    ('provenance', 'docs/test-tools.md'): 1,
    ('provenance', 'docs/worktree-paths.md'): 2,
    ('provenance', 'docs/worktree-remove.md'): 2,
    ('provenance', 'installer/machine-setup.ps1'): 2,
    ('provenance', 'kitlib/backlog/check.py'): 2,
    ('provenance', 'kitlib/backlog/errors.py'): 2,
    ('provenance', 'kitlib/backlog/landable.py'): 2,
    ('provenance', 'kitlib/backlog/offer.py'): 2,
    ('provenance', 'kitlib/backlog/plan.py'): 2,
    ('provenance', 'kitlib/backlog/record.py'): 2,
    ('provenance', 'kitlib/backlog/scope.py'): 1,
    ('provenance', 'kitlib/backlog/stale.py'): 2,
    ('provenance', 'kitlib/commit/__init__.py'): 1,
    ('provenance', 'kitlib/config/load.py'): 2,
    ('provenance', 'kitlib/config/owner.py'): 2,
    ('provenance', 'kitlib/docs/budget.py'): 1,
    ('provenance', 'kitlib/docs/codemap.py'): 1,
    ('provenance', 'kitlib/docs/index.py'): 1,
    ('provenance', 'kitlib/forbidden/__init__.py'): 2,
    ('provenance', 'kitlib/gate/land.py'): 2,
    ('provenance', 'kitlib/gate/reuse.py'): 1,
    ('provenance', 'kitlib/gate/steps.py'): 2,
    ('provenance', 'kitlib/git/batch.py'): 2,
    ('provenance', 'kitlib/git/checkout.py'): 1,
    ('provenance', 'kitlib/git/repo.py'): 1,
    ('provenance', 'kitlib/landed/__init__.py'): 2,
    ('provenance', 'kitlib/machinepaths/__init__.py'): 2,
    ('provenance', 'kitlib/machinesetup/__init__.py'): 4,
    ('provenance', 'kitlib/machinesetup/extras.py'): 1,
    ('provenance', 'kitlib/machinesetup/settings.py'): 8,
    ('provenance', 'kitlib/preflight/leftovers.py'): 1,
    ('provenance', 'kitlib/proc/shield.py'): 2,
    ('provenance', 'kitlib/pytests/__init__.py'): 2,
    ('provenance', 'kitlib/pytests/child.py'): 2,
    ('provenance', 'kitlib/pytests/runner.py'): 2,
    ('provenance', 'kitlib/records/landing.py'): 2,
    ('provenance', 'kitlib/reviewpack/__init__.py'): 1,
    ('provenance', 'kitlib/reviewpack/trailers.py'): 1,
    ('provenance', 'kitlib/stubs/__init__.py'): 2,
    ('provenance', 'kitlib/syncbase/__init__.py'): 1,
    ('provenance', 'kitlib/testlist/__init__.py'): 2,
    ('provenance', 'kitlib/tooldeps/__init__.py'): 2,
    ('provenance', 'kitlib/tooldeps/deps.py'): 2,
    ('provenance', 'kitlib/verdict/__init__.py'): 1,
    ('provenance', 'kitlib/worktree/holders.py'): 2,
    ('provenance', 'kitlib/worktree/new.py'): 2,
    ('provenance', 'kitlib/worktree/remove.py'): 2,
    ('provenance', 'kitlib/worktree/sweep.py'): 2,
    ('provenance', 'kitlib/worktree/task.py'): 2,
    ('provenance', 'tests/backlog/test_offer.py'): 1,
    ('provenance', 'tests/backlog/test_records.py'): 2,
    ('provenance', 'tests/backlog/test_verbs.py'): 1,
    ('provenance', 'tests/board/test_board.py'): 2,
    ('provenance', 'tests/docs/test_budget.py'): 2,
    ('provenance', 'tests/docs/test_codemap.py'): 1,
    ('provenance', 'tests/docs/test_index.py'): 1,
    ('provenance', 'tests/git/test_stray.py'): 2,
    ('provenance', 'tests/git/test_worktree_paths.py'): 1,
    ('provenance', 'tests/landed/test_landed.py'): 2,
    ('provenance', 'tests/machinepaths/test_machinepaths.py'): 2,
    ('provenance', 'tests/machinesetup/test_machinesetup.py'): 7,
    ('provenance', 'tests/pmwatch/test_pmwatch.py'): 1,
    ('provenance', 'tests/preflight/test_preflight.py'): 2,
    ('provenance', 'tests/proc/test_shield.py'): 2,
    ('provenance', 'tests/pytests/test_pytests.py'): 2,
    ('provenance', 'tests/reviewpack/test_coauthor.py'): 1,
    ('provenance', 'tests/reviewpack/test_pack.py'): 2,
    ('provenance', 'tests/reviewpack/test_rules.py'): 2,
    ('provenance', 'tests/stubs/test_kit_stubs.py'): 1,
    ('provenance', 'tests/syncbase/test_advice.py'): 2,
    ('provenance', 'tests/syncbase/test_sync.py'): 2,
    ('provenance', 'tests/testlist/test_testlist.py'): 2,
    ('provenance', 'tests/testutil.py'): 2,
    ('provenance', 'tests/tooldeps/piecetable.py'): 1,
    ('provenance', 'tests/tooldeps/test_tool_deps.py'): 2,
    ('provenance', 'tests/worktree/test_new_worktree.py'): 2,
    ('provenance', 'tests/worktree/test_worktree_remove.py'): 1,
    ('provenance', 'tools/machine_setup.py'): 1,
    ('record-id', 'docs/guard.md'): 1,
    ('record-id', 'docs/install.md'): 1,
    ('record-id', 'tests/delegations/test_cli.py'): 1,
    ('record-id', 'tests/delegations/test_run.py'): 13,
    ('record-id', 'tests/docs/test_install_junctions.py'): 1,
    ('record-id', 'tests/guard/test_backlog_role.py'): 1,
    ('record-id', 'tests/guard/test_cheap.py'): 5,
    ('record-id', 'tests/host/test_claude_peer.py'): 2,
}
# Owner-specific prose becomes generic: "the owner" for the pronouns, no one machine's parts.
PENDING_OWNER_TEXT = {
    ('gender', 'kitlib/backlog/plan.py'): 1,
    ('gender', 'kitlib/backlog/record.py'): 1,
    ('gender', 'kitlib/commit/__init__.py'): 2,
    ('gender', 'kitlib/config/owner.py'): 1,
    ('gender', 'kitlib/coordinatorstart/__init__.py'): 2,
    ('gender', 'kitlib/worktree/remove.py'): 3,
    ('gender', 'kitlib/worktree/task.py'): 1,
    ('gender', 'tests/coordinatorstart/test_coordinatorstart.py'): 1,
    ('gender', 'tests/gate/test_land_owner.py'): 1,
    ('gender', 'tests/guard/test_rules.py'): 1,
    ('hardware', 'kitlib/lock/layout.py'): 2,
    ('hardware', 'kitlib/lockui/window.py'): 1,
    ('hardware', 'kitlib/proc/cpu.py'): 2,
    ('hardware', 'kitlib/proc/process.py'): 1,
    ('hardware', 'kitlib/proc/shield.py'): 1,
    ('hardware', 'tests/lock/test_layout.py'): 2,
    ('hardware', 'tests/lockmcp/scratch_server.py'): 1,
}
PENDING = (PENDING_FIXTURES, PENDING_HISTORY, PENDING_OWNER_TEXT)


def j(*parts):
    return ''.join(parts)


def configured_folders():
    """The real machine config's `machine_path_folders` through the home fence's one exception
    (tests.real_install_config); () when there is no config file or it sets none, and then the
    one NOTE naming the unset key (tests.shipped.real_machine_folders)."""
    from tests import shipped
    return shipped.real_machine_folders()


class BuiltIn(unittest.TestCase):
    def test_no_new_owner_text_in_a_tracked_file(self):
        found, unused = scrub.scan(tracked_files(), read_text, ALLOWED,
                                   classes=scrub.classes(configured_folders()))
        over, under = scrub.ratchet(found, PENDING)
        for cls, path, n, base in under:
            print('scrub: %s has %d %s hit(s), baseline %d: lower the baseline in tests/scrub/'
                  'test_builtin.py' % (path, n, cls, base))
        keys = {(c, p) for c, p, _, _ in over}
        lines = ['%s (%s: %d hit(s), baseline %d)' % (p, c, n, b) for c, p, n, b in over]
        lines += ['  %s:%d: %s (%s)' % (h.path, h.line, h.text, h.cls) for h in found
                  if (h.cls, h.path) in keys]
        stale = ['  %s %s %r (%s)' % entry for entry in unused]
        # One assertion, so a stale entry and a new hit are both reported in the same run.
        self.assertEqual((over, unused), ([], []),
                         'owner-specific text in tracked files (write it generically; '
                         'an invented record id starts with %s; a permanent exception is an '
                         'ALLOWED entry with its reason):\n%s\nstale ALLOWED entries (nothing '
                         'matches them; remove each):\n%s'
                         % (scrub.MADE_UP_ID_PREFIX, '\n'.join(lines) or '  none',
                            '\n'.join(stale) or '  none'))

    def test_pending_blocks_do_not_share_a_key(self):
        seen = {}
        for i, block in enumerate(PENDING):
            for key in block:
                self.assertNotIn(key, seen, 'in two PENDING blocks: %r' % (key,))
                seen[key] = i


class TheChecks(unittest.TestCase):
    """Each class on invented text: it must find what it is for and pass what it is not."""

    def found(self, files, allowed=(), store=scrub.NAME_STORE):
        hits, _ = scrub.scan(sorted(files), files.get, allowed, store=store)
        return ['%s:%d: %s (%s)' % (h.path, h.line, h.text, h.cls) for h in hits]

    def test_every_class_is_found_with_file_and_line(self):
        files = {'docs/a.md': j('ask ', 'h', 'im; ', 'H', 'is call\n'),
                 'kitlib/b.py': j('# see ', 'ABC', '-DEF-12', ' for why\n'),
                 'docs/c.md': j('Por', 'ted from the earlier one\n', 'an ', 'ol', 'd kit file\n'),
                 'docs/d.md': j('pinned at ', '9f3e', '2a1', '\n'),
                 'knowledge/e.md': j('on an ', 'RT', 'X 4090', ' with ', '16', ' cores\n')}
        self.assertEqual(self.found(files), [
            'docs/a.md:1: ' + j('h', 'im') + ' (gender)',
            'docs/a.md:1: ' + j('H', 'is') + ' (gender)',
            'docs/c.md:1: ' + j('Por', 'ted from') + ' (provenance)',
            'docs/c.md:2: ' + j('ol', 'd kit') + ' (provenance)',
            'docs/d.md:1: ' + j('9f3e', '2a1') + ' (pinned-hash)',
            'kitlib/b.py:1: ' + j('ABC', '-DEF-12') + ' (record-id)',
            'knowledge/e.md:1: ' + j('RT', 'X 4090') + ' (hardware)',
            'knowledge/e.md:1: ' + j('16', ' cores') + ' (hardware)'])

    def test_tests_are_scanned_except_for_hashes(self):
        files = {'tests/x/test_y.py': j('# ', 'h', 'e said ', 'ABC', '-1', ' at ', 'a1b2', 'c3d', '\n')}
        self.assertEqual(self.found(files), [
            'tests/x/test_y.py:1: ' + j('h', 'e') + ' (gender)',
            'tests/x/test_y.py:1: ' + j('ABC', '-1') + ' (record-id)'])

    def test_what_only_looks_like_a_hit_passes(self):
        files = {'docs/a.md': j('the ', 'the', 'me, ', 'hel', 'p and ', 'hist', 'ory; ', 'where',
                                'as she shell\n'),
                 'docs/b.md': j('YYYY', '-MM-DD, ', 'UTF', '-8, ', 'EX', '-WIDGET-1', ', ', 'R', '-nnnn, x',
                                'R', '-1234 and a ', 'Q', '-nnnn, abc-def-1\n'),
                 'docs/c.md': j('4b825dc642cb6eb9a06', '0e54bf8d69288fbee4904', ' ', '0123456789',
                                'abcdef', ' deadbeef 1234567 ', 'a1b2c3', '\n'),
                 'docs/d.md': j('2', ' cores, ', '4', ' threads, the PM kit, ', 'sup', 'port from x\n')}
        self.assertEqual(self.found(files), [])

    def test_a_one_letter_question_or_ruling_id_is_a_record_id(self):
        # An id glued to a letter is another token; T, A, M, P and S ids are the hub's, and pass.
        files = {'docs/a.md': j('see ', 'Q', '-123', ' and ', 'R', '-4\n'),
                 'tests/x/test_y.py': j('"', 'Q', '-1', '"\n'),
                 'docs/b.md': j('x', 'R', '-1234 ', 'T', '-1 ', 'A', '-001 ', 'S', '-1 ', 'Q', '-x ',
                                'q', '-1\n')}
        self.assertEqual(self.found(files), [
            'docs/a.md:1: ' + j('Q', '-123') + ' (record-id)',
            'docs/a.md:1: ' + j('R', '-4') + ' (record-id)',
            'tests/x/test_y.py:1: ' + j('Q', '-1') + ' (record-id)'])

    def test_the_id_shape_allowlist_covers_the_hub_but_not_other_files(self):
        line = j('ask ', 'Q', '-123\n')
        files = {'tests/hubsite/test_a.py': line, 'tools/hub.py': line,
                 'tests/ledger/test_a.py': j('R', '-9001\n'), 'kitlib/other.py': line,
                 'tests/guard/test_rules_hub_read.py': line, 'tests/guard/test_other.py': line}
        hits, _ = scrub.scan(sorted(files), files.get, ALLOWED)
        self.assertEqual([h.path for h in hits], ['kitlib/other.py', 'tests/guard/test_other.py'])

    def test_standard_crypto_names_are_no_record_ids(self):
        real = j('ABC', '-DEF-12')
        for name in ('EMSA-PKCS1', 'RSASSA-PKCS1', 'SHA-384'):
            with self.subTest(name):
                files = {'docs/a.md': 'signed with %s\n' % name}
                self.assertEqual(self.found(files), [])
                files = {'docs/a.md': 'signed with %s, see %s\n' % (name, real)}
                self.assertEqual(self.found(files), ['docs/a.md:1: %s (record-id)' % real])

    def test_an_allow_entry_covers_only_its_file_and_class(self):
        line = j('ask ', 'h', 'im at ', '9f3e', '2a1', '\n')
        files = {'docs/a.md': line, 'docs/b.md': line}
        allow = (('docs/a.md', 'gender', 'ask', 'example'),)
        hits, unused = scrub.scan(sorted(files), files.get, allow)
        self.assertEqual(sorted((h.path, h.cls) for h in hits),
                         [('docs/a.md', 'pinned-hash'), ('docs/b.md', 'gender'),
                          ('docs/b.md', 'pinned-hash')])
        self.assertEqual(unused, [])
        _, unused = scrub.scan(sorted(files), files.get, allow + (('docs/z.md', 'gender', 'x', 'gone'),))
        self.assertEqual(unused, [('docs/z.md', 'gender', 'x', 'gone')])

    def test_the_ratchet_fails_more_and_new_and_prints_fewer(self):
        H = scrub.Hit
        found = [H('docs/a.md', 1, 'gender', 'x'), H('docs/a.md', 2, 'gender', 'x'),
                 H('docs/b.md', 1, 'gender', 'x'), H('docs/new.md', 1, 'record-id', 'x')]
        base_one = {('gender', 'docs/a.md'): 1, ('gender', 'docs/b.md'): 1}
        base_two = {('gender', 'docs/c.md'): 3}
        over, under = scrub.ratchet(found, (base_one, base_two))
        self.assertEqual(over, [('gender', 'docs/a.md', 2, 1), ('record-id', 'docs/new.md', 1, 0)])
        self.assertEqual(under, [('gender', 'docs/c.md', 0, 3)])
        self.assertEqual(scrub.ratchet(found[:1], ({('gender', 'docs/a.md'): 2},)),
                         ([], [('gender', 'docs/a.md', 1, 2)]))

    def test_provenance_names_the_replaced_kit_not_this_one(self):
        old, pm = j('ol', 'd'), j('P', 'M')
        files = {'docs/a.md': '\n'.join([
            j('the switch from the ', old, ' ', pm, ' kit'),
            j('as the ', pm, " kit's `_paths", '.py` did'),
            j('the ', pm, ' kit gate', '.py checks'),
            j(old, ' ', pm, '-kit spellings'),
            j('an earl', 'ier kit, a previ', 'ous kit and a lega', 'cy kit'),
            j('Por', 'ted\nfrom the one before, an ', old, '\nkit file'),
            '']),
            'docs/b.md': j('# ', pm, ' kit\n', 'the ', pm, ' kit as a plugin; ', pm,
                           ' kit tools live here\n')}
        self.assertEqual(self.found(files), [
            'docs/a.md:1: ' + j(old, ' ', pm, ' kit') + ' (provenance)',
            'docs/a.md:2: ' + j(pm, " kit's `_paths", '.py') + ' (provenance)',
            'docs/a.md:3: ' + j(pm, ' kit gate', '.py') + ' (provenance)',
            'docs/a.md:4: ' + j(old, ' ', pm, '-kit') + ' (provenance)',
            'docs/a.md:5: ' + j('earl', 'ier kit') + ' (provenance)',
            'docs/a.md:5: ' + j('previ', 'ous kit') + ' (provenance)',
            'docs/a.md:5: ' + j('lega', 'cy kit') + ' (provenance)',
            'docs/a.md:6: ' + j('Por', 'ted from') + ' (provenance)',
            'docs/a.md:7: ' + j(old, ' kit') + ' (provenance)'])

    def test_an_allow_entry_matches_the_line_a_wrapped_hit_starts_on(self):
        files = {'docs/a.md': j('x\nPor', 'ted\nfrom y\n')}
        self.assertEqual(self.found(files, (('docs/a.md', 'provenance', 'Por', 'starts here'),)), [])
        self.assertEqual(len(self.found(files, (('docs/a.md', 'provenance', 'from y', 'no'),))), 1)

    def test_hardware_shapes(self):
        files = {'docs/a.md': '\n'.join([
            j('a ', '12', '-core part with ', '24', ' hardware threads'),
            j('a Thread', 'ripper, an EP', 'YC, an Inte', 'l Core i7'),
            j('an RT', 'X  3080'),
            j('8', '-core, ', '4', ' threads and a core i7 alone pass'),
            ''])}
        self.assertEqual(self.found(files), [
            'docs/a.md:1: ' + j('12', '-core') + ' (hardware)',
            'docs/a.md:1: ' + j('24', ' hardware threads') + ' (hardware)',
            'docs/a.md:2: ' + j('Thread', 'ripper') + ' (hardware)',
            'docs/a.md:2: ' + j('EP', 'YC') + ' (hardware)',
            'docs/a.md:2: ' + j('Inte', 'l Core i7') + ' (hardware)',
            'docs/a.md:3: ' + j('RT', 'X 3080') + ' (hardware)'])

    def test_a_drive_path_into_a_user_profile_is_found_in_tests_too(self):
        # The scrub reads no configuration, and the kit codes no machine folder list (the machine
        # config's machine_path_folders): only a user profile is a machine folder here.
        bs = chr(92)
        files = {'tests/x/test_y.py': '\n'.join([
            j('a = "', 'Z', ':', bs * 2, 'Src', bs * 2, 'repo"'),
            j('b = "', 'z', ':/', 'us', 'ers', bs, 'x"'),
            j('c = "', 'C', ':/', 'us', 'ers/x and ', 'D', ':/', 'kit', '-temp"'),
            j('d = "', 'D', ':/src/alpha", "', 'C', ':/', 'Users', 'x/y"'),
            ''])}
        self.assertEqual(self.found(files), [
            'tests/x/test_y.py:2: ' + j('z', ':/', 'us', 'ers') + ' (drive)',
            'tests/x/test_y.py:3: ' + j('C', ':/', 'us', 'ers') + ' (drive)'])

    def test_the_scrub_codes_no_folder_but_a_user_profile(self):
        self.assertEqual(scrub.MACHINE_FOLDERS, ('Users',))
        self.assertIs(scrub.classes(), scrub.CLASSES)
        self.assertIs(scrub.classes(()), scrub.CLASSES)

    def test_a_configured_machine_folder_is_found_in_tests_too(self):
        # The machine config's machine_path_folders, passed in (scrub.classes): the scrub itself
        # reads no configuration.
        bs = chr(92)
        files = {'tests/x/test_y.py': '\n'.join([
            j('a = "', 'Z', ':', bs * 2, 'Src', bs * 2, 'repo"'),
            j('b = "', 'D', ':/', 'srcs', '/x and ', 'C', ':/', 'us', 'ers/x"'),
            ''])}
        hits, _ = scrub.scan(sorted(files), files.get, classes=scrub.classes(('Src',)))
        self.assertEqual(['%s:%d: %s (%s)' % (h.path, h.line, h.text, h.cls) for h in hits], [
            'tests/x/test_y.py:1: ' + j('Z', ':', bs * 2, 'Src') + ' (drive)',
            'tests/x/test_y.py:2: ' + j('C', ':/', 'us', 'ers') + ' (drive)'])

    def test_the_gate_check_reads_the_real_configs_machine_path_folders(self):
        import json
        import os
        import tempfile
        from unittest import mock
        import tests
        from kitlib.config import user as kuser
        from tests.scrub import test_builtin as me
        home = tempfile.mkdtemp(prefix='kit-scrub-home-')
        self.addCleanup(__import__('shutil').rmtree, home, True)
        with mock.patch.object(tests, '_REAL_HOME', home), \
                mock.patch.object(tests, '_REAL_POINTER', (None, 'no pointer here')):
            self.assertEqual(me.configured_folders(), ())       # no machine config file
            os.makedirs(os.path.join(home, kuser.DIR_NAME))
            with open(os.path.join(home, kuser.DIR_NAME, kuser.FILE_NAME), 'w', encoding='utf-8') as f:
                json.dump({'machine_path_folders': ['Src']}, f)
            self.assertEqual(me.configured_folders(), ('Src',))
            extra = 'tests/zz_new.py'
            files, real_read = me.tracked_files() + [extra], me.read_text

            def read(rel):
                return j('p = "', 'D', ':/', 'Src', '/x"\n') if rel == extra else real_read(rel)
            result = unittest.TestResult()
            with mock.patch.object(me, 'tracked_files', lambda: files), \
                    mock.patch.object(me, 'read_text', read):
                me.BuiltIn('test_no_new_owner_text_in_a_tracked_file').run(result)
        self.assertEqual(len(result.failures), 1, result.errors)
        self.assertIn('tests/zz_new.py (drive: 1 hit(s), baseline 0)', result.failures[0][1])

    def test_machine_folders_agree_with_the_shipped_text_scrub(self):
        import re
        from tests.shipped import BASE_FORBIDDEN
        shipped = {w.lower() for rx, what in BASE_FORBIDDEN if what != 'ruling id'
                   for w in re.findall('[A-Za-z]{4,}', rx.pattern)}
        self.assertEqual(shipped, {f.lower() for f in scrub.MACHINE_FOLDERS})

    def test_a_new_hit_in_a_tracked_file_turns_the_gate_check_red(self):
        from unittest import mock
        from tests.scrub import test_builtin as me
        extra = 'docs/zz-new.md'

        files, real_read = me.tracked_files() + [extra], me.read_text

        def read(rel):
            return j('ask ', 'h', 'im\n') if rel == extra else real_read(rel)
        result = unittest.TestResult()
        with mock.patch.object(me, 'tracked_files', lambda: files), \
                mock.patch.object(me, 'read_text', read):
            me.BuiltIn('test_no_new_owner_text_in_a_tracked_file').run(result)
        self.assertEqual(len(result.failures), 1, result.errors)
        self.assertIn(extra + ':1: ' + j('h', 'im') + ' (gender)', result.failures[0][1])

    def test_a_hashed_name_is_found_without_being_written_down(self):
        name = 'quillfernow'                      # invented; NAME_STORE does not hold it
        salt = scrub.NAME_STORE['salt']
        store = {'salt': salt, 'tokens': [
            {'length': len(name), 'digest': forbidden.digest(bytes.fromhex(salt), name)}]}
        files = {'docs/a.md': 'fine\nthe Quillfernow repo\n', 'tests/b.py': 'x = "quill-fernow"\n'}
        self.assertEqual(self.found(files, store=store),
                         ['docs/a.md:2: <name> (name)', 'tests/b.py:1: <name> (name)'])
        self.assertEqual(self.found(files), [])   # the shipped store does not know the name


if __name__ == '__main__':
    unittest.main()
