"""review_pack and the -C paths: forward slashes out, and a -C whose backslashes the shell ate
(`Z:GitHub...`) refused with the forward-slash form named."""
import contextlib
import io

from kitlib.preflight import paths
from tests.reviewpack.repo import PackCase, cli


class TheMangledC(PackCase):
    def setUp(self):
        super().setUp()
        self.git('checkout', '-q', '-b', 'feature')
        self.write('f.txt', '1')
        self.commit_all('change')

    def test_a_c_with_the_backslashes_eaten_is_refused_naming_the_fix(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['-C', 'Z:GitHubnowhereatall', 'feature'])
        self.assertEqual(code, 2)
        self.assertIn('Instead: pass forward slashes, as -C Z:/GitHubnowhereatall', out.getvalue())

    def test_real_paths_in_either_slash_are_not_flagged(self):
        for where in (self.root, self.root.replace('\\', '/')):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(['-C', where, 'feature', '--base', 'main'])
            self.assertNotIn('Instead: pass forward', out.getvalue())
            self.assertEqual(code, 0, out.getvalue())

    def test_the_output_has_no_backslash_path(self):
        code, out = self.run_pack('feature', '--base', 'main')
        self.assertNotRegex(out, r'[A-Za-z]:\\')


class TheHelper(PackCase):
    def test_forward_and_mangled(self):
        self.assertEqual(paths.forward('at Z:\\a\\b c', 'Z:\\a\\b c'), 'at Z:/a/b c')
        self.assertIsNone(paths.mangled_dir('Z:/GitHub/x'))
        self.assertIsNone(paths.mangled_dir('Z:\\GitHub\\x'))
        self.assertEqual(paths.mangled_dir('Z:GitHub\\x'), 'Z:/GitHub/x')
