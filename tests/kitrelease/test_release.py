"""Tracked release identity, immutable publication and Git-free relocation acceptance."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import kitrelease as R, packageexport as P
from kitlib.kitrelease import bootstrap as B
from kitlib.git import cmd
from tests import gitfixture
from tests.realgit import real_git


CHECKER = Path(B.__file__).read_bytes()
ROOT = Path(__file__).resolve().parents[2]


def tracked_fixture():
    repo = gitfixture.TempRepo()
    files = {'kitlib/kitrelease/bootstrap.py': CHECKER,
             'kitlib/kitrelease/__init__.py': (ROOT / 'kitlib/kitrelease/__init__.py').read_bytes(),
             'kitlib/__init__.py': b'',
             'tools/kit_release.py': (ROOT / 'tools/kit_release.py').read_bytes(),
             'tools/operation.py': b'pass\n',
             'mcp/bridge_frontend.py': b'pass\n',
             'tools/bridge_service.py': b'pass\n',
             'hosts/claude/shell/skills/shared/SKILL.md': b'Claude entry\n',
             'codex/skills/shared/SKILL.md': b'Codex entry\n',
             'templates/example/.claude/skills/check/SKILL.md': b'template entry\n',
             'AGENTS.md': b'Reviewed rules\n',
             'hosts.json': (ROOT / 'hosts.json').read_bytes(),
             'kit.json': b'{"name": "pm-kit", "version": "1.2.3"}\n',
             'boot/kitboot.py': b'pass\n',
             '.kit.json': b'{"private":"checkout only"}\n',
             '.claude/session.json': b'private Claude state\n',
             '.codex/session.json': b'private Codex state\n',
             'tests/private.py': b'private test\n',
             'knowledge/asset-generation/guide.md': b'shared knowledge resource\n',
             'optional/agenttemp/agent_temp_sweep.py': b'pass\n',
             'hub/legacy.html': b'legacy duplicate\n'}
    for name, body in files.items():
        target = Path(repo.root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    repo.commit('reviewed runtime', *files)
    return repo


class Release(unittest.TestCase):
    def setUp(self):
        self.repo = tracked_fixture()
        self.addCleanup(self.repo.cleanup)
        self.scratch = tempfile.TemporaryDirectory(prefix='kit-release-')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / 'release'
        self.commit = self.repo.head()

    def export(self):
        return R.export(self.repo.root, self.commit, self.root)

    def test_one_reviewed_sha_ignores_dirty_untracked_and_checkout_state(self):
        self.repo.write('tools/operation.py', 'dirty\n')
        self.repo.write('tools/untracked.py', 'local\n')
        result = self.export()
        self.assertEqual((self.root / 'tools/operation.py').read_bytes(), b'pass\n')
        for name in ('tools/untracked.py', '.kit.json', '.claude', '.codex', '.git', 'tests', 'hub'):
            self.assertFalse((self.root / name).exists(), name)
        self.assertTrue((self.root / 'templates/example/.claude/skills/check/SKILL.md').is_file())
        self.assertEqual((self.root / 'optional/agenttemp/agent_temp_sweep.py').read_bytes(), b'pass\n')
        self.assertEqual((self.root / 'knowledge/asset-generation/guide.md').read_bytes(),
                         b'shared knowledge resource\n')
        marker = P.verify(self.root)
        self.assertEqual(marker['commit'], self.commit)
        self.assertEqual(result['identity'], hashlib.sha256(P.encoded(marker)).hexdigest())
        self.assertFalse(result['ready'])
        self.assertEqual(result['version'], '1.2.3')                # kit.json, the one version
        self.assertTrue((self.root / 'boot/kitboot.py').is_file())
        self.assertEqual(self.export(), result)

    def test_host_shells_come_from_hosts_json_and_an_unregistered_folder_is_not_shipped(self):
        self.export()
        self.assertTrue((self.root / 'hosts.json').is_file())
        self.assertTrue((self.root / 'hosts/claude/shell/skills/shared/SKILL.md').is_file())
        self.assertFalse((self.root / 'codex').exists())     # no hosts.json entry names it
        self.assertEqual(B.ROOTS, B.CORE_ROOTS + B.host_roots(ROOT / 'hosts.json'))
        path = Path(self.scratch.name) / 'hosts.json'
        path.write_text(json.dumps({'a': {'shell': 'hosts/a/shell'}, 'b': {'shell': 'second'},
                                    'c': {'shell': 'hosts/c/shell'}, 'd': {'shell': 'tools'}}))
        self.assertEqual(B.host_roots(path), ('hosts', 'second'))
        for bad in ('{', '{}', '[]', '{"a": {}}', '{"a": {"shell": "../x"}}'):
            path.write_text(bad)
            with self.subTest(bad=bad), self.assertRaises(B.ReleaseError):
                B.host_roots(path)
        with self.assertRaises(B.ReleaseError):
            B.host_roots(Path(self.scratch.name) / 'missing.json')

    def test_new_commit_cannot_change_existing_publication(self):
        result = self.export()
        self.repo.write('tools/operation.py', 'updated\n')
        self.repo.commit('new runtime', 'tools/operation.py')
        with self.assertRaises(ValueError):
            R.export(self.repo.root, self.repo.head(), self.root)
        self.assertEqual(R.verify(self.root, result['identity']), result)

    def test_no_relative_paths_abbreviated_commits_or_untrusted_identity(self):
        for source, commit, destination in [('relative', self.commit, self.root),
                                             (self.repo.root, 'HEAD', self.root),
                                             (self.repo.root, self.commit[:12], self.root),
                                             (self.repo.root, self.commit, 'relative')]:
            with self.subTest(source=source, commit=commit), self.assertRaises(ValueError):
                R.export(source, commit, destination)
        result = self.export()
        for identity in (None, '', 'a' * 63, 'A' * 64, 'f' * 64):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                R.verify(self.root, identity)
        with self.assertRaises(ValueError):
            R.verify(self.root, result['identity'], 'f' * 40)

    def test_tamper_extra_and_missing_files_refused_against_pinned_metadata(self):
        result = self.export()
        target = self.root / 'tools/operation.py'
        target.write_bytes(b'changed\n')
        with self.assertRaises(ValueError):
            R.verify(self.root, result['identity'])
        target.write_bytes(b'pass\n')
        extra = self.root / 'tools/extra.py'
        extra.write_bytes(b'pass\n')
        with self.assertRaises(ValueError):
            R.verify(self.root, result['identity'])
        extra.unlink()
        target.unlink()
        with self.assertRaises(ValueError):
            R.verify(self.root, result['identity'])

    def test_self_consistent_changed_marker_does_not_establish_provenance(self):
        result = self.export()
        marker = P.verify(self.root)
        body = b'changed\n'
        (self.root / 'tools/operation.py').write_bytes(body)
        marker['files']['tools/operation.py'] = hashlib.sha256(body).hexdigest()
        (self.root / P.MARKER).write_bytes(P.encoded(marker))
        P.verify(self.root)  # Consistency alone can pass; the trusted pin still refuses.
        with self.assertRaises(ValueError):
            R.verify(self.root, result['identity'])

    def test_runtime_roots_and_bootstrap_are_required(self):
        for files in ({'kitlib/a.py': b'pass', 'tools/a.py': b'pass'},
                      {'kitlib/kitrelease/bootstrap.py': CHECKER}):
            with self.subTest(files=list(files)):
                destination = Path(self.scratch.name) / str(len(files))
                marker = P.metadata(files, self.commit, 'shared-runtime')
                P.publish(files, self.commit, 'shared-runtime', destination)
                with self.assertRaises(ValueError):
                    R.verify(destination, hashlib.sha256(P.encoded(marker)).hexdigest())
        minimal = gitfixture.TempRepo()
        self.addCleanup(minimal.cleanup)
        minimal.write('tools/a.py', 'pass\n')
        minimal.commit('missing bootstrap', 'tools/a.py')
        with self.assertRaises(ValueError):
            R.export(minimal.root, minimal.head(), Path(self.scratch.name) / 'missing')

    def test_wrong_subtree_or_tracked_state_inside_runtime_refused(self):
        result = self.export()
        marker = P.verify(self.root)
        marker['subtree'] = 'other'
        (self.root / P.MARKER).write_bytes(P.encoded(marker))
        with self.assertRaises(ValueError):
            R.verify(self.root, hashlib.sha256(P.encoded(marker)).hexdigest())
        self.repo.write('kitlib/.codex/session.json', 'state\n')
        self.repo.commit('bad state', 'kitlib/.codex/session.json')
        with self.assertRaises(ValueError):
            R.export(self.repo.root, self.repo.head(), Path(self.scratch.name) / 'bad')
        self.assertFalse((Path(self.scratch.name) / 'bad').exists())
        self.assertFalse(result['ready'])

    def test_relocation_keeps_identity_and_uses_one_publisher(self):
        result = self.export()
        destination = Path(self.scratch.name) / 'relocated' / result['identity']
        with mock.patch.object(P, 'publish', wraps=P.publish) as publisher:
            copied = R.copy_release(self.root, result['identity'], destination)
        self.assertEqual(publisher.call_count, 1)
        self.assertEqual(copied, {**result, 'root': str(destination)})
        self.assertEqual(P.verify(self.root), P.verify(destination))
        self.assertEqual(R.copy_release(self.root, result['identity'], destination), copied)

    def test_copy_refuses_tamper_and_keeps_unowned_destination(self):
        result = self.export()
        destination = Path(self.scratch.name) / 'unowned'
        destination.mkdir()
        (destination / 'owner.txt').write_bytes(b'keep')
        with self.assertRaises(ValueError):
            R.copy_release(self.root, result['identity'], destination)
        self.assertEqual((destination / 'owner.txt').read_bytes(), b'keep')
        (self.root / 'tools/operation.py').write_bytes(b'tamper')
        with self.assertRaises(ValueError):
            R.copy_release(self.root, result['identity'], Path(self.scratch.name) / 'new')
        self.assertFalse((Path(self.scratch.name) / 'new').exists())

    def test_copy_destination_cannot_mutate_immutable_source(self):
        result = self.export()
        with self.assertRaises(ValueError):
            R.copy_release(self.root, result['identity'], self.root / 'tools/nested')
        self.assertEqual(R.verify(self.root, result['identity']), result)
        self.assertEqual(R.copy_release(self.root, result['identity'], self.root), result)

    def test_copy_rechecks_snapshot_after_concurrent_mutation(self):
        result = self.export()
        original = P.verify

        def change_after_inventory(folder, expected=None):
            marker = original(folder, expected)
            (self.root / 'tools/operation.py').write_bytes(b'changed')
            return marker

        with mock.patch.object(P, 'verify', side_effect=change_after_inventory), self.assertRaises(ValueError):
            R.copy_release(self.root, result['identity'], Path(self.scratch.name) / 'new')

    def test_bindings_are_exact_relocated_paths_with_isolated_no_bytecode_python(self):
        result = self.export()
        binding = R.launch_bindings(self.root, result['identity'], sys.executable)
        self.assertEqual(binding['python_args'], ['-I', '-B'])
        self.assertEqual(binding['env'], {'PM_KIT_ROOT': str(self.root),
                                         'PM_KIT_RELEASE_ID': result['identity']})
        self.assertEqual(binding['front'], str(self.root / 'mcp/bridge_frontend.py'))
        self.assertEqual(binding['service'], str(self.root / 'tools/bridge_service.py'))
        self.assertFalse(binding['ready'])
        with self.assertRaises(ValueError):
            R.launch_bindings(self.root, result['identity'], 'python')

    def test_duplicate_reserved_and_case_colliding_marker_paths_refused(self):
        result = self.export()
        marker = P.verify(self.root)
        for path in ('tools/NUL.py', 'tools/a:stream', 'tools/../escape', 'TOOLS/operation.py',
                     '.git/config', 'tools/.kit-package.json'):
            with self.subTest(path=path):
                changed = dict(marker, files={**marker['files'], path: 'a' * 64})
                (self.root / P.MARKER).write_bytes(P.encoded(changed))
                with self.assertRaises(ValueError):
                    R.verify(self.root, hashlib.sha256(P.encoded(changed)).hexdigest())
        text = P.encoded(marker).decode('utf-8').replace('"schema": 1', '"schema": 1, "schema": 1')
        (self.root / P.MARKER).write_text(text, encoding='utf-8')
        with self.assertRaises(ValueError):
            R.verify(self.root, result['identity'])

    def test_link_ancestors_refused(self):
        fake = mock.Mock(st_mode=0, st_file_attributes=0x400)
        with mock.patch.object(Path, 'lstat', return_value=fake), self.assertRaises(ValueError):
            R.verify(self.root, 'a' * 64)


@real_git('release acceptance: real tracked Git blobs then relocated isolated subprocess with no Git on PATH')
class GitFreeAcceptance(unittest.TestCase):
    setUp = Release.setUp
    export = Release.export

    def test_native_junction_release_path_is_refused_without_following(self):
        result = self.export()
        junction = Path(self.scratch.name) / 'linked'
        # A junction needs no symlink privilege and is confined to this owned fixture.
        run = cmd.subprocess_run([os.environ['COMSPEC'], '/d', '/c', 'mklink', '/J',
                                  str(junction), str(self.root)], cwd=self.scratch.name,
                                 env=gitfixture.fixture_env(), capture_output=True, text=True,
                                 timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        with self.assertRaises(ValueError):
            R.verify(junction, result['identity'])
        self.assertEqual(R.verify(self.root, result['identity']), result)

    def test_real_source_and_relocated_checker_need_no_git_or_node_at_runtime(self):
        result = self.export()
        destination = Path(self.scratch.name) / 'relocated'
        copied = R.copy_release(self.root, result['identity'], destination)
        env = gitfixture.fixture_env({'PATH': '', 'KIT_FAKE_GIT': '0'})
        program = (
            'import json,runpy,sys; '
            'check=runpy.run_path(sys.argv[1]); '
            'result=check["verify"](sys.argv[2],sys.argv[3],sys.argv[4]); '
            'assert not any(n=="kitlib" or n.startswith("kitlib.") for n in sys.modules); '
            'print(json.dumps(result))')
        run = cmd.subprocess_run([sys.executable, '-I', '-B', '-c', program,
                                  str(destination / 'kitlib/kitrelease/bootstrap.py'), str(destination),
                                  copied['identity'], copied['commit']], cwd=self.scratch.name,
                                 env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), copied)
        # Actual relocated CLI argument/exit-code acceptance imports no packageexport/Git.
        run = cmd.subprocess_run([sys.executable, '-I', '-B', str(destination / 'tools/kit_release.py'),
                                  'verify', '--root', str(destination), '--identity', copied['identity'],
                                  '--commit', copied['commit']], cwd=self.scratch.name, env=env,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), copied)
        self.assertFalse(list(destination.rglob('__pycache__')))
        (destination / 'tools/operation.py').write_bytes(b'tamper')
        run = cmd.subprocess_run([sys.executable, '-I', '-B', str(destination / 'tools/kit_release.py'),
                                  'verify', '--root', str(destination), '--identity', copied['identity']],
                                 cwd=self.scratch.name, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 2)
        self.assertIn('refused', run.stderr)
