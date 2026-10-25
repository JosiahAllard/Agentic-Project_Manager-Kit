"""The PM start checklist's rulebooks line (kitlib.pmstart.rulebook_lines; docs/pm-start.md)."""
import os
import shutil
import tempfile
import unittest

from kitlib import pmstart


class RulebookLinesTest(unittest.TestCase):
    def folder(self, books):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        rules = os.path.join(d, "rules")
        os.makedirs(rules)
        for name, text in books.items():
            with open(os.path.join(rules, name + ".md"), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        return d, rules

    def test_counts_and_the_all_command(self):
        d, rules = self.folder({
            "all": "# Shared\n\n## T\n- a.one: x\n  (from R-0001)\n- a.two: y\n  (from R-0001)\n",
            "kit": "# Kit\n\nRepos: kit\n\n## T\n- k.one: x\n  (from R-0001)\n"})
        line, = pmstart.rulebook_lines({"ledger_dir": d})
        self.assertIn("rulebooks (standing rules, read before deciding) in %s" % rules, line)
        self.assertIn("kit 1, shared 2 rules", line)
        self.assertTrue(line.endswith('tools/ledger.py" rules --all'), line)

    def test_no_ledger_configured_says_so(self):
        line, = pmstart.rulebook_lines({})
        self.assertIn("none", line)
        self.assertIn("no ledger_dir or ledger_path is set", line)

    def test_no_rulebook_file_says_so(self):
        d, rules = self.folder({})
        self.assertEqual(pmstart.rulebook_lines({"ledger_dir": d}),
                         ["rulebooks (standing rules): none in %s" % rules])
