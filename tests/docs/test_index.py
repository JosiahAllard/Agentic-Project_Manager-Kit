"""kitlib.docs.index: the index lookups (find, tools, role), the keyword check,
path_refs, the batched index check and the stale sweep (from the PM kit's
tools/test_doc_index.py).

The unit cases run the functions on fixture text; the repository cases build a throwaway git
repository (tests.gitfixture) and call `index.run` on it. The entry point's argument handling is
in test_cli.py. Dropped from the source suite (docs/doc-tools.md "Not ported"): the git process
counts (_testutil.GitSpawns is not in the kit), KIT_REPO_ROOT stub mode and copy mode
(one kit copy; its cases run against -C in test_cli.py).
"""
import json
import os
import unittest
from unittest import mock

from kitlib.docs import index
from tests.gitfixture import TempRepo
from tests.realgit import real_git

FIXTURE = '''# Doc index

Every tracked `.md`, one line: `path | what it holds | read when | kw: a, b`.
`tools\\doc_budget.py` fails a branch that adds a `.md` without a line here.

## Tools (one call instead of many)
- `python tools/gate.py` | picks the per-branch gate | every gate | kw: gate, tests, Machine Lock
- `python tools/backlog.py next` | the backlog store | picking work | kw: backlog, priority
- Each tool acts on its own file's checkout: run it in your worktree, never main's copy.

## Code map (generated, never committed)
- `python tools/codemap.py` | one line per source area | before searching | kw: code, layout

## Live registries (agent-writable, 8 KB each)
- `docs/ops/ports.md` | port blocks and reservations for the network | before choosing a port | kw: ports, udp
- `docs/specs/NET_TIMEOUTS_DESIGN.md` | ENet timeout constants | transport work | kw: timeouts, reconnect
- `docs/specs/RELAY_DESIGN.md` | relay protocol | internet play | kw: relay, internet
'''

# "text" matching, TOTP only in an entry's own words.
SAMPLE_INDEX = '''# Doc index

Header prose, not an entry: `path | what | when | kw: a, b`.

## Tools (one call instead of many)
- `python tools/preflight.py` | branch, dirty files | FIRST call | kw: start, branch
- `python tools/gate.py` | picks the gate, `--note "<a|b>"` | every gate run | kw: gate, test

## Plan and rules
- `docs/reference/DEPLOY_SPLIT.md` | operator sign-in, TOTP, PBKDF2 | relitigating | kw: decisions, security
- `docs/ops/flakes.md` | known flakes and a gate rerun rule | a test fails | kw: flake, rerun
'''

PROSE_FIXTURE = '''## Scale (one call instead of many)
- Same wrapper, `res://tools/scale_preview.tscn` (`tools/ScalePreview.cs`) | preview | scale calls | keys: scale, preview, crossfire
'''


def entry_paths(hits):
    return [l.split('`')[1] for _n, _s, l, _k in hits]


class FindTests(unittest.TestCase):
    def test_find_prefix_match(self):
        # "time" starts "timeouts"; case-insensitive; no other entry printed
        self.assertEqual(entry_paths(index.find_entries(FIXTURE, 'TIME')),
                         ['docs/specs/NET_TIMEOUTS_DESIGN.md'])

    def test_find_prefix_not_substring(self):
        # "net" starts NET_TIMEOUTS_DESIGN's path word but is only inside "internet": no relay hit
        self.assertEqual(entry_paths(index.find_entries(FIXTURE, 'net')),
                         ['docs/specs/NET_TIMEOUTS_DESIGN.md'])

    def test_find_all_words_must_match(self):
        self.assertEqual(entry_paths(index.find_entries(FIXTURE, 'machine lock')), ['python tools/gate.py'])
        self.assertEqual(index.find_entries(FIXTURE, 'machine ports'), [])

    def test_find_searches_keys_and_path_not_title(self):
        # "network" is only in the ports entry's title column: not searched
        self.assertEqual(index.find_entries(FIXTURE, 'network'), [])
        # "codemap" is only in the path
        self.assertEqual(entry_paths(index.find_entries(FIXTURE, 'codemap')), ['python tools/codemap.py'])

    def test_text_mode_searches_the_whole_line_by_substring(self):
        self.assertEqual(entry_paths(index.find_entries(FIXTURE, 'network', 'text')),
                         ['docs/ops/ports.md'])
        self.assertEqual(entry_paths(index.find_entries(FIXTURE, 'net', 'text')),
                         ['docs/ops/ports.md', 'docs/specs/NET_TIMEOUTS_DESIGN.md',
                          'docs/specs/RELAY_DESIGN.md'])

    def test_text_mode_puts_exact_keyword_hits_first(self):
        hits = index.find_entries(SAMPLE_INDEX, 'gate', 'text')
        self.assertEqual(entry_paths(hits), ['python tools/gate.py', 'docs/ops/flakes.md'])


class KeywordCheckTests(unittest.TestCase):
    def test_keyed_fixture_is_clean(self):
        self.assertEqual(index.keyword_misses(FIXTURE), [])

    def test_entry_without_keys_is_flagged(self):
        text = FIXTURE.replace(' | kw: ports, udp', '')
        misses = index.keyword_misses(text)
        self.assertEqual(len(misses), 1, misses)
        self.assertIn('docs/INDEX.md:15: entry has no keywords (end it with "| kw: a, b"', misses[0])

    def test_the_miss_names_the_configured_index(self):
        misses = index.keyword_misses(FIXTURE.replace(' | kw: ports, udp', ''), 'docs/MAP.md')
        self.assertIn('docs/MAP.md:15: entry has no keywords', misses[0])

    def test_empty_keys_field_is_flagged(self):
        self.assertEqual(len(index.keyword_misses(FIXTURE.replace('kw: code, layout', 'kw: '))), 1)

    def test_keys_alias_is_accepted(self):
        self.assertEqual(index.keyword_misses(FIXTURE.replace('kw: code, layout', 'keys: code, layout')), [])

    def test_a_keys_index_without_keys_is_flagged_too(self):
        text = FIXTURE.replace('kw:', 'keys:').replace(' | keys: ports, udp', '')
        misses = index.keyword_misses(text)
        self.assertEqual(len(misses), 1, misses)
        self.assertIn('docs/INDEX.md:15: entry has no keywords', misses[0])

    def test_prose_and_header_lines_are_not_entries(self):
        lines = [l for _n, _s, l, _k in index.index_entries(FIXTURE)]
        self.assertEqual(len(lines), 6, lines)
        self.assertFalse(any(l.startswith(('Every', '- Each', '`tools')) for l in lines))

    def test_deleted_entries_leave_the_check_clean(self):
        # Whole INDEX lines get deleted: the check is per line, with no expected list
        text = '\n'.join(l for l in FIXTURE.splitlines() if 'specs/' not in l)
        self.assertEqual(index.keyword_misses(text), [])

    def test_index_checks_run_the_keyword_check_on_index_only(self):
        keyless = FIXTURE.replace(' | kw: backlog, priority', '')
        texts = {'docs/INDEX.md': keyless, 'docs/other.md': keyless}
        bad = index.index_checks(['docs/INDEX.md', 'docs/other.md'], {}, texts.get)
        self.assertEqual(len(bad), 1, bad)
        self.assertIn('docs/INDEX.md:8:', bad[0])

    def check(self, body):
        return index.index_checks(['docs/INDEX.md'], {}, lambda p: body)

    def test_an_entry_without_keywords_fails(self):
        bad = self.check(SAMPLE_INDEX + '- `docs/new.md` | a new doc | never\n')
        self.assertEqual(len(bad), 1, bad)
        self.assertIn('entry has no keywords', bad[0])
        self.assertIn('docs/new.md', bad[0])

    def test_an_empty_keyword_field_fails(self):
        self.assertEqual(len(self.check(SAMPLE_INDEX + '- `docs/new.md` | a | b | kw: \n')), 1)

    def test_entries_with_keywords_pass(self):
        self.assertEqual(self.check(SAMPLE_INDEX), [])

    # Every entry form, not only "- `" / "* `". Each of these escaped the kw check before.
    OTHER_FORMS = [
        '+ `docs/new.md` | a | b',
        '  - `docs/new.md` | a | b',
        '1. `docs/new.md` | a | b',
        '2) `docs/new.md` | a | b',
        '- docs/new.md | a | b',
        '- **`docs/new.md`** | a | b',
        '| `docs/new.md` | a | b |',
    ]

    def test_every_entry_form_without_keywords_fails(self):
        for form in self.OTHER_FORMS:
            with self.subTest(form=form):
                bad = self.check(SAMPLE_INDEX + form + '\n')
                self.assertEqual(len(bad), 1, bad)
                self.assertIn('docs/new.md', bad[0])

    def test_every_entry_form_with_keywords_passes_and_is_found(self):
        for form in self.OTHER_FORMS:
            with self.subTest(form=form):
                line = form + ' kw: zebra |' if form.endswith('|') else form + ' | kw: zebra'
                body = SAMPLE_INDEX + line + '\n'
                self.assertEqual(self.check(body), [])
                self.assertEqual([l for _n, _s, l, _k in index.find_entries(body, 'zebra')],
                                 [line])
                self.assertIn('zebra', index.known_keywords(body))

    def test_a_table_header_and_rule_are_not_entries(self):
        body = SAMPLE_INDEX + '| Path | What |\n|---|:---:|\n| `docs/new.md` | a | kw: zebra |\n'
        self.assertEqual(self.check(body), [])
        self.assertEqual(len(index.index_entries(body)), 5)

    def test_a_list_item_without_columns_is_prose(self):
        self.assertEqual(index.index_entries('1. Read `docs/x.md` first.\n- plain prose\n'), [])


class ProseLeadEntryTests(unittest.TestCase):
    # An entry may open with prose ("- Same wrapper, ...") before its first backtick; a keyed
    # bullet is still an entry when it does not open with a backtick.
    def test_prose_lead_keyed_bullet_is_an_entry(self):
        entries = index.index_entries(PROSE_FIXTURE)
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(entries[0][3], ['scale', 'preview', 'crossfire'])

    def test_prose_lead_keyed_bullet_is_findable(self):
        self.assertEqual(len(index.find_entries(PROSE_FIXTURE, 'scale')), 1)

    def test_a_bare_line_must_open_with_the_backtick(self):
        self.assertEqual(index.index_entries('Prose `x.md` | y | kw: z\n'), [])


class PathRefs(unittest.TestCase):
    def test_a_double_dot_segment_is_dropped(self):
        self.assertEqual(index.path_refs('See `link/.../adapter/Foo.java`.'), [])

    def test_a_bare_double_dot_segment_is_also_dropped(self):
        self.assertEqual(index.path_refs('See `foo/../bar.md`.'), [])

    def test_two_dots_inside_one_filename_are_not_a_segment_and_are_kept(self):
        self.assertEqual(index.path_refs('See `docs/ops/flakes..md`.'), ['docs/ops/flakes..md'])

    def test_a_backslash_ref_is_normalised_and_checked_by_segment(self):
        self.assertEqual(index.path_refs(r'See `link\...\adapter\Foo.java`.'), [])
        self.assertEqual(index.path_refs(r'See `tools\gate.py:12`.'), ['tools/gate.py'])

    def test_globs_and_placeholders_are_not_paths(self):
        self.assertEqual(index.path_refs('`docs/**/*.md` and `docs/reference/<area>.md`'), [])

    def test_the_union_of_both_repos_extensions_is_matched(self):
        self.assertEqual(index.path_refs('`a/b.tscn` `a/c.gd` `a/d.kt` `a/e.yml` `a/f.png`'),
                         ['a/b.tscn', 'a/c.gd', 'a/d.kt', 'a/e.yml'])


class RepoCase(unittest.TestCase):
    """A fresh repository per test whose tree starts empty (the fixture's README is removed, so
    the .md counts are the test's own), `build(self)` run once in setUp."""

    def build(self):
        pass

    def setUp(self):
        self.r = TempRepo()
        self.addCleanup(self.r.cleanup)
        self.addCleanup(index.close_readers)
        self.repo = self.r.root
        self.r.git('rm', '-q', 'README.md')
        self.r.git('commit', '-q', '-m', 'empty')
        self.build()

    def write(self, rel, body):
        self.r.write(rel, body)

    def commit(self, message='docs'):
        self.r.git('add', '-A')
        self.r.git('commit', '-q', '-m', message)

    def kit_json(self, section):
        self.write('.kit.json', json.dumps({'doc_index': section}))

    def capture(self, **kw):
        """(exit status, output) of index.run on this repository."""
        got = []
        code = index.run(self.repo, got.append, **kw)
        return code, ''.join(l + '\n' for l in got)

    def stale(self, cfg, days, ref='HEAD'):
        files = self.r.git('ls-tree', '-r', '--name-only', ref).splitlines()
        got = []
        index.stale_report(self.repo, cfg, files, days, got.append, ref=ref)
        return ''.join(l + '\n' for l in got)


class IndexCheck(RepoCase):
    """The index check reads every tracked .md at `head` with ONE `git cat-file --batch`."""

    def add_docs(self, first, n):
        for i in range(first, first + n):
            self.write('notes/doc%02d.md' % i, '# Doc %d\n\n## Index\n\n- Body\n\n## Body\n\ntext\n' % i)
        self.commit()

    def test_clean_docs_pass_with_the_ok_line(self):
        self.add_docs(1, 3)
        code, out = self.capture()
        self.assertEqual(code, 0, out)
        self.assertIn('DOC INDEX OK (HEAD as committed, 3 .md files)', out)

    def test_a_section_missing_from_its_index_block_is_still_found(self):
        # The batched read must feed index_checks the same text `git show` did.
        self.add_docs(1, 2)
        with open(os.path.join(self.repo, 'notes', 'doc02.md'), 'a', encoding='utf-8', newline='\n') as fh:
            fh.write('\n## Unindexed\n\nmore\n')
        self.commit('a section the index block does not name')
        code, out = self.capture()
        self.assertEqual(code, 1, out)
        self.assertIn('notes/doc02.md: section "Unindexed" is not in its index block', out)
        self.assertIn('Fix: add the index line in the same commit as the shard or section.', out)

    def test_a_doc_with_a_non_ascii_name_is_listed_and_read(self):
        # Without -z, git lists "notes/d\303\266c.md" in C-quotes: not ending in .md, never read.
        self.write('notes/döc.md', '# Doc\n\n## Index\n\n- Body\n\n## Body\n\ntext\n\n## Unindexed\n\nmore\n')
        self.commit('a non-ASCII name')
        code, out = self.capture()
        self.assertEqual(code, 1, out)
        self.assertIn('notes/döc.md: section "Unindexed" is not in its index block', out)

    def test_a_failed_batch_read_is_a_failure_not_a_pass_over_empty_docs(self):
        self.add_docs(1, 2)
        with mock.patch.object(index.batch, 'cat_file', lambda *a, **k: (None, 'reader gone')):
            code, out = self.capture()
        self.assertEqual(code, 1, out)
        self.assertIn('DOC INDEX FAILED: the git reader died, so the docs were not read: reader gone', out)
        self.assertNotIn('DOC INDEX OK', out)

    def test_a_path_outside_the_batch_goes_through_show_and_a_dead_show_reader_is_reported(self):
        # run() batches every path index_checks reads; texts_at answers any other path through
        # `show`, and reader_died names a reader that died there so run() fails, not passes.
        self.add_docs(1, 1)

        class Dead:
            dead_reason = 'git cat-file died: test'

            def text(self, rev, path):
                return ''

            def close(self):
                pass
        with mock.patch.object(index.batch, 'CatFile', lambda root: Dead()):
            text, why = index.texts_at(self.repo, 'HEAD', ['notes/doc01.md'])
            self.assertIsNone(why)
            self.assertIn('# Doc 1', text('notes/doc01.md'))
            self.assertIsNone(index.reader_died(self.repo))
            self.assertEqual(text('NOTES.md'), '')
            self.assertEqual(index.reader_died(self.repo), 'git cat-file died: test')

    def test_shard_index_config_is_read_through_kit_json(self):
        self.add_docs(1, 1)
        self.write('docs/decisions/d1.md', '# D1\n')
        self.write('docs/DECISIONS.md', '# Decisions\n')
        self.commit()
        self.write('.kit.json', json.dumps({'doc_budget': {'shard_indexes': {'docs/decisions': 'docs/DECISIONS.md'}}}))
        code, out = self.capture()
        self.assertEqual(code, 1, out)
        self.assertIn('docs/decisions/d1.md: not linked from docs/DECISIONS.md', out)

    def test_the_keyword_check_reads_the_configured_index(self):
        self.write('docs/MAP.md', '# Map\n\n- `docs/x.md` | a doc | always\n')
        self.write('docs/INDEX.md', '# Not the index\n\n- `docs/y.md` | a doc | always\n')
        self.commit()
        self.kit_json({'index': 'docs/MAP.md'})
        code, out = self.capture()
        self.assertEqual(code, 1, out)
        self.assertIn('docs/MAP.md:3: entry has no keywords', out)
        self.assertNotIn('docs/INDEX.md:3:', out)

    def test_an_empty_listing_exits_2_not_ok(self):
        code, out = self.capture()
        self.assertEqual(code, 2, out)
        self.assertIn('doc_index: cannot list HEAD (git exit 0): no tracked files', out)

    def test_an_unknown_head_exits_2(self):
        self.add_docs(1, 1)
        code, out = self.capture(head='no-such-ref')
        self.assertEqual(code, 2, out)
        self.assertIn('doc_index: cannot list no-such-ref (git exit 128)', out)


class ListingCheck(RepoCase):
    """Every tracked .md is named on an entry line of the index (check 0, moved here from
    doc_budget): only where the index file exists (decision 26: it is the opt-in, `.kit.json` or
    not) and "doc_index".enabled is not false."""

    SELF = '- `docs/INDEX.md` | this index | always | kw: index\n'

    def docs(self, index_body=None, kit=None, extra=('docs/a.md',)):
        for p in extra:
            self.write(p, '# %s\n' % p)
        if index_body is not None:
            self.write('docs/INDEX.md', '# Index\n\n' + index_body)
        self.commit()
        if kit is not None:
            self.write('.kit.json', json.dumps(kit))

    def test_a_missing_index_skips_the_listing_and_says_so_once(self):
        # The doc index is opt-in (decision 26): no index at the head is no listing check.
        self.docs(kit={})
        code, out = self.capture()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count('doc_index: no docs/INDEX.md in this repository, so the listing '
                                   'check is skipped (the doc index is opt-in'), 1, out)
        self.assertIn('DOC INDEX OK (HEAD as committed, 1 .md files)', out)
        self.assertNotIn('the index is missing', out)

    def test_a_missing_configured_index_is_named_in_the_skip(self):
        self.docs(kit={'doc_index': {'index': 'handbook/MAP.md'}})
        code, out = self.capture()
        self.assertEqual(code, 0, out)
        self.assertIn('doc_index: no handbook/MAP.md in this repository', out)

    def test_enabled_false_says_nothing_about_a_missing_index(self):
        self.docs(kit={'doc_index': {'enabled': False}})
        code, out = self.capture()
        self.assertEqual(code, 0, out)
        self.assertNotIn('opt-in', out)

    def test_an_index_the_base_has_still_fails_when_the_branch_deletes_it(self):
        # A branch cannot switch the check off by deleting the index: --config-ref is the base.
        self.docs(self.SELF + '- `docs/a.md` | a doc | always | kw: a\n', kit={})
        self.commit('kit json')
        base = self.r.git('rev-parse', 'HEAD').strip()
        self.r.git('rm', '-q', 'docs/INDEX.md')
        self.commit('drop the index')
        code, out = self.capture(config_ref=base)
        self.assertEqual(code, 1, out)
        self.assertIn('  docs/INDEX.md: the index is missing (no such file at the ref checked)', out)
        self.assertNotIn('opt-in', out)
        code, out = self.capture()                       # no base to compare: the skip
        self.assertEqual(code, 0, out)

    def test_has_index_counts_a_failed_ref_probe_as_present(self):
        self.docs(kit={})
        self.assertFalse(index.has_index(self.repo, 'docs/INDEX.md', ['docs/a.md']))
        self.assertTrue(index.has_index(self.repo, 'docs/INDEX.md', ['docs/INDEX.md']))
        self.assertTrue(index.has_index(self.repo, 'docs/INDEX.md', [], config_ref='no-such-ref'))
        self.assertFalse(index.has_index(self.repo, 'docs/INDEX.md', [], config_ref='HEAD'))

    def test_an_entry_line_lists_a_doc(self):
        self.docs(self.SELF + '- `docs/a.md` | a doc | always | kw: a\n', kit={})
        code, out = self.capture()
        self.assertEqual(code, 0, out)
        self.assertIn('DOC INDEX OK (HEAD as committed, 2 .md files)', out)

    def test_a_path_in_prose_or_a_table_header_lists_nothing(self):
        body = (self.SELF + '\nSee `docs/a.md` for the rules.\n\n'
                '| `docs/b.md` | holds | read | kw |\n|---|---|---|---|\n')
        self.docs(body, kit={}, extra=('docs/a.md', 'docs/b.md'))
        code, out = self.capture()
        self.assertEqual(code, 1, out)
        for p in ('docs/a.md', 'docs/b.md'):
            self.assertIn('  %s: not in docs/INDEX.md (no entry line names it; add `path | what it '
                          'holds | read when | kw: topic, topic`)' % p, out)
        self.assertNotIn('  docs/INDEX.md: not in', out)

    def test_a_glob_entry_covers_what_it_matches(self):
        self.docs(self.SELF + '- `docs/a.md` | a | always | kw: a\n'
                  '- `docs/items/*.md` | one per item | never | kw: items\n',
                  kit={}, extra=('docs/a.md', 'docs/items/x1.md', 'docs/items/x2.md'))
        code, out = self.capture()
        self.assertEqual(code, 0, out)

    def test_a_retired_path_is_not_a_listing_miss(self):
        # Its presence is doc_budget's failure ("retired path"), not a second one here.
        self.docs(self.SELF, kit={'doc_budget': {'retired_paths': ['docs/a.md']}})
        code, out = self.capture()
        self.assertEqual(code, 0, out)

    def test_enabled_false_skips_the_listing(self):
        self.docs(kit={'doc_index': {'enabled': False}})
        code, out = self.capture()
        self.assertEqual(code, 0, out)
        self.assertNotIn('not in docs/INDEX.md', out)

    def test_no_kit_json_and_no_index_skips_the_listing(self):
        self.docs()
        code, out = self.capture()
        self.assertEqual(code, 0, out)
        self.assertIn('so the listing check is skipped', out)
        self.assertIn('DOC INDEX OK', out)

    def test_an_index_with_no_kit_json_is_checked(self):
        # The index file alone opts in (decision 26); .kit.json only overrides.
        self.docs(self.SELF)
        code, out = self.capture()
        self.assertEqual(code, 1, out)
        self.assertIn('  docs/a.md: not in docs/INDEX.md (no entry line names it', out)
        self.assertNotIn('skipped', out)

    def test_the_listing_reads_the_configured_index(self):
        self.write('docs/MAP.md', '# Map\n\n- `docs/MAP.md` | map | always | kw: map\n')
        self.docs(kit={'doc_index': {'index': 'docs/MAP.md'}})
        code, out = self.capture()
        self.assertEqual(code, 1, out)
        self.assertIn('  docs/a.md: not in docs/MAP.md (no entry line names it', out)
        self.assertNotIn('docs/INDEX.md', out)

    def test_listing_misses_is_pure_and_keeps_the_listing_order(self):
        idx = '- `docs/INDEX.md` | i | a | kw: i\n- `docs/k/*.md` | k | n | kw: k\n'
        files = ['docs/z.md', 'docs/k/one.md', 'docs/INDEX.md', 'tools/x.py', 'docs/b.md', 'docs/old.md']
        got = index.listing_misses(files, {'retired_paths': ['docs/old.md']}, idx)
        self.assertEqual([l.split(':')[0].strip() for l in got], ['docs/z.md', 'docs/b.md'])
        self.assertEqual(index.listing_misses(files, {}, '', 'docs/MAP.md'),
                         ['  docs/MAP.md: the index is missing (no such file at the ref checked)'])


class ShowReader(RepoCase):
    def test_show_reads_blobs_at_a_ref_and_answers_empty_for_a_missing_one(self):
        self.write('a.md', 'one\r\ntwo\n')
        self.commit()
        self.assertEqual(index.show(self.repo, 'HEAD', 'a.md'), 'one\ntwo\n')
        self.assertEqual(index.show(self.repo, 'HEAD', 'missing.md'), '')
        self.assertIsNone(index.reader_died(self.repo))


@real_git('git blame')
class StaleReportMissingRefs(RepoCase):
    """`stale_report`'s "missing ref" row walks a state-doc line through `path_refs`."""

    def test_a_missing_ref_on_a_state_doc_line_is_reported(self):
        self.write('docs/STATE.md', 'row | present `tools/gone.py` here |\n')
        self.commit()
        out = self.stale({'state_docs': ['docs/STATE.md']}, 9999)
        self.assertIn('missing ref `tools/gone.py`', out)

    def test_an_elided_ref_on_a_state_doc_line_is_skipped_not_reported_missing(self):
        self.write('docs/STATE.md',
                   'row | present `link/.../adapter/Foo.java` and `tools/gone.py` too |\n')
        self.commit()
        out = self.stale({'state_docs': ['docs/STATE.md']}, 9999)
        self.assertIn('missing ref `tools/gone.py`', out)
        self.assertNotIn('adapter/Foo.java', out)


class ProxyCheckCase(RepoCase):
    """A repo with an index and one state doc on main, for the labelled-listing tests."""

    def build(self):
        self.write('.kit.json', json.dumps({'doc_budget': {'state_docs': ['docs/STATE.md']}}))
        self.write('docs/INDEX.md', '# Index\n\n- `docs/INDEX.md` | the index | always | kw: index\n'
                   '- `docs/STATE.md` | state | daily | kw: state\n')
        self.write('docs/STATE.md', '# State\n\n- row | on main |\n')
        self.commit('base')


class ProxyCheckLabels(ProxyCheckCase):
    """Every listing-based mode says which tree it read, and
    nothing it was asked to read is skipped in silence."""

    def test_the_ok_line_names_the_ref_and_the_md_count(self):
        code, out = self.capture()
        self.assertEqual(code, 0, out)
        self.assertIn('DOC INDEX OK (HEAD as committed, 2 .md files)', out)
        self.r.git('branch', 'side')
        code, out = self.capture(head='side')
        self.assertIn('DOC INDEX OK (side as committed, 2 .md files)', out)

    def test_a_state_doc_missing_from_the_listing_is_named_and_the_report_is_incomplete(self):
        self.r.git('rm', '-q', 'docs/STATE.md')
        self.commit('state doc gone')
        code, out = self.capture(stale=True, days=9999)
        self.assertEqual(code, 0, out)
        self.assertIn('docs/STATE.md: not in the HEAD listing - NOT swept', out)
        self.assertIn('INCOMPLETE: 1 state doc(s) could not be read', out.splitlines()[-1])

    def test_a_failed_blame_is_named_and_the_report_is_incomplete(self):
        with mock.patch.object(index, 'git', side_effect=lambda root, *a, **k: index.Git(
                128, '', 'fatal: blame broke')):
            out = self.stale({'state_docs': ['docs/STATE.md']}, 0)
        self.assertIn('docs/STATE.md: git blame failed (exit 128) - NOT swept: fatal: blame broke', out)
        self.assertIn('INCOMPLETE: 1 state doc(s) could not be read', out.splitlines()[-1])

    def test_a_role_heading_miss_names_the_head_it_listed(self):
        cfg = {'doc_budget': {'state_docs': ['docs/STATE.md']},
               'doc_index': {'roles': {'worker': ['docs/STATE.md#No such heading']}}}
        self.write('.kit.json', json.dumps(cfg))
        self.commit('roles')
        self.r.git('branch', 'side')
        code, out = self.capture(role='worker', head='side')
        self.assertEqual(code, 1, out)
        self.assertIn('no heading contains "No such heading": searched only docs/STATE.md as '
                      'tracked at side', out)

    def test_a_doc_restricted_section_miss_names_the_one_file_not_a_count(self):
        code, out = self.capture(section='No such heading', doc='docs/STATE.md')
        self.assertIn('searched only docs/STATE.md as tracked at HEAD.', out)
        self.assertNotIn('.md files tracked', out)
        self.write('docs/new.md', '# New\n\n## Fresh heading\n')            # not committed
        code, out = self.capture(section='Fresh heading', doc='docs/new.md')
        self.assertIn('searched only docs/new.md as tracked at HEAD (it is not tracked there, so '
                      'it was not read)', out)

    def test_a_section_miss_says_only_tracked_md_files_were_searched(self):
        self.write('docs/new.md', '# New\n\n## Fresh heading\n\ntext\n')     # not committed
        code, out = self.capture(section='Fresh heading')
        self.assertIn('no heading contains "Fresh heading"', out)
        self.assertIn('searched only the 2 .md files tracked at HEAD', out)
        self.assertIn('an uncommitted new doc is not searched', out)

    def test_a_section_hit_prints_the_span_from_the_working_tree(self):
        code, out = self.capture(section='state')
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[:2], ['docs/STATE.md:1-3', '# State'])


@real_git('git blame')
class ProxyCheckBlame(ProxyCheckCase):
    """The stale report: it blames the listed head."""

    def test_stale_blames_the_listed_head_and_names_it_on_the_summary(self):
        self.r.git('checkout', '-q', '-b', 'side')
        self.write('docs/STATE.md', '# State\n\n- row | on main |\n- row | side only |\n')
        self.commit('side row')
        self.r.git('checkout', '-q', 'main')
        out = self.stale({'state_docs': ['docs/STATE.md']}, 0, ref='side')
        self.assertIn('side only', out)                 # a line only the side branch has
        self.assertNotIn('NOT swept', out)
        self.assertTrue(out.splitlines()[-1].startswith('STALE REPORT (side as committed): '), out)
        code, out = self.capture(stale=True, head='side')
        self.assertEqual(code, 0, out)
        self.assertTrue(out.splitlines()[-1].startswith('STALE REPORT (side as committed): '), out)

    def test_days_zero_lists_a_doc_touched_today(self):
        # `--stale --days 0` is an age of 0 days, not unset: the row committed a moment ago is listed.
        code, out = self.capture(stale=True, days=0)
        self.assertEqual(code, 0, out)
        self.assertIn('docs/STATE.md:3 0 days | - row | on main |', out)

    def test_a_path_on_one_state_doc_does_not_change_the_ref_the_next_is_blamed_at(self):
        self.write('docs/A.md', '- row | names `tools/gone.py` |\n')
        self.write('docs/B.md', '- row | second doc |\n')
        self.commit('two state docs')
        out = self.stale({'state_docs': ['docs/A.md', 'docs/B.md']}, 0, ref='main')
        self.assertIn('missing ref `tools/gone.py`', out)
        self.assertIn('docs/B.md:1 0 days | - row | second doc |', out)
        self.assertNotIn('NOT swept', out)
        self.assertNotIn('INCOMPLETE', out)
        self.assertTrue(out.splitlines()[-1].startswith('STALE REPORT (main as committed): '), out)


class IndexLookupText(RepoCase):
    """The lookups through find_match "text" in .kit.json: only the hit lines print."""

    def build(self):
        self.write('docs/INDEX.md', SAMPLE_INDEX)
        self.kit_json({'find_match': 'text'})

    def test_find_hit_prints_only_the_matching_entries(self):
        code, out = self.capture(find='TOTP')   # entry text, not a keyword; any case
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), [SAMPLE_INDEX.splitlines()[9]])

    def test_find_hit_on_a_keyword_sorts_keyword_hits_first(self):
        code, out = self.capture(find='gate')
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2, out)
        self.assertIn('tools/gate.py', lines[0])
        self.assertIn('docs/ops/flakes.md', lines[1])

    def test_find_miss_lists_the_known_keywords_and_exits_1(self):
        code, out = self.capture(find='no-such-topic')
        self.assertEqual(code, 1, out)
        self.assertIn('Nearest keywords:', out)
        self.assertNotIn('Known keywords', out)
        self.assertNotIn('tools/preflight.py', out)

    def test_find_miss_prints_only_the_ten_nearest_keywords(self):
        text = chr(10).join(['## Docs'] + ['- `d%d.md` | t | w | kw: gate%d' % (i, i) for i in range(15)] + ['- `z.md` | t | w | kw: zebra'])
        near = index.nearest_keywords(text, 'gate')
        self.assertEqual(len(near), 10)
        self.assertNotIn('zebra', near)
        out = []
        self.assertEqual(index.print_find(text, 'gat3x', out.append), 1)
        self.assertEqual(len(out[1].split(', ')), 10)

    def test_tools_prints_only_the_tools_section(self):
        code, out = self.capture(tools=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), SAMPLE_INDEX.splitlines()[4:7])

    def test_the_default_keywords_mode_does_not_find_entry_text(self):
        os.remove(os.path.join(self.repo, '.kit.json'))
        code, out = self.capture(find='TOTP')
        self.assertEqual(code, 1, out)

    def test_an_unknown_find_match_refuses(self):
        # The schema's Enum now names the allowed values (was: must be "keywords" or "text").
        self.kit_json({'find_match': 'fuzzy'})
        code, out = self.capture(find='gate')
        self.assertEqual(code, 2, out)
        self.assertIn('doc_index: refused: kit configuration:', out)
        self.assertIn("find_match must be one of 'keywords', 'text'", out)

    def test_an_unknown_doc_index_key_refuses(self):
        self.kit_json({'find_mode': 'text'})
        code, out = self.capture(find='gate')
        self.assertEqual(code, 2, out)
        self.assertIn('unknown key(s) find_mode', out)

    def test_find_and_tools_read_the_configured_index(self):
        os.rename(os.path.join(self.repo, 'docs', 'INDEX.md'), os.path.join(self.repo, 'docs', 'MAP.md'))
        self.kit_json({'find_match': 'text', 'index': 'docs/MAP.md'})
        code, out = self.capture(find='TOTP')
        self.assertEqual(code, 0, out)
        code, out = self.capture(tools=True)
        self.assertEqual(code, 0, out)
        self.kit_json({'find_match': 'text'})
        code, out = self.capture(find='TOTP')
        self.assertEqual(code, 1, out)
        self.assertIn('doc_index: this repository has no doc index (no docs/INDEX.md)', out)

    def test_an_unreadable_index_still_refuses(self):
        # Only an absent index is "no doc index"; any other read error stays a refusal.
        with mock.patch.object(index, 'read_index', side_effect=PermissionError('denied')):
            code, out = self.capture(find='TOTP')
        self.assertEqual(code, 2, out)
        self.assertIn('doc_index: cannot read docs/INDEX.md: denied', out)

    def test_a_missing_tools_section_exits_1(self):
        self.write('docs/INDEX.md', '# Doc index\n')
        code, out = self.capture(tools=True)
        self.assertEqual((code, out), (1, 'no "## Tools" section in docs/INDEX.md\n'))


ROLE_INDEX = '''# Doc index

## Repo
- `docs/ops/REPO.md` "Worktrees and branches" | where work happens | first | kw: worktree, branch
- `docs/ops/REPO.md` "The gate" | which gate | every change | kw: gate, test
- `docs/ops/gotchas.md` | traps | before editing | kw: gotchas
- `python tools/landed.py` | is it landed | after a landing | kw: landed
- `docs/backlog/*.md` | the store | picking work | kw: backlog
'''

REPO_MD = '''# Repo

## Worktrees and branches
Work in .claude/worktrees/<slug>.
One branch per workstream.

## The gate
''' + ''.join('gate line %d\n' % i for i in range(1, 31)) + '''
## Commits
Explicit paths.
'''

BRIEF = '''# Brief template

Lookups by role, beyond the brief's own:
- worker, deep-worker: `worktree`, `gate`, `gotchas`.
- lander: `landed`, `gate` (the landing gate).
- reviewer: `nothing-matches`, `gotchas`.

After the block.
- not-a-role: `ignored`
'''


class RoleLookup(RepoCase):
    """role: every doc section a role reads, in one call."""

    def build(self):
        self.write('docs/INDEX.md', ROLE_INDEX)
        self.write('docs/ops/REPO.md', REPO_MD)
        self.write('docs/ops/gotchas.md', '# Gotchas\n\n- trap one\n- trap two\n')
        self.write('docs/ops/brief-template.md', BRIEF)
        self.commit('docs')

    def roles(self, **extra):
        cfg = {'roles': {'worker': ['worktree', 'gotchas', 'docs/ops/REPO.md#Commits'],
                         'lander': ['landed', 'backlog']}}
        cfg.update(extra)
        self.kit_json(cfg)

    def test_a_known_role_prints_each_section(self):
        self.roles()
        code, out = self.capture(role='worker')
        self.assertEqual(code, 0, out)
        self.assertIn('ROLE worker: worktree, gotchas, docs/ops/REPO.md#Commits', out)
        self.assertIn('=== worktree -> `docs/ops/REPO.md` "Worktrees and branches"', out)
        self.assertIn('One branch per workstream.', out)
        self.assertIn('=== gotchas -> `docs/ops/gotchas.md`', out)
        self.assertIn('- trap two', out)                       # a doc with no quoted section: its head
        self.assertIn('=== docs/ops/REPO.md#Commits', out)
        self.assertIn('Explicit paths.', out)
        self.assertNotIn('gate line', out)                     # only the named sections

    def test_a_non_doc_entry_prints_its_index_line(self):
        self.roles()
        code, out = self.capture(role='lander')
        self.assertEqual(code, 0, out)
        self.assertIn('- `python tools/landed.py` | is it landed', out)
        self.assertIn('- `docs/backlog/*.md` | the store', out)  # a glob is not one doc

    def test_each_section_is_capped_with_a_pointer_to_the_rest(self):
        self.roles(role_lines=5, roles={'worker': ['gate']})
        code, out = self.capture(role='worker')
        self.assertEqual(code, 0, out)
        self.assertIn('gate line 4', out)
        self.assertNotIn('gate line 5\n', out)
        self.assertIn('more lines; read docs/ops/REPO.md from line', out)

    def test_an_unknown_role_exits_2_and_lists_the_known_ones(self):
        self.roles()
        code, out = self.capture(role='janitor')
        self.assertEqual(code, 2, out)
        self.assertIn('unknown role "janitor". Known roles: lander, worker', out)
        self.assertNotIn('===', out)

    def test_no_roles_configured_exits_2(self):
        code, out = self.capture(role='worker')
        self.assertEqual(code, 2, out)
        self.assertIn('no roles configured', out)

    def test_roles_read_from_the_brief_template(self):
        self.kit_json({'roles_doc': 'docs/ops/brief-template.md', 'roles_marker': 'Lookups by role'})
        code, out = self.capture(role='deep-worker')
        self.assertEqual(code, 0, out)
        self.assertIn('ROLE deep-worker: worktree, gate, gotchas', out)
        self.assertIn('gate line 1', out)
        code, out = self.capture(role='not-a-role')
        self.assertEqual(code, 2, out)
        self.assertIn('Known roles: deep-worker, lander, reviewer, worker', out)

    def test_a_topic_with_no_entry_prints_the_rest_and_exits_1(self):
        self.kit_json({'roles_doc': 'docs/ops/brief-template.md', 'roles_marker': 'Lookups by role'})
        code, out = self.capture(role='reviewer')
        self.assertEqual(code, 1, out)
        self.assertIn('=== nothing-matches -> no docs/INDEX.md entry matches it', out)
        self.assertIn('- trap one', out)

    def test_a_roles_doc_without_its_marker_refuses(self):
        self.kit_json({'roles_doc': 'docs/ops/brief-template.md', 'roles_marker': 'Roles here'})
        code, out = self.capture(role='worker')
        self.assertEqual(code, 2, out)
        self.assertIn('has no line starting "Roles here"', out)

    def test_a_roles_doc_that_cannot_be_read_refuses(self):
        self.kit_json({'roles_doc': 'docs/ops/missing.md', 'roles_marker': 'Lookups by role'})
        code, out = self.capture(role='worker')
        self.assertEqual(code, 2, out)
        self.assertIn('"doc_index".roles_doc docs/ops/missing.md cannot be read', out)

    def test_bad_role_config_refuses(self):
        # role_lines 0 is now refused by the schema (Int(1)): "must be a whole number >= 1"
        # (was: "must be a positive integer").
        for cfg, want in (({'roles': {'worker': []}}, 'non-empty list'),
                          ({'roles': {'worker': ['gate']}, 'roles_doc': 'x.md', 'roles_marker': 'y'},
                           'not both'),
                          ({'roles_doc': 'x.md'}, 'go together'),
                          ({'roles': {'worker': ['gate']}, 'role_lines': 0}, 'role_lines must be a whole number >= 1')):
            with self.subTest(cfg=cfg):
                self.kit_json(cfg)
                code, out = self.capture(role='worker')
                self.assertEqual(code, 2, out)
                self.assertIn(want, out)


if __name__ == '__main__':
    unittest.main()
