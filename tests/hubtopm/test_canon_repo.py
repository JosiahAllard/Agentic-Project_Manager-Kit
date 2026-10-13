"""The Action Hub's repo-tag mapping, `canonRepo` (docs/action-hub.md "repos/<name>").

An item's `repo` tag maps to the repo whose repos/ doc has that name, or lists it in `aliases`;
the page holds no alias of its own. The function is cut out of hosts/claude/shell/hub/action-hub.html and run
under node against a stub `state`.
"""
import json
import re
import shutil
import subprocess
import unittest

from tests.hubtopm.test_hubtopm import page

NODE = shutil.which("node")


def canon(repos, tags):
    m = re.search(r"  function canonRepo\(r\)\{.*?return hit \? named\(hit\) : \"\"; \}", page(), re.S)
    assert m, "canonRepo missing from hosts/claude/shell/hub/action-hub.html"
    src = ("const state = {repos: %s};\n%s\nconsole.log(JSON.stringify(%s.map(canonRepo)));"
           % (json.dumps(repos), m.group(0), json.dumps(tags)))
    out = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class CanonRepo(unittest.TestCase):
    def test_no_aliases_maps_only_real_names(self):
        repos = [{"id": "alpha", "name": "Alpha"}, {"id": "beta"}]
        self.assertEqual(canon(repos, ["alpha", "BETA", "a", "machine", "", None]),
                         ["Alpha", "beta", "", "", "", ""])

    def test_aliases_from_the_repo_doc(self):
        repos = [{"id": "alpha", "name": "Alpha", "aliases": ["a", "Old-Alpha"]},
                 {"id": "beta", "name": "beta", "aliases": "not-a-list"}]
        self.assertEqual(canon(repos, ["A", "old-alpha", "not-a-list", "beta"]),
                         ["Alpha", "Alpha", "", "beta"])

    def test_a_real_name_wins_over_an_alias(self):
        repos = [{"name": "alpha", "aliases": ["beta"]}, {"name": "beta"}]
        self.assertEqual(canon(repos, ["beta"]), ["beta"])

    def test_the_page_names_no_alias(self):
        self.assertNotRegex(page(), r"\{\s*pm\s*:")
        # A repository's own name or alias in the page is the scrub's to catch (tests/scrub/
        # test_generic.py, the install names), so this test names none.


if __name__ == "__main__":
    unittest.main()
