import unittest

from tests.shipped import ROOT, violations

KNOWLEDGE = ROOT / "knowledge" / "asset-generation"


class KnowledgeTest(unittest.TestCase):
    def test_base_files_present(self):
        for name in ("README.md", "BEST_PRACTICES.md", "MODELS.md"):
            self.assertTrue((KNOWLEDGE / name).is_file(), name)

    def test_no_repo_or_owner_specifics(self):
        self.assertEqual(violations(KNOWLEDGE), [])

    def test_no_logs_or_samples(self):
        names = sorted(p.relative_to(KNOWLEDGE).as_posix() for p in KNOWLEDGE.rglob("*") if p.is_file())
        self.assertEqual(names, ["BEST_PRACTICES.md", "MODELS.md", "README.md"])


class OptionalPackagesTest(unittest.TestCase):
    def test_no_repo_or_owner_specifics(self):
        self.assertEqual(violations(ROOT / "optional"), [])
        self.assertEqual(violations(ROOT / "tests" / "optional"), [])


if __name__ == "__main__":
    unittest.main()
