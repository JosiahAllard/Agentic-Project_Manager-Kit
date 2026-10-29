"""The lock's hardware look on Linux (docs/linux-support-plan.md section 2.4, slice 6 of section 8):
kitlib.platform.linux.hardware reads /sys and /proc, layout.detect_linux turns that into detect()'s
dict, and the four synthetic trees of tests/lock/linux_trees.py divide with no shared id. These
run on any platform: the reader is pointed at a tree in a temp folder. LiveSys reads the running
Linux machine's own /sys."""
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import kitlib.platform
from kitlib.lock import api
from kitlib.lock import layout as lay
from kitlib.platform.linux import hardware
from tests.lock import linux_trees
from tests.platforms import posix_only

DIVISIONS = ({}, {"cpu_slot_threads": 1}, {"cpu_slot_threads": 2}, {"cpu_slot_threads": 3})


def smi_runner(answer):
    """A subprocess.run stand-in for nvidia-smi: `answer` as its output, or a missing program."""
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if answer is None:
            raise FileNotFoundError(2, "No such file or directory", args[0])
        return subprocess.CompletedProcess(args, 0, stdout=answer, stderr="")
    run.calls = calls
    return run


class TreeCase(unittest.TestCase):
    def tree(self, name, drop=(), extra=None):
        """The folder of tree `name`, without the files `drop` names (prefixes), plus `extra`."""
        t = linux_trees.TREES[name]
        files = {k: v for k, v in t["files"].items() if not k.startswith(tuple(drop))}
        files.update(extra or {})
        folder = tempfile.mkdtemp(prefix="linux-tree-")
        self.addCleanup(_remove, folder)
        return linux_trees.write(files, folder)

    def look(self, name, **kw):
        t = linux_trees.TREES[name]
        return lay.detect_linux(root=self.tree(name, **kw), affinity=set(t["affinity"]),
                                run=smi_runner(t["smi"]))


def _remove(folder):
    for top, dirs, files in os.walk(folder, topdown=False):
        for f in files:
            os.remove(os.path.join(top, f))
        for d in dirs:
            os.rmdir(os.path.join(top, d))
    os.rmdir(folder)


def assert_divides(case, hw, allowed):
    """Every division of hw: no id in two slots, none outside `allowed`, and the default covers it."""
    for d in DIVISIONS:
        with case.subTest(division=d):
            L = lay.derive(hw, dict(d))
            flat = [i for s in L["slots_cpu"] for i in s]
            case.assertEqual(len(flat), len(set(flat)), "an id in two slots: %r" % L["slots_cpu"])
            case.assertLessEqual(set(flat), set(allowed))
            for a in range(len(L["cpu_masks"])):
                for b in range(a + 1, len(L["cpu_masks"])):
                    case.assertEqual(L["cpu_masks"][a] & L["cpu_masks"][b], 0)
            if not d:
                case.assertEqual(set(flat), set(allowed))
            case.assertTrue(lay.valid_layout(L))


class FourTrees(TreeCase):
    """Each tree reads as the kernel's layout says and divides with no shared id."""

    def check(self, name):
        want = linux_trees.TREES[name]["expect"]
        hw = self.look(name)
        self.assertEqual(hw["failed"], [], hw["problems"])
        self.assertEqual(hw["threads"], want["threads"])
        self.assertEqual([c["threads"] for c in hw["cores"]], want["cores"])
        self.assertEqual([c["efficiency"] for c in hw["cores"]], want["efficiency"])
        self.assertEqual(hw["l3"], want["l3"])
        self.assertEqual(hw["memory_mb"], want["memory_mb"])
        self.assertEqual([(g["name"], g["vendor_id"], g["vram_mb"], g["default_share"])
                          for g in hw["gpus"]], want["gpus"])
        self.assertEqual(hw["cpu_sets"], [])
        self.assertEqual(hw["source"]["cpu"], "linux")
        self.assertEqual([t for g in hw["groups"] for t in g["threads"]], want["threads"])
        assert_divides(self, hw, want["threads"])
        return hw

    def test_desktop(self):
        hw = self.check("desktop")
        self.assertEqual(lay.derive(hw)["slots_cpu"][:2], [[0, 8], [1, 9]])
        self.assertEqual(hw["gpus"][0]["luid"], "pci:0000:01:00.0")

    def test_hybrid(self):
        hw = self.check("hybrid")
        self.assertTrue(all(c["smt"] for c in hw["cores"][:6]))
        self.assertFalse(any(c["smt"] for c in hw["cores"][6:]))

    def test_gaps(self):
        hw = self.check("gaps")
        self.assertEqual(lay.derive(hw)["slots_cpu"],
                         [[0], [1], [2], [3], [6, 14], [7], [12], [13]])
        self.assertTrue(hw["cores"][0]["smt"], "an offline sibling still makes the core SMT")

    def test_container(self):
        hw = self.check("container")
        self.assertEqual(lay.derive(hw)["cpu_slots"], 4)

    def test_no_two_trees_need_a_real_machine(self):
        self.assertEqual(sorted(linux_trees.TREES), ["container", "desktop", "gaps", "hybrid"])


class Reader(TreeCase):
    """The pieces of the reader on their own."""

    def test_cpulist(self):
        self.assertEqual(hardware.parse_cpulist("0-3,8,10-11\n"), [0, 1, 2, 3, 8, 10, 11])
        self.assertEqual(hardware.parse_cpulist("\n"), [])
        with self.assertRaises(ValueError):
            hardware.parse_cpulist("0-x")

    def test_the_cgroup_cuts_what_the_affinity_allows(self):
        root = self.tree("container")
        self.assertEqual(hardware.allowed(root, set(range(32))), [4, 5, 6, 7, 20, 21, 22, 23])
        self.assertEqual(hardware.allowed(root, {4, 5}), [4, 5])

    def test_the_memory_is_capped_by_a_parent_cgroups_max(self):
        self.assertEqual(hardware.memory(self.tree("container")), {"total_mb": 8192, "free_mb": 7168})
        self.assertEqual(hardware.memory(self.tree("desktop")), {"total_mb": 32000, "free_mb": 29296})

    def test_nvidia_smi_is_asked_by_bus_id(self):
        run = smi_runner(linux_trees.TREES["desktop"]["smi"])
        cards, problems = hardware.adapters(self.tree("desktop"), run)
        self.assertEqual(problems, [])
        self.assertEqual(run.calls, [["nvidia-smi", "--query-gpu=pci.bus_id,name,memory.total",
                                      "--format=csv,noheader"]])
        self.assertEqual([(c["name"], c["vram_mb"], c["luid"]) for c in cards],
                         [("NVIDIA Example GPU", 24564, "pci:0000:01:00.0")])

    def test_no_nvidia_smi_names_the_card_from_proc_and_says_so(self):
        # The folder is the bus id (0000:01:00.0) on Linux; a Windows test folder cannot hold a
        # colon, and the reader keys by the file's Bus Location line, so any folder name serves.
        info = {"proc/driver/nvidia/gpus/card-a/information":
                "Model: \t\t NVIDIA Example GPU\nIRQ:   \t\t 140\n"
                "Bus Location: \t 0000:01:00.0\n"}
        cards, problems = hardware.adapters(self.tree("desktop", extra=info), smi_runner(None))
        self.assertEqual([(c["name"], c["vram_mb"]) for c in cards], [("NVIDIA Example GPU", 0)])
        self.assertEqual(len(problems), 1)
        self.assertIn("nvidia-smi", problems[0])

    def test_no_drm_class_is_no_card_and_not_a_failed_look(self):
        hw = self.look("gaps")
        self.assertEqual((hw["gpus"], hw["failed"]), ([], []))

    def test_the_seam_primitives_read_the_root(self):
        with mock.patch.object(hardware, "ROOT", self.tree("desktop")), \
                mock.patch.object(hardware, "_affinity", return_value=None):
            self.assertEqual(hardware.threads(), list(range(16)))
            self.assertEqual(hardware.all_mask(), 0xFFFF)
            self.assertEqual(hardware.l3_domains(), [0xFFFF])
            self.assertEqual(hardware.memory_mb(), {"total_mb": 32000, "free_mb": 29296})

    def test_the_snapshot_reads_free_memory_through_the_seam(self):
        """A node's status (api.snapshot) on Linux: the free memory comes from the platform's
        memory_mb, never the Windows binding (which refuses off Windows)."""
        hw = self.look("desktop")
        with mock.patch.object(kitlib.platform, "NAME", "linux"), \
                mock.patch.object(hardware, "ROOT", self.tree("desktop")):
            out = api._hardware(types.SimpleNamespace(testing=False), lay.derive(hw, {}), hw)
        self.assertEqual(out["memory_mb"]["free"], 29296)


class FailedLooks(TreeCase):
    """What cannot be read is in `problems`; a stand-in part is named in `failed`."""

    def test_no_online_list_is_a_failed_groups_look_on_the_affinity(self):
        hw = self.look("desktop", drop=[linux_trees.CPU + "/online"])
        self.assertEqual(hw["failed"], ["groups"])
        self.assertEqual(hw["threads"], list(range(16)))
        assert_divides(self, hw, range(16))

    def test_no_topology_is_a_failed_cores_look_one_thread_a_core(self):
        topo = ["%s/cpu%d/topology" % (linux_trees.CPU, n) for n in range(16)]
        hw = self.look("desktop", drop=topo)
        self.assertEqual(hw["failed"], ["cores"])
        self.assertEqual([c["threads"] for c in hw["cores"]], [[n] for n in range(16)])
        assert_divides(self, hw, range(16))

    def test_a_sibling_list_that_disagrees_still_gives_each_id_one_core(self):
        bad = {linux_trees.CPU + "/cpu8/topology/core_cpus_list": "8-9\n"}
        hw = self.look("desktop", extra=bad)
        flat = [t for c in hw["cores"] for t in c["threads"]]
        self.assertEqual(sorted(flat), list(range(16)))
        assert_divides(self, hw, range(16))

    def test_a_malformed_file_is_a_problem_never_a_raise(self):
        hw = self.look("desktop", extra={"proc/meminfo": "garbage\n",
                                         linux_trees.CPU + "/online": "0-x\n"})
        self.assertIsNone(hw["memory_mb"])
        self.assertIn("groups", hw["failed"])
        self.assertTrue(hw["problems"])


class Dispatch(unittest.TestCase):
    def test_detect_on_linux_is_the_linux_look(self):
        with mock.patch.object(kitlib.platform, "NAME", "linux"), \
                mock.patch.object(lay, "detect_linux", return_value={"linux": True}) as look, \
                mock.patch.object(lay.cpu, "listed_groups", side_effect=AssertionError("windows")):
            self.assertEqual(lay.detect(), {"linux": True})
        look.assert_called_once_with()

    def test_detect_elsewhere_never_asks_linux(self):
        with mock.patch.object(kitlib.platform, "NAME", "windows"), \
                mock.patch.object(lay, "detect_linux", side_effect=AssertionError("linux")), \
                mock.patch.object(lay.cpu, "listed_groups", side_effect=RuntimeError("windows look")):
            with self.assertRaisesRegex(RuntimeError, "windows look"):
                lay.detect()


@posix_only("reads the running Linux machine's own /sys and /proc")
class LiveSys(unittest.TestCase):
    def test_the_live_machine_divides_with_no_shared_id(self):
        if kitlib.platform.NAME != "linux":
            self.skipTest("not Linux")
        hw = lay.detect()
        self.assertEqual(hw["source"]["cpu"], "linux")
        self.assertLessEqual(set(hw["threads"]), set(os.sched_getaffinity(0)))
        assert_divides(self, hw, hw["threads"])
        print("\nlive look: threads %s, cores %d, l3 %d, memory %s MB, gpus %s, failed %s, problems %s"
              % (hw["threads"], len(hw["cores"]), len(hw["l3"]), hw["memory_mb"],
                 [(g["name"], g["vram_mb"]) for g in hw["gpus"]], hw["failed"], hw["problems"]))


if __name__ == "__main__":
    unittest.main()
