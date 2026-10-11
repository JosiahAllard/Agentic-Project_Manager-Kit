"""kitlib.config load/schema: `.kit.json` read and checked whole, section files (and no legacy
files), duplicate keys, the hook and owner sections, and load_at reading a revision (a real temp repo)."""
import unittest

from kitlib import config
from kitlib.config.load import _gate_rules as loadmod_gate_rules, check_value
from kitlib.config import schema
from kitlib.config.types import Regex
from tests import gitfixture
from tests.config.helpers import (AGENT, BETA_FILES, BETA_SHAPE, OWNER, ALPHA_FILES,
                                  ALPHA_SHAPE, TempTree)


class TreeCase(unittest.TestCase):

    def tree(self, kit=None, files=None):
        t = TempTree(kit, files)
        self.addCleanup(t.cleanup)
        return t

    def load(self, kit=None, files=None):
        return config.load(self.tree(kit, files).root)

    def refused(self, kit=None, files=None):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(kit, files)
        return str(cm.exception)


class DefaultsTest(TreeCase):

    def test_no_kit_json_is_every_default(self):
        cfg = self.load()
        self.assertEqual(cfg.main_branch, 'main')
        self.assertEqual(cfg.remote, 'origin')
        self.assertIs(cfg.push_block, False)
        self.assertEqual(cfg.tools_dir, 'tools')
        self.assertEqual(cfg.section('owner')['paths'], schema.OWNER_PATHS)
        self.assertIsNone(cfg.hook())
        self.assertFalse(cfg.has('gate'))

    def test_a_section_is_its_defaults_with_the_written_keys_laid_over(self):
        cfg = self.load({'repo': {'main_branch': 'master', 'push_block': True}})
        self.assertEqual(cfg.main_branch, 'master')
        self.assertIs(cfg.push_block, True)
        self.assertEqual(cfg.remote, 'origin')

    def test_defaults_are_fresh_copies(self):
        cfg = self.load()
        cfg.section('owner')['paths'].append('X')
        self.assertEqual(cfg.section('owner')['paths'], schema.OWNER_PATHS)

    def test_an_explicit_null_is_allowed_where_the_default_is_unset(self):
        cfg = self.load({'doc_budget': {'stale_days': None}})
        self.assertIsNone(cfg.section('doc_budget')['stale_days'])

    def test_notes_are_allowed_at_every_level_and_dropped(self):
        cfg = self.load({'_about': 'x', 'comment': 'y',
                         'repo': {'comment': 'z', 'name': 'r'},
                         'gate': {'steps': [{'name': 's', 'always': True, 'cmd': ['x'], '_why': 'w'}]}})
        self.assertNotIn('comment', cfg.section('repo'))
        self.assertEqual(cfg.raw('repo')['comment'], 'z')


class RefusalTest(TreeCase):

    def test_an_unknown_section_refuses_and_names_the_known_ones(self):
        why = self.refused({'repos': {}})
        self.assertIn('unknown section(s) repos', why)
        self.assertIn('repo', why)

    def test_an_unknown_key_refuses(self):
        self.assertIn('unknown key(s) main', self.refused({'repo': {'main': 'x'}}))

    def test_every_problem_is_named_in_one_error(self):
        why = self.refused({'nosuch': {}, 'repo': {'main_branch': 3, 'bogus': 1},
                            'preflight': {'udp_ports': [1]}})
        for part in ('nosuch', '"repo".main_branch must be a string', 'bogus',
                     '"preflight".udp_ports must hold exactly 2 items'):
            self.assertIn(part, why)

    def test_bad_json_refuses_rather_than_reading_as_no_config(self):
        self.assertIn('is not valid JSON', self.refused('{"repo": '))

    def test_a_non_object_refuses(self):
        self.assertIn('must hold a JSON object', self.refused('[1]'))

    def test_a_section_of_the_wrong_type_refuses(self):
        self.assertIn('"repo" must be an object', self.refused({'repo': 'repo.json'}))
        self.assertIn('or a repo-relative .json path', self.refused({'gate': 3}))

    def test_a_bad_regex_and_a_missing_group_refuse(self):
        self.assertIn('does not compile', self.refused({'gate': {'errors': {'match': '('}}}))
        self.assertIn('"gate".fail must be regex or list of regex', self.refused({'gate': {'fail': ['(']}}))
        errs = []
        Regex(groups=('failed',)).check('^GATE', 'x', errs)
        self.assertEqual(errs, ['x needs the named group(s) failed'])
        self.assertIn('unknown key(s) verdict_pattern',
                      self.refused({'landing': {'verdict_pattern': '^GATE'}}))

    def test_a_path_outside_the_repository_refuses(self):
        self.assertIn('must be a path inside the repository',
                      self.refused({'board': {'state_file': '../x.json'}}))
        self.assertIn('must be a path inside the repository',
                      self.refused({'repo': {'tools_dir': 'C:/tools'}}))

    def test_an_owner_email_must_be_an_email(self):
        self.assertIn('an email address', self.refused({'owner': {'email': 'not-an-address'}}))


class GateRulesTest(TreeCase):

    def steps(self, *steps):
        return self.refused({'gate': {'steps': list(steps)}})

    def test_duplicate_step_names_refuse(self):
        s = {'name': 'a', 'always': True, 'cmd': ['x']}
        self.assertIn("duplicate step name 'a'", self.steps(s, dict(s)))

    def test_an_unknown_placeholder_refuses(self):
        self.assertIn('unknown placeholder(s) {repo}',
                      self.steps({'name': 'a', 'always': True, 'cmd': ['{repo}/x']}))

    def test_full_cmd_is_a_cmd_step_argv_with_known_placeholders(self):
        self.assertIn('"full_cmd": unknown placeholder(s) {repo}',
                      self.steps({'name': 'a', 'always': True, 'cmd': ['x'], 'full_cmd': ['{repo}']}))
        why = self.refused({'hook': {'module': 'gate_hook'},
                            'gate': {'steps': [{'name': 'a', 'always': True, 'check': 'pins',
                                                'full_cmd': ['x']}]}})
        self.assertIn('full_cmd only apply to a "cmd" step', why)
        step = {'name': 'a', 'always': True, 'cmd': ['x', '--select'], 'full_cmd': ['x', '--full']}
        self.assertEqual(self.load({'gate': {'steps': [step]}}).section('gate')['steps'], [step])

    def test_a_step_needs_globs_or_a_reason_to_run(self):
        self.assertIn('needs "globs"', self.steps({'name': 'a', 'cmd': ['x']}))

    def test_contradictions_refuse(self):
        why = self.steps({'name': 'a', 'always': True, 'manual': True, 'cmd': ['x']},
                         {'name': 'b', 'always': True, 'cmd': ['x'], 'requires_strict': True},
                         {'name': 'd', 'always': True, 'cmd': ['x'], 'requires': ['git status']})
        for part in ('contradict', '"requires_strict" needs "requires"', 'must be a bare command name'):
            self.assertIn(part, why)

    def test_the_dropped_step_keys_and_repo_gate_keys_refuse(self):
        why = self.steps({'name': 'a', 'always': True, 'cmd': ['x'], 'unless_only': ['*.md'],
                          'cmd_windows': ['x.exe'], 'exit_code': False, 'offline_flag': '--offline',
                          'kill_retry': True})
        self.assertIn('unknown key(s) cmd_windows, exit_code, kill_retry, offline_flag, unless_only',
                      why)
        # kill_retry went once slot() was shown to end a step's whole tree (tests/lock/
        # test_slot_treekill.py); false is no more a key than true.
        self.assertIn('unknown key(s) kill_retry',
                      self.steps({'name': 'a', 'always': True, 'cmd': ['x'], 'kill_retry': False}))
        why = self.refused({'gate': {'dependency_pin_check': True, 'python_tests_audit': True,
                                     'python_tests_docs': ['m'], 'python_tests_docs_floor': 1,
                                     'python_tests_drops': 'd.txt', 'python_tests_selected_floor': True}})
        self.assertIn('"gate": unknown key(s) dependency_pin_check, python_tests_audit, python_tests_docs, '
                      'python_tests_docs_floor, python_tests_drops, python_tests_selected_floor', why)

    def test_a_step_runs_exactly_one_of_cmd_and_check(self):
        why = self.refused({'hook': {'module': 'gate_hook'},
                            'gate': {'steps': [{'name': 'a', 'always': True},
                                               {'name': 'b', 'always': True, 'cmd': ['x'], 'check': 'pins'}]}})
        self.assertIn('"gate".steps[0] needs exactly one of "cmd" and "check"', why)
        self.assertIn('"gate".steps[1] needs exactly one of "cmd" and "check"', why)

    def test_a_check_step_refuses_the_subprocess_keys(self):
        why = self.refused({'hook': {'module': 'gate_hook'},
                            'gate': {'steps': [{'name': 'a', 'always': True, 'check': 'pins', 'cwd': 'x',
                                                'merged_args': ['-m'],
                                                'ok_exits': [0, 1], 'reuse': True}]}})
        self.assertIn('cwd, merged_args, ok_exits, reuse only apply to a "cmd" step', why)

    def test_a_steps_reuse_is_true_or_a_group_name(self):
        why = self.steps({'name': 'a', 'always': True, 'cmd': ['x'], 'reuse': 'two words'})
        self.assertIn('reuse', why)
        cfg = self.load({'gate': {'steps': [{'name': 'a', 'always': True, 'cmd': ['x'],
                                             'reuse': 'pair'}]}})
        self.assertEqual(cfg.section('gate')['steps'][0]['reuse'], 'pair')

    def test_a_check_step_needs_a_hook_module(self):
        why = self.steps({'name': 'pin-check', 'always': True, 'check': 'pins'})
        self.assertIn('step(s) pin-check run a hook check, and no "hook".module is named', why)

    def test_a_check_name_is_a_word(self):
        why = self.refused({'hook': {'module': 'gate_hook'},
                            'gate': {'steps': [{'name': 'a', 'always': True, 'check': '../x'}]}})
        self.assertIn('a check name', why)

    def test_a_check_step_with_a_hook_module_loads(self):
        step = {'name': 'pin-check', 'always': True, 'check': 'pins', 'warn': True}
        cfg = self.load({'hook': {'module': 'gate_hook'}, 'gate': {'steps': [step]}})
        self.assertEqual(cfg.section('gate')['steps'], [step])


class SectionFileTest(TreeCase):

    def test_a_section_may_name_a_repo_relative_file(self):
        cfg = self.load({'tool_tests': 'tools/tt.json'},
                        {'tools/tt.json': {'mentions': 'tokens'}})
        self.assertEqual(cfg.section('tool_tests')['mentions'], 'tokens')
        self.assertEqual(cfg.section_file('tool_tests'), 'tools/tt.json')

    def test_a_named_file_that_is_missing_refuses(self):
        self.assertIn('which does not exist', self.refused({'tool_tests': 'tools/tt.json'}))

    def test_a_named_file_outside_the_repository_refuses(self):
        self.assertIn('not inside the repository', self.refused({'tool_tests': '../tt.json'}))

    def test_a_named_file_is_checked_like_an_inline_section(self):
        self.assertIn('unknown key(s) nope',
                      self.refused({'tool_tests': 'tt.json'}, {'tt.json': {'nope': 1}}))

    def test_an_unnamed_legacy_file_is_never_read(self):
        cfg = self.load({}, {'tools/gate.json': {'bogus': 1}, 'tools/pieces.json': '{'})
        self.assertFalse(cfg.has('gate'))
        self.assertFalse(cfg.has('pieces'))
        self.assertIsNone(cfg.section_file('gate'))
        self.assertEqual(cfg.section('gate')['steps'], [])

    def test_a_legacy_file_is_read_when_kit_json_names_it(self):
        cfg = self.load({'gate': 'tools/gate.json'}, {'tools/gate.json': {'trailers': True}})
        self.assertEqual(cfg.section_file('gate'), 'tools/gate.json')
        self.assertIs(cfg.section('gate')['trailers'], True)


class DuplicateKeyTest(TreeCase):

    def test_a_key_written_twice_refuses_at_any_depth(self):
        for text, key in (('{"repo": {}, "repo": {}}', 'repo'),
                          ('{"repo": {"name": "a", "name": "b"}}', 'name'),
                          ('{"gate": {"steps": [{"name": "s", "always": true, "cmd": ["x"], "cmd": ["y"]}]}}', 'cmd'),
                          ('{"comment": "a", "comment": "b"}', 'comment')):
            self.assertIn('written twice in one object: %s' % key, self.refused(text))

    def test_every_key_written_twice_is_named(self):
        why = self.refused('{"repo": {"name": "a", "name": "b", "remote": "o", "remote": "p"}}')
        self.assertIn('name, remote', why)

    def test_a_section_file_with_a_key_written_twice_refuses(self):
        self.assertIn('tt.json: key(s) written twice in one object: mentions',
                      self.refused({'tool_tests': 'tt.json'}, {'tt.json': '{"mentions": "path", "mentions": "tokens"}'}))

    def test_the_same_key_in_two_objects_is_fine(self):
        cfg = self.load('{"repo": {"name": "a"}, "backlog": {"ledger_repo": "a"}, "commit": {"merge_fold": true}}')
        self.assertEqual(cfg.section('repo')['name'], 'a')


class HookAndOwnerTest(TreeCase):

    def test_the_hook_is_read_from_the_hook_section(self):
        cfg = self.load({'hook': {'module': 'repo_hook', 'options': {'lanes': 2}}})
        self.assertEqual(cfg.hook(), {'module': 'repo_hook', 'options': {'lanes': 2}})

    def test_the_old_hook_and_owner_spellings_refuse(self):
        why = self.refused({'gate': {'hook': {'module': 'gate_hook'}},
                            'doc_budget': {'owner_email': OWNER, 'agent_emails': [AGENT],
                                           'owner_only': ['X.md'], 'owner_only_except': []}})
        self.assertIn('"gate": unknown key(s) hook', why)
        self.assertIn('"doc_budget": unknown key(s) agent_emails, owner_email, owner_only, owner_only_except', why)

    def test_a_hook_module_name_must_be_an_identifier(self):
        self.assertIn('a module name', self.refused({'hook': {'module': '../evil'}}))

    def test_the_owner_section_is_read(self):
        cfg = self.load({'owner': {'email': OWNER, 'agent_emails': [AGENT], 'paths': ['X.md'],
                                   'paths_except': []}})
        owner = cfg.owner()
        self.assertEqual((owner['email'], owner['agent_emails'], owner['paths'], owner['paths_except']),
                         (OWNER, [AGENT], ['X.md'], []))
        self.assertTrue(cfg.owner_rules().is_owner(OWNER))


class RealRepoShapeTest(TreeCase):
    """Every section and key two real repositories' `.kit.json` files use loads."""

    def test_the_alpha_shape_loads(self):
        cfg = self.load(ALPHA_SHAPE, ALPHA_FILES)
        self.assertEqual(cfg.main_branch, 'master')
        self.assertEqual(cfg.hook(), {'module': 'gate_hook', 'options': {'python_tests_audit': True}})
        self.assertEqual(cfg.section_file('tool_tests'), 'tools/tool_tests.json')
        self.assertEqual(cfg.section_file('pieces'), 'tools/pieces.json')
        self.assertEqual(cfg.owner()['email'], OWNER)
        self.assertEqual(cfg.section('landing')['lock_file'], 'git:alpha-landing.lock')
        self.assertEqual(config.load_hook_module(cfg)[0].HOOKED, 'alpha')
        checks, options = config.load_gate_checks(cfg)
        self.assertEqual((sorted(checks), options), (['pin_check'], {'python_tests_audit': True}))

    def test_the_beta_shape_loads(self):
        cfg = self.load(BETA_SHAPE, BETA_FILES)
        self.assertEqual(cfg.main_branch, 'main')
        self.assertEqual(cfg.section('preflight')['udp_ports'], [7800, 7999])
        self.assertEqual(cfg.section('worktree_remove')['branch_template'], 'phase/{slug}')
        self.assertEqual(cfg.owner()['email'], OWNER)
        self.assertEqual(config.load_hook_module(cfg)[0].HOOKED, 'beta')
        checks, options = config.load_gate_checks(cfg)
        self.assertEqual(sorted(checks), ['py_drops'])
        self.assertEqual(options['python_tests_docs_floor'], 105)


class LoadAtTest(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)

    def test_load_at_reads_the_revision_not_the_working_tree(self):
        self.repo.write('.kit.json', '{"repo": {"main_branch": "trunk"}}')
        self.repo.commit('kit', '.kit.json')
        self.repo.write('.kit.json', '{"repo": {"main_branch": "other"}}')
        self.assertEqual(config.load_at(self.repo.root, 'HEAD').main_branch, 'trunk')
        self.assertEqual(config.load(self.repo.root).main_branch, 'other')

    def test_a_revision_without_kit_json_is_every_default(self):
        self.repo.write('.kit.json', '{"repo": {"main_branch": "trunk"}}')
        self.assertEqual(config.load_at(self.repo.root, self.repo.initial).main_branch, 'main')

    def test_section_files_are_read_at_the_revision_and_unnamed_ones_never(self):
        self.repo.write('.kit.json', '{"tool_tests": "tools/tt.json"}')
        self.repo.write('tools/tt.json', '{"mentions": "tokens"}')
        self.repo.write('tools/gate.json', '{"trailers": true}')
        self.repo.commit('kit', '.kit.json', 'tools/tt.json', 'tools/gate.json')
        self.repo.write('tools/tt.json', '{"mentions": "path"}')
        cfg = config.load_at(self.repo.root, 'HEAD')
        self.assertEqual(cfg.section('tool_tests')['mentions'], 'tokens')
        self.assertFalse(cfg.has('gate'))

    def test_a_named_file_missing_at_the_revision_refuses(self):
        self.repo.write('.kit.json', '{"tool_tests": "tools/tt.json"}')
        self.repo.commit('kit', '.kit.json')
        self.repo.write('tools/tt.json', '{}')
        with self.assertRaises(config.ConfigError) as cm:
            config.load_at(self.repo.root, 'HEAD')
        self.assertIn('which does not exist', str(cm.exception))

    def test_an_unknown_or_option_like_revision_refuses(self):
        for rev in ('no-such-branch', '--output=x'):
            with self.assertRaises(config.ConfigError):
                config.load_at(self.repo.root, rev)


class WrongTypesTest(TreeCase):
    """A wrongly typed gate or hook is a ConfigError naming it, never a TypeError (review item 1)."""

    def test_a_wrongly_typed_gate_is_a_config_error(self):
        for gate, named in (({'steps': 5}, '"gate".steps'),
                            ({'steps': [{'name': 'a', 'cmd': 5, 'always': True}]}, '"gate".steps[0].cmd'),
                            ({'steps': [{'name': 'a', 'cmd': ['x'], 'merged_args': 5, 'always': True}]},
                             '"gate".steps[0].merged_args'),
                            ({'steps': [{'name': 'a', 'cmd': ['x'], 'requires': 5, 'always': True}]},
                             '"gate".steps[0].requires'),
                            ({'steps': [{'name': ['x'], 'cmd': ['x'], 'always': True}]}, '"gate".steps[0].name')):
            with self.subTest(gate=gate):
                self.assertIn(named, self.refused({'gate': gate}))

    def test_the_gate_rules_skip_values_of_the_wrong_type(self):
        errs = []
        loadmod_gate_rules({'steps': [{'name': ['x'], 'cmd': 5, 'merged_args': 5, 'requires': 5,
                                       'globs': ['*']}, 5]}, errs)
        loadmod_gate_rules({'steps': 5}, errs)
        self.assertEqual(errs, [])           # no TypeError, and nothing the type check already names

    def test_hook_options_with_no_module_refuse(self):
        self.assertIn('"hook".options needs a "hook".module', self.refused({'hook': {'options': {'a': 1}}}))
        cfg = config.Config('.', 'x', {'hook': {'options': {'a': 1}}}, {'hook': None})
        with self.assertRaises(config.ConfigError):
            cfg.hook()
        self.assertIsNone(self.load({'hook': {'options': {}}}).hook())


class CheckValueTest(unittest.TestCase):

    def test_check_value_names_every_problem(self):
        with self.assertRaises(config.ConfigError) as cm:
            check_value(schema.HEAVY_ENTRY, {'program': 'x.bat', 'kind': 'bad kind!'}, 'e')
        self.assertIn('e needs "why"', str(cm.exception))
        self.assertIn('a kind name', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
