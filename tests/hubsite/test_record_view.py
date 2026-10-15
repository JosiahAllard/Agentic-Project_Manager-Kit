"""kitlib.hubsite.backlog: one record as the record page reads it (Views.record), and what a new
record may hold (Views.record_template). Names from a URL are checked before any file or git is
touched, and every refusal is a fixed phrase that names no path."""
import hashlib
import os
import unittest
from unittest import mock

from kitlib.backlog import record as brecord
from kitlib.git import cmd as gitcmd
from kitlib.hubsite import backlog
from tests.hubsite.test_backlog_views import Case, record

BAD_IDS = ("..", "../A", "A/..", "a/b", "a\\b", "%2e%2e", "%2F", "A..B", ".A", "-A", "", " A",
           "A B", "A:B", "C:", "A" * 129, "a\x00b", None, 7)
BAD_REPOS = ("..", "../r", "r/x", "r\\x", "%2e%2e", ".r", "", "r x", "C:", "r" * 101, None)


class RecordCase(Case):

    def views(self, *pairs, **kw):
        return Case.views(self, *pairs, **kw)

    def read(self, repo, rid, name="r"):
        return self.views((name, repo.root)).record(name, rid)

    def assert_refused(self, call, code, said, field):
        with self.assertRaises(backlog.RecordError) as got:
            call()
        self.assertEqual((got.exception.code, got.exception.said, got.exception.field),
                         (code, said, field))
        return got.exception


class AnOpenRecord(RecordCase):

    def test_an_open_record_is_its_fields_in_file_order_its_body_and_its_version(self):
        text = record("A", "first", priority="2", blocked_on="item:B") + "blocked_on: owner\n\nThe body.\n"
        repo = self.main_repo(A=text)
        got = self.read(repo, "A")
        commit = repo.head()
        self.assertEqual(got["repo"], "r")
        self.assertEqual(got["id"], "A")
        self.assertEqual(got["where"], "open")
        self.assertTrue(got["readable"])
        self.assertEqual(got["fields"], [["id", "A"], ["summary", "first"], ["priority", "2"],
                                         ["blocked_on", "item:B"], ["blocked_on", "owner"]])
        self.assertEqual(got["body"], "The body.\n")
        self.assertEqual(got["version"],
                         "%s:%s" % (commit, hashlib.sha1(text.encode("utf-8")).hexdigest()))
        self.assertEqual(got["blockers"], [
            {"value": "item:B", "kind": brecord.blocker_kind("item:B"), "target": "B"},
            {"value": "owner", "kind": brecord.blocker_kind("owner"), "target": None}])
        self.assertEqual(got["hold"], {"owner_ask": None, "hold_ruling": None, "q": None})
        self.assertIsNone(got["branch"])
        self.assertIsNone(got["removed"])

    def test_the_version_changes_with_the_text_and_the_store_commit(self):
        repo = self.main_repo(A=record("A", "first"), B=record("B", "other"))
        v1 = self.read(repo, "A")["version"]
        self.put(repo, "touch B", B=record("B", "changed"))
        v2 = self.read(repo, "A")["version"]
        self.put(repo, "change A", A=record("A", "second"))
        v3 = self.read(repo, "A")["version"]
        self.assertEqual(len({v1, v2, v3}), 3)
        self.assertEqual(v1.split(":")[1], v2.split(":")[1])    # same text, later commit

    def test_the_repository_name_matches_without_case_and_answers_the_configured_one(self):
        repo = self.main_repo(A=record("A", "first"))
        got = self.views(("Repo", repo.root)).record("rEPO", "A")
        self.assertEqual(got["repo"], "Repo")

    def test_a_held_record_shows_its_hold(self):
        repo = self.main_repo(A=record("A", "held", owner_ask="pick one", q="Q-123"))
        self.assertEqual(self.read(repo, "A")["hold"],
                         {"owner_ask": "pick one", "hold_ruling": None, "q": "Q-123"})

    def test_a_file_that_is_not_a_record_is_no_such_record(self):
        repo = self.main_repo(README="just words\n")
        self.assert_refused(lambda: self.read(repo, "README"), "not_found", backlog.NO_RECORD, "id")

    def test_an_unreadable_record_is_shown_as_unreadable(self):
        repo = self.main_repo(A="id: A\nnot a header line\n\nbody\n")
        got = self.read(repo, "A")
        self.assertFalse(got["readable"])
        self.assertEqual(got["fields"], [["id", "A"]])


class TheBranch(RecordCase):

    def test_a_pulled_record_shows_its_live_branch_and_worktree(self):
        repo = self.main_repo(A=record("A", "first", state="pulled", pulled_by="kit/work"))
        wt = repo.worktree("work")
        repo.write("x.txt", "x\n", root=wt)
        tip = repo.commit("work on it", "x.txt", cwd=wt)
        branch = self.read(repo, "A")["branch"]
        self.assertEqual(branch["name"], "kit/work")
        self.assertEqual(branch["source"], "pulled_by")
        self.assertIs(branch["exists"], True)
        self.assertTrue(tip.startswith(branch["tip"]))
        self.assertEqual(branch["subject"], "work on it")
        self.assertRegex(branch["date"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(branch["worktree"], ".claude/worktrees/work")
        self.assertEqual((branch["ahead"], branch["behind"]), (1, 0))
        self.assertIsNone(branch["error"])
        self.put(repo, "main moves", B=record("B", "other"))
        branch = self.read(repo, "A")["branch"]
        self.assertEqual((branch["ahead"], branch["behind"]), (1, 1))

    def test_a_pulled_record_whose_branch_is_gone_says_so(self):
        repo = self.main_repo(A=record("A", "first", state="pulled", pulled_by="kit/gone"))
        branch = self.read(repo, "A")["branch"]
        self.assertEqual((branch["name"], branch["exists"], branch["tip"], branch["error"]),
                         ("kit/gone", False, None, None))

    def test_a_parked_record_with_parked_from_shows_that_branch(self):
        repo = self.main_repo(A=record("A", "first", state="parked", q="Q-123",
                                       parked_from="kit/work"))
        repo.worktree("work")
        branch = self.read(repo, "A")["branch"]
        self.assertEqual((branch["name"], branch["source"], branch["exists"]),
                         ("kit/work", "parked_from", True))

    def test_fallback_a_parked_record_with_no_parked_from_shows_its_last_pulled_by(self):
        repo = self.main_repo(A=record("A", "first", state="pulled", pulled_by="kit/older"))
        self.put(repo, "pull again", A=record("A", "first", state="pulled", pulled_by="kit/work"))
        self.put(repo, "unrelated", B=record("B", "other"))
        self.put(repo, "backlog: park A on Q-123",
                 A=record("A", "first", state="parked", q="Q-123"))
        repo.worktree("work")
        branch = self.read(repo, "A")["branch"]
        self.assertEqual((branch["name"], branch["source"], branch["exists"]),
                         ("kit/work", "history", True))

    def test_fallback_reads_at_most_the_cap_of_versions(self):
        repo = self.main_repo(A=record("A", "first", state="pulled", pulled_by="kit/work"))
        self.put(repo, "backlog: park A on Q-123",
                 A=record("A", "first", state="parked", q="Q-123"))
        self.put(repo, "edit while parked",
                 A=record("A", "first", state="parked", q="Q-123", priority="2"))
        path = "docs/backlog/A.md"
        self.assertEqual(backlog.last_pull(repo.root, repo.head(), path), "kit/work")
        self.assertIsNone(backlog.last_pull(repo.root, repo.head(), path, cap=2))

    def test_fallback_never_shows_a_pull_given_up_before_the_parking(self):
        # Pulled on a branch, released to open, parked later: the record was never parked from
        # that branch, so it shows none.
        repo = self.main_repo(**{"EX-7": record("EX-7", "first", state="pulled",
                                                pulled_by="kit/work")})
        self.put(repo, "backlog: release EX-7", **{"EX-7": record("EX-7", "first", state="open")})
        self.put(repo, "backlog: park EX-7 on Q-123",
                 **{"EX-7": record("EX-7", "first", state="parked", q="Q-123")})
        repo.worktree("work")
        self.assertIsNone(backlog.last_pull(repo.root, repo.head(), "docs/backlog/EX-7.md"))
        self.assertIsNone(self.read(repo, "EX-7")["branch"])
        # A version with no state line is open too.
        self.put(repo, "backlog: no state", **{"EX-7": record("EX-7", "first")})
        self.put(repo, "backlog: park EX-7 again",
                 **{"EX-7": record("EX-7", "first", state="parked", q="Q-123")})
        self.assertIsNone(self.read(repo, "EX-7")["branch"])

    def test_a_parked_record_never_pulled_has_no_branch(self):
        repo = self.main_repo(A=record("A", "first", state="parked", q="Q-123"))
        self.assertIsNone(self.read(repo, "A")["branch"])

    def test_an_open_record_has_no_branch_even_with_a_pulled_by(self):
        repo = self.main_repo(A=record("A", "first", state="open", pulled_by="kit/work"))
        self.assertIsNone(self.read(repo, "A")["branch"])

    def test_a_branch_name_git_could_misread_is_never_asked(self):
        repo = self.main_repo(A=record("A", "first"))
        with mock.patch.object(gitcmd, "call", side_effect=AssertionError("git was asked")):
            for bad in ("-x", "--all", "a..b", "a//b", "a/", "a.lock", "a.", "a b", "a:b",
                        "a^{commit}", "refs/../x", ""):
                with self.subTest(name=bad):
                    got = backlog.branch_block(repo.root, bad, "pulled_by")
                    self.assertEqual((got["error"], got["exists"]), (backlog.BAD_BRANCH, None))

    def test_a_branch_git_cannot_read_fails_the_block_not_the_record(self):
        repo = self.main_repo(A=record("A", "first", state="pulled", pulled_by="kit/work"))
        real = gitcmd.call

        def broken(args, cwd, *a, **kw):
            if "refs/heads/kit/work^{commit}" in args:
                return gitcmd.Result(None, b"", "git rev-parse timed out after 30 seconds")
            return real(args, cwd, *a, **kw)
        with mock.patch.object(gitcmd, "call", side_effect=broken):
            got = self.read(repo, "A")
        self.assertEqual(got["where"], "open")
        self.assertEqual(got["branch"]["error"], backlog.BRANCH_FAILED)


class ACompletedRecord(RecordCase):

    def removed_by(self, subject):
        repo = self.main_repo(A=record("A", "done", state="open") + "\nwhat it was\n")
        self.put(repo, subject, A=None)
        return repo, self.read(repo, "A")

    def test_a_closed_record_shows_how_when_and_its_last_text(self):
        repo, got = self.removed_by("closes A: it works")
        self.assertEqual(got["where"], "completed")
        self.assertEqual(got["removed"]["how"], "closed")
        self.assertEqual(got["removed"]["commit"], repo.head())
        self.assertEqual(got["removed"]["subject"], "closes A: it works")
        self.assertRegex(got["removed"]["at"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(got["fields"], [["id", "A"], ["summary", "done"], ["state", "open"]])
        self.assertEqual(got["body"], "what it was\n")
        self.assertIsNone(got["branch"])

    def test_retired_deleted_and_any_other_removal(self):
        for subject, how in (("retires A: not needed", "retired"),
                             ("backlog: delete A: a mistake", "deleted"),
                             ("tidy the store", "removed")):
            with self.subTest(subject=subject):
                self.assertEqual(self.removed_by(subject)[1]["removed"]["how"], how)

    def test_a_record_closed_on_a_merged_branch_names_the_branch_commit(self):
        repo = self.main_repo(A=record("A", "done"), C=record("C", "stays"))
        wt = repo.worktree("work")
        os.remove(os.path.join(wt, "docs", "backlog", "A.md"))
        closing = repo.commit("closes A: done on the branch", "docs/backlog/A.md", cwd=wt)
        self.put(repo, "meanwhile", B=record("B", "other"))
        repo.git("merge", "-q", "--no-ff", "-m", "Merge kit/work", "kit/work")
        got = self.read(repo, "A")
        self.assertEqual((got["removed"]["how"], got["removed"]["commit"]), ("closed", closing))

    def test_a_legacy_closed_line_is_completed(self):
        repo = self.main_repo(A=record("A", "done", closed="2026-01-02"))
        got = self.read(repo, "A")
        self.assertEqual((got["where"], got["removed"]),
                         ("completed", {"how": "closed", "commit": None, "at": "2026-01-02",
                                        "subject": None}))

    def test_the_search_for_a_removal_walks_at_most_the_cap_of_commits(self):
        repo = self.main_repo(**{"EX-9": record("EX-9", "done")})
        self.put(repo, "closes EX-9: done", **{"EX-9": None})
        closing = repo.head()
        for n in range(3):
            self.put(repo, "later %d" % n, **{"EX-%d" % (20 + n): record("EX-%d" % (20 + n), "x")})
        path = "docs/backlog/EX-9.md"
        self.assertTrue(backlog.removal(repo.root, repo.head(), path, cap=4).startswith(closing))
        self.assertEqual(backlog.removal(repo.root, repo.head(), path, cap=3), "")
        with mock.patch.object(backlog, "REMOVAL_CAP", 3):
            self.assert_refused(lambda: self.read(repo, "EX-9"), "not_found", backlog.NO_RECORD,
                                "id")
        self.assertEqual(self.read(repo, "EX-9")["removed"]["commit"], closing)

    def test_an_id_never_held_asks_git_for_a_bounded_walk_only(self):
        repo = self.main_repo(**{"EX-1": record("EX-1", "first")})
        real, asked = gitcmd.run_text, []

        def recording(args, cwd, *a, **kw):
            asked.append(list(args))
            return real(args, cwd, *a, **kw)
        with mock.patch.object(gitcmd, "run_text", side_effect=recording):
            self.assert_refused(lambda: self.read(repo, "EX-404"), "not_found",
                                backlog.NO_RECORD, "id")
        logs = [a for a in asked if a and a[0] == "log"]
        self.assertEqual(len(logs), 1)
        self.assertIn("-n", logs[0])
        self.assertEqual(logs[0][logs[0].index("-n") + 1], str(backlog.REMOVAL_CAP))
        # A pathspec or a --diff-filter would make -n count matches, not commits walked.
        self.assertNotIn("--", logs[0])
        self.assertFalse([a for a in logs[0] if a.startswith("--diff-filter")])

    def test_a_record_added_again_after_removal_is_open(self):
        repo, _ = self.removed_by("closes A: it works")
        self.put(repo, "back again", A=record("A", "again"))
        got = self.read(repo, "A")
        self.assertEqual((got["where"], got["removed"]), ("open", None))


class WhatIsRefused(RecordCase):

    def test_an_id_the_store_never_held_is_no_such_record(self):
        repo = self.main_repo(A=record("A", "first"))
        self.assert_refused(lambda: self.read(repo, "EX-404"), "not_found", backlog.NO_RECORD, "id")

    def test_a_repository_not_configured_is_repo_not_known(self):
        repo = self.main_repo(A=record("A", "first"))
        v = self.views(("r", repo.root))
        self.assert_refused(lambda: v.record("other", "A"), "not_found", backlog.NO_REPO, "repo")
        self.assert_refused(lambda: v.record_template("other"), "not_found", backlog.NO_REPO,
                            "repo")

    def test_a_name_that_is_no_id_or_repository_is_refused_before_anything_is_read(self):
        def never(*a, **kw):
            raise AssertionError("read before the name was checked")
        v = backlog.Views(never, opener=never, reuse_s=0)
        with mock.patch.object(gitcmd, "call", side_effect=never), \
                mock.patch.object(backlog.hubbacklog, "blobs", side_effect=never):
            for bad in BAD_IDS:
                with self.subTest(id=bad):
                    e = self.assert_refused(lambda: v.record("r", bad), "invalid",
                                            backlog.BAD_ID, "id")
                    self.assertNotIn(str(bad), str(e)) if bad else None
            for bad in BAD_REPOS:
                with self.subTest(repo=bad):
                    self.assert_refused(lambda: v.record(bad, "A"), "invalid", backlog.BAD_REPO,
                                        "repo")
                    self.assert_refused(lambda: v.record_template(bad), "invalid",
                                        backlog.BAD_REPO, "repo")

    def test_a_record_outside_the_store_folder_cannot_be_named(self):
        repo = self.main_repo(A=record("A", "first"))
        repo.write("README2.md", record("README2", "outside"))
        repo.commit("outside the store", "README2.md")
        for rid in ("../../README2", "..%2F..%2FREADME2"):
            with self.subTest(id=rid):
                self.assert_refused(lambda: self.read(repo, rid), "invalid", backlog.BAD_ID, "id")

    def test_no_checkout_and_a_missing_folder_are_read_errors_naming_no_path(self):
        repo = self.main_repo(A=record("A", "first"))
        gone = os.path.join(repo.root, "no-such-folder")
        for pairs, said in (([("r", None)], backlog.NO_CHECKOUT),
                            ([("r", gone)], backlog.NO_FOLDER)):
            v = self.views(*pairs)
            for call in (lambda: v.record("r", "A"), lambda: v.record_template("r")):
                with self.subTest(said=said):
                    with self.assertRaises(backlog.ReadError) as got:
                        call()
                    self.assertEqual(got.exception.said, said)
                    self.assertNotIn(repo.base, str(got.exception))
                    self.assertTrue(backlog.fixed(backlog.say(got.exception)))

    def test_a_store_that_cannot_be_opened_is_a_fixed_phrase(self):
        repo = self.main_repo(A=record("A", "first"))
        secret = os.path.join(repo.root, "secret-path")

        def opener(root, fetch=False):
            raise RuntimeError("git log: fatal: %s is broken" % secret)
        v = self.views(("r", repo.root), opener=opener)
        with self.assertRaises(backlog.ReadError) as got:
            v.record("r", "A")
        self.assertTrue(backlog.fixed(got.exception.said))
        self.assertNotIn("secret-path", str(got.exception))

    def test_a_repository_list_that_cannot_be_read_is_a_fixed_phrase(self):
        def broken():
            raise OSError("C:\\somewhere\\repos.json is locked")
        v = backlog.Views(broken, reuse_s=0)
        with self.assertRaises(backlog.ReadError) as got:
            v.record("r", "A")
        self.assertEqual(got.exception.said, backlog.NO_REPOS)
        self.assertTrue(backlog.fixed(backlog.NO_REPOS))
        self.assertEqual(backlog.say(got.exception), backlog.NO_REPOS)  # not READ_FAILED
        self.assertNotIn("somewhere", backlog.say(got.exception))

    def test_a_store_with_no_ref_yet_has_no_records(self):
        repo = self.main_repo(A=record("A", "first"))
        v = self.views(("r", repo.root), opener=lambda root, fetch=False: (None, "x", False))
        self.assert_refused(lambda: v.record("r", "A"), "not_found", backlog.NO_RECORD, "id")


class TheTemplate(RecordCase):

    def test_the_template_names_every_field_and_the_legal_values(self):
        repo = self.main_repo(A=record("A", "first"))
        got = self.views(("R", repo.root)).record_template("r")
        self.assertEqual(got["repo"], "R")
        self.assertEqual(got["id_grammar"], "upper")
        self.assertEqual(got["id_pattern"], brecord.ID_GRAMMARS["upper"])
        self.assertEqual([f["key"] for f in got["fields"]], list(brecord.FIELDS))
        by_key = {f["key"]: f for f in got["fields"]}
        self.assertEqual((by_key["id"]["count"], by_key["id"]["required"]), (brecord.ONE, True))
        self.assertIs(by_key["blocked_on"]["required"], brecord.FIELDS["blocked_on"][0] in
                      (brecord.ONE, brecord.ONE_PLUS))
        self.assertEqual(by_key["pulled_by"]["written_by"],
                         __import__("kitlib.backlog.plan", fromlist=["x"]).WRITTEN_BY.get("pulled_by"))
        self.assertEqual(got["states"], list(brecord.STATES))
        self.assertEqual(got["blocked_on"], {"plain": list(brecord.BLOCKED_ON_PLAIN),
                                             "prefixes": list(brecord.BLOCKED_ON_PREFIXES)})
        self.assertEqual(got["efforts"], ["s", "m", "l", "-"])
        self.assertIn(brecord.OWNER_PRIORITY, got["priorities"])
        self.assertEqual(got["priority_by"], ["owner", "pm", "agent", "-"])

    def test_the_template_follows_the_repositorys_id_grammar_and_phases(self):
        repo = self.main_repo(A=record("A", "first"))
        repo.write(".kit.json", '{"backlog": {"store": "main", "id_grammar": "mixed", '
                                '"phases": ["P1", "P2"]}}')
        got = self.views(("r", repo.root)).record_template("r")
        self.assertEqual((got["id_grammar"], got["phases"]), ("mixed", ["P1", "P2"]))

    def test_a_kit_json_that_cannot_be_read_is_a_fixed_phrase(self):
        repo = self.main_repo(A=record("A", "first"))
        repo.write(".kit.json", "{not json")
        with self.assertRaises(backlog.ReadError) as got:
            self.views(("r", repo.root)).record_template("r")
        self.assertEqual(got.exception.said, backlog.BAD_STORE)


if __name__ == "__main__":
    unittest.main()
