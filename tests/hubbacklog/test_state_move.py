"""hub_backlog's state file across the machine config folder's move (docs/kit-json.md "Moving the
machine config folder"; docs/action-hub.md "backlog/<repo> and completed/<repo>"): the first run
after the move reads the retired folder's state, so each write stays pinned to the version the
store holds, and writes the new folder's. Every case runs main() under a scratch home folder."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import hubbacklog, pmstart
from kitlib.config import migration, user

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "hosts", "claude", "shell", "hub"))
import hub_backlog                                                  # noqa: E402

DOCS = ({"repo": "r", "items": [{"id": "A"}]}, {"repo": "r", "days": 90, "items": []})
STATE_NAME = "hub_backlog_state.json"      # the file's name, as docs/action-hub.md gives it


class StateAcrossTheMove(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="kit-hub-state-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = os.path.join(self.base, "home")
        self.new = os.path.join(self.home, user.DIR_NAME, STATE_NAME)
        self.old = os.path.join(self.home, migration.OLD_DIR_NAME, STATE_NAME)
        self.out = os.path.join(self.base, "batch.json")
        real_plan = hub_backlog.plan
        for p in (mock.patch.dict(os.environ, {"USERPROFILE": self.home, "HOME": self.home}),
                  mock.patch.object(pmstart, "load_config", return_value={"sessions_file": "s.json"}),
                  mock.patch.object(pmstart, "known_repos", return_value=["r"]),
                  mock.patch.object(hubbacklog, "git", return_value=b"pm-root"),
                  mock.patch.object(hubbacklog, "repo_root", return_value="/r"),
                  mock.patch.object(hub_backlog, "plan",
                                    lambda names, roots, state, now, force=False:
                                    real_plan(names, roots, state, now, snap=lambda *a, **k: DOCS, force=force))):
            p.start()
            self.addCleanup(p.stop)

    def put(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def load(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return hub_backlog.main(argv)

    def batch(self):
        return {(w["collection"], w["doc_id"]): w for w in self.load(self.out)}

    def test_the_first_run_after_the_move_reads_the_old_state_and_writes_the_new(self):
        old_state = {"backlog/r": {"d": "stale", "v": 4}, "completed/r": {"d": hubbacklog.digest(DOCS[1]), "v": 2}}
        self.put(self.old, old_state)
        self.assertEqual(os.path.basename(hub_backlog.STATE), STATE_NAME)
        self.assertEqual(self.main(["--out", self.out]), 0)
        b = self.batch()
        self.assertEqual(list(b), [("backlog", "r")])                  # completed/r unchanged: not re-sent
        self.assertEqual(b[("backlog", "r")]["if_version"], 4)         # pinned, so the store takes it
        self.assertEqual(self.load(self.new)["backlog/r"]["v"], 5)
        self.assertEqual(self.load(self.new)["completed/r"], old_state["completed/r"])
        self.assertEqual(self.load(self.old), old_state)               # the old file is never written

    def test_the_new_state_wins_once_it_exists(self):
        self.put(self.old, {"backlog/r": {"d": "stale", "v": 4}})
        self.put(self.new, {"backlog/r": {"d": "stale", "v": 9}})
        self.assertEqual(self.main(["--out", self.out]), 0)
        self.assertEqual(self.batch()[("backlog", "r")]["if_version"], 9)

    def test_a_given_state_file_is_read_and_written(self):
        given = os.path.join(self.base, "given.json")
        self.put(self.old, {"backlog/r": {"d": "stale", "v": 4}})
        self.put(given, {"backlog/r": {"d": "stale", "v": 7}})
        self.assertEqual(self.main(["--out", self.out, "--state", given]), 0)
        self.assertEqual(self.batch()[("backlog", "r")]["if_version"], 7)
        self.assertEqual(self.load(given)["backlog/r"]["v"], 8)
        self.assertFalse(os.path.exists(self.new))


class StatePaths(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="kit-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.new = os.path.join(self.home, user.DIR_NAME, "s.json")
        self.old = os.path.join(self.home, migration.OLD_DIR_NAME, "s.json")

    def put(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")

    def test_neither_reads_and_writes_the_new_file(self):
        self.assertEqual(migration.state_paths("s.json", self.home), (self.new, self.new))

    def test_the_old_file_alone_is_read_and_the_new_one_written(self):
        self.put(self.old)
        self.assertEqual(migration.state_paths("s.json", self.home), (self.old, self.new))

    def test_the_new_file_wins(self):
        self.put(self.old)
        self.put(self.new)
        self.assertEqual(migration.state_paths("s.json", self.home), (self.new, self.new))


class BadMachineConfig(unittest.TestCase):

    def test_a_bad_location_is_exit_2_not_no_config(self):
        base = tempfile.mkdtemp(prefix="kit-home-")
        self.addCleanup(shutil.rmtree, base, True)
        with open(os.path.join(base, user.DIR_NAME), "w", encoding="utf-8") as f:
            f.write("a file where the folder goes")
        with mock.patch.dict(os.environ, {"USERPROFILE": base, "HOME": base}), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(hub_backlog.main(["--out", os.path.join(base, "b.json")]), 2)
        said = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("machine config", said)
        self.assertIn("not a folder", said)
        self.assertFalse(os.path.exists(os.path.join(base, "b.json")))


if __name__ == "__main__":
    unittest.main()
