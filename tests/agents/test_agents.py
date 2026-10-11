import json
import re
import unittest
from pathlib import Path

from tests.shipped import scan

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


def strict_frontmatter(text):
    """Parse the frontmatter the way a YAML loader would accept it, for the flat `key: value` shape
    the agent files use. Returns (dict, problems). An unquoted scalar holding ': ' (or ' #') is a
    YAML error, and a quoted one must close its quote at the end of the line."""
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        return None, ["no frontmatter block"]
    out, problems = {}, []
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        km = re.match(r"([A-Za-z][\w-]*):(?: +(.*))?$", line)
        if not km:
            problems.append("not a flat 'key: value' line: %r" % line[:60])
            continue
        key, val = km.group(1), (km.group(2) or "").strip()
        if key in out:
            problems.append("duplicate key %s" % key)
        if val[:1] in ("'", '"'):
            q = val[0]
            body = val[1:-1]
            if len(val) < 2 or val[-1] != q:
                problems.append("%s: quote not closed at end of line" % key)
            elif q == "'" and re.search(r"(?<!')'(?!')", body):
                problems.append("%s: lone ' inside single-quoted scalar" % key)
            elif q == '"' and re.search(r'(?<!\\)"', body):
                problems.append('%s: unescaped " inside double-quoted scalar' % key)
            val = body
        else:
            if ": " in val or val.endswith(":"):
                problems.append("%s: unquoted scalar contains ': '" % key)
            if " #" in val:
                problems.append("%s: unquoted scalar contains ' #'" % key)
            if val[:1] in "[]{}&*!|>%@`,":
                problems.append("%s: unquoted scalar starts with %r" % (key, val[:1]))
        out[key] = val
    return out, problems


class AgentFilesTest(unittest.TestCase):
    def test_frontmatter_is_valid_yaml(self):
        """An agent file whose frontmatter does not parse is dropped from every session."""
        for p in sorted(AGENTS.glob("*.md")):
            with self.subTest(agent=p.name):
                fm, problems = strict_frontmatter(p.read_text(encoding="utf-8"))
                self.assertEqual(problems, [], p.name)
                self.assertEqual(fm.get("name"), p.stem, p.name)
                self.assertTrue(fm.get("description"), p.name)

    def test_strict_parser_refuses_colon_space_in_plain_scalar(self):
        _, problems = strict_frontmatter("---\nname: x\ndescription: go (a): those go to y.\n---\nbody\n")
        self.assertTrue(problems)
        _, problems = strict_frontmatter('---\nname: x\ndescription: "go (a): those go to y."\n---\nbody\n')
        self.assertEqual(problems, [])

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
        # read here: a bad config fails this test; the base patterns run with or without names
        found = scan([(p.name, p.read_text(encoding="utf-8")) for p in AGENTS.glob("*.md")],
                     base=BASE_FORBIDDEN)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
