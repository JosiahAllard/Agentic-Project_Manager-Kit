"""The gradle detector (docs/module-map.md "Detectors"), on fixture trees."""
import unittest

from kitlib.modulemap import check as C
from kitlib.modulemap.detectors import gradle
from kitlib.modulemap.load import ModuleMap, ROOT

from tests.modulemap._fixtures import Tree

MAP = {'modules': {'api': {'paths': ['shared/api']}, 'gameval': {'paths': ['shared/gameval']},
                   'worker': {'paths': ['worker']}, 'data': {'paths': ['artifacts/gamedata']}}}
SETTINGS = ('rootProject.name = "r"\n'
            'include("api", "gameval", "runtime", "worker")\n'
            'project(":api").projectDir = file("shared/api")\n'
            'project(":gameval").projectDir = file("shared/gameval")\n'
            'project(":runtime").projectDir = file("worker/runtime")\n')


def found(tree, mm=None):
    mm = mm or ModuleMap(MAP)
    return [(c.src, c.dst, c.kind, c.line, c.target)
            for c in gradle.detect(tree.files(), mm, tree.read())]


class GradleTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree({
            'settings.gradle.kts': SETTINGS,
            'shared/api/build.gradle.kts': 'plugins { java }\n',
            'shared/gameval/build.gradle.kts': '',
            'shared/gameval/gameval.gradle.kts': '',
            'worker/runtime/build.gradle.kts': '',
            'artifacts/gamedata/items.json': '{}',
            'tools/x.txt': '',
        })
        self.addCleanup(self.t.cleanup)

    def test_a_project_dependency_maps_through_the_settings_projectdir(self):
        self.t.write('worker/worker/build.gradle.kts',
                     'dependencies {\n  implementation(project(":api"))\n'
                     '  implementation(project(":runtime"))\n}\n')
        self.assertEqual(found(self.t), [('worker', 'api', 'build', 2, ':api')])

    def test_a_project_with_no_override_is_its_path_under_the_root(self):
        self.t.write('shared/api/build.gradle.kts', 'dependencies { api(project(path = ":worker")) }\n')
        self.t.write('worker/build.gradle.kts', '')
        self.assertEqual(found(self.t), [('api', 'worker', 'build', 1, ':worker')])

    def test_an_unknown_project_is_skipped(self):
        self.t.write('worker/worker/build.gradle.kts', 'dependencies { api(project(":nowhere")) }\n')
        self.assertEqual(found(self.t), [])

    def test_apply_from_is_a_build_crossing_and_root_paths_are_data(self):
        self.t.write('worker/worker/build.gradle.kts',
                     'apply(from = rootDir.resolve("shared/gameval/gameval.gradle.kts"))\n'
                     'val d = rootProject.layout.projectDirectory.dir("artifacts/gamedata")\n'
                     'val e = rootProject.file("tools/x.txt")\n'
                     'val f = file("../../artifacts/gamedata/items.json")\n'
                     'val g = "$rootDir/artifacts/gamedata"\n'
                     'val h = file("src/main")\n')
        self.assertEqual(found(self.t), [
            ('worker', 'gameval', 'build', 1, 'shared/gameval/gameval.gradle.kts'),
            ('worker', 'data', 'data', 2, 'artifacts/gamedata'),
            ('worker', ROOT, 'data', 3, 'tools/x.txt'),
            ('worker', 'data', 'data', 4, '../../artifacts/gamedata/items.json'),
            ('worker', 'data', 'data', 5, '"$rootDir/artifacts/gamedata"')])

    def test_groovy_apply_from_with_a_bare_literal(self):
        self.t.write('worker/worker/build.gradle',
                     "apply from: '../../shared/gameval/gameval.gradle.kts'\n")
        self.assertEqual(found(self.t), [('worker', 'gameval', 'build', 1,
                                          '../../shared/gameval/gameval.gradle.kts')])

    def test_comments_and_interpolated_paths_are_skipped(self):
        self.t.write('worker/worker/build.gradle.kts',
                     '// implementation(project(":api"))\n'
                     '/* val d = rootProject.file("tools/x.txt") */\n'
                     'val u = uri("https://repo.example/x") // project(":gameval")\n'
                     'val p = rootProject.file("$name/x")\n')
        self.assertEqual(found(self.t), [])

    def test_a_root_build_script_and_settings_are_not_read(self):
        self.t.write('build.gradle.kts', 'dependencies { implementation(project(":api")) }\n')
        self.assertEqual(found(self.t), [])

    def test_the_check_runs_the_gradle_detector(self):
        self.t.write('worker/worker/build.gradle.kts', 'dependencies { api(project(":api")) }\n')
        out = C.check(ModuleMap(MAP), {}, self.t.files(), self.t.read())
        self.assertIn('REFUSED  worker -> api  build  worker/worker/build.gradle.kts:1  '
                      '(gradle: :api)', out.lines)


if __name__ == '__main__':
    unittest.main()
