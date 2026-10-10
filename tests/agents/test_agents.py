import json
import re
import unittest
from pathlib import Path

from tests.shipped import install_patterns

AGENTS = Path(__file__).resolve().parents[2] / "claude" / "agents"
EXPECTED = {"coordinator", "worker", "opus-worker", "strong-worker", "deep-worker", "consultant", "reviewer",
            "lander", "records", "researcher", "spec-writer", "test-runner", "asset-smith",
            "sound-smith", "tech-artist"}
BASE_FORBIDDEN = [(re.compile(r"\bR-\d+"), "ruling id"),
                  (re.compile(r"[A-Za-z]:[\/]+Users[\/]", re.I), "Windows user path")]


def frontmatter(text):
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    return out


class AgentFilesTest(unittest.TestCase):
    def test_set_is_complete(self):
        self.assertEqual({p.stem for p in AGENTS.glob("*.md")}, EXPECTED)

    def test_no_chatgpt_fallback_in_agents_or_knowledge(self):
        """A generator outage stops the run; an external-prompt fallback is a repo choice, not the kit's."""
        root = AGENTS.parents[1]
        for p in list(AGENTS.glob("*.md")) + list((root / "knowledge").rglob("*.md")):
            self.assertNotIn("chatgpt", p.read_text(encoding="utf-8").lower(), p.name)

    def test_frontmatter_parses(self):
        for p in AGENTS.glob("*.md"):
            fm = frontmatter(p.read_text(encoding="utf-8"))
            self.assertIsNotNone(fm, p.name)
            self.assertEqual(fm.get("name"), p.stem, p.name)
            self.assertTrue(fm.get("description"), p.name)
            self.assertIn(fm.get("model"), {"opus", "sonnet", "haiku"}, p.name)

    def test_plugin_manifest_lists_every_agent_file(self):
        # plugin.json's `agents` takes files, not a folder, and replaces the default agents/
        # lookup: an agent file it does not list is never loaded.
        manifest = json.loads((AGENTS.parents[1] / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["agents"]),
                         sorted("./claude/agents/%s" % p.name for p in AGENTS.glob("*.md")))
        self.assertEqual(manifest["skills"], ["./claude/skills/"])
        self.assertTrue((AGENTS.parents[1] / "claude" / "skills").is_dir())

    def test_agent_naming_a_skill_can_load_skills(self):
        # An explicit `tools:` list without Skill stops the agent invoking any skill (also on the
        # main thread under `claude --agent`), so a body that says to use one is unreachable.
        skills = [d.name for d in (AGENTS.parent / "skills").iterdir() if d.is_dir()]
        for p in AGENTS.glob("*.md"):
            text = p.read_text(encoding="utf-8")
            tools = frontmatter(text).get("tools")
            if not tools:
                continue
            named = [s for s in skills if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(s), text)]
            if named:
                self.assertIn("Skill", [t.strip() for t in tools.split(",")],
                              f"{p.name} names skill(s) {named} but its tools: list has no Skill")

    def test_no_repo_or_owner_specifics(self):
        patterns = BASE_FORBIDDEN + install_patterns()   # read here: a bad config fails this test
        for p in AGENTS.glob("*.md"):
            text = p.read_text(encoding="utf-8")
            for rx, what in patterns:
                self.assertIsNone(rx.search(text), f"{p.name}: {what}")


if __name__ == "__main__":
    unittest.main()
