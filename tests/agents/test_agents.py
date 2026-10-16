import json
import re
import unittest
from pathlib import Path

from tests.shipped import scan

AGENTS = Path(__file__).resolve().parents[2] / "hosts" / "claude" / "shell" / "agents"
EXPECTED = {"pm", "coordinator", "weak-worker", "worker", "senior-worker", "strong-worker", "deep-worker", "consultant", "reviewer",
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
        if line[:1] in (" ", "\t"):        # a map's child: never a top-level key, never shadows one
            continue
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip()
    return out


MAP_KEY = "experimental"    # the one top-level key an agent file may open a map under


def strict_frontmatter(text):
    """Parse the frontmatter the way a YAML loader would accept it, for the shape the agent files
    use: flat `key: value` lines, plus one map, the top-level key `experimental` with no value of
    its own followed by `key: value` lines indented two spaces (`experimental:` over
    `cacheTtl: 1h`). Returns (dict, problems); a map's value is a dict. An unquoted scalar holding
    ': ' (or ' #') is a YAML error, and a quoted one must close its quote at the end of the line.
    Any other indent (a tab, four spaces, a line under a key that has a value, a map inside a map,
    a map under any key but `experimental`) is a problem: the files do not use it, so this parser
    does not model it. So is a key with nothing after its colon and no map under it. A whole-line
    comment at the left margin (`# ...`, the generated-file line the renderer writes) is YAML a
    loader skips, and so is skipped here."""
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        return None, ["no frontmatter block"]
    top, problems, parent, opened = {}, [], None, []
    for line in m.group(1).splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        km = re.match(r"(  )?([A-Za-z][\w-]*):(?: +(.*))?$", line)
        if not km:
            problems.append("not a 'key: value' line, flat or one map level down: %r" % line[:60])
            continue
        key, val = km.group(2), (km.group(3) or "").strip()
        if km.group(1):                      # a line of the map opened by the key above
            if parent is None:
                problems.append("indented line under no empty-valued key: %r" % line[:60])
                continue
            if not isinstance(top[parent], dict):
                top[parent] = {}
                if parent != MAP_KEY:
                    problems.append("%s: a map under a key other than %s" % (parent, MAP_KEY))
            out, label = top[parent], "%s.%s" % (parent, key)
            if not val:
                problems.append("%s: no value (a map inside a map is not modelled)" % label)
        else:
            out, label, parent = top, key, (None if val else key)
        if key in out:
            problems.append("duplicate key %s" % label)
        if val[:1] in ("'", '"'):
            q = val[0]
            body = val[1:-1]
            if len(val) < 2 or val[-1] != q:
                problems.append("%s: quote not closed at end of line" % label)
            elif q == "'" and re.search(r"(?<!')'(?!')", body):
                problems.append("%s: lone ' inside single-quoted scalar" % label)
            elif q == '"' and re.search(r'(?<!\\)"', body):
                problems.append('%s: unescaped " inside double-quoted scalar' % label)
            val = body
        elif val:
            if ": " in val or val.endswith(":"):
                problems.append("%s: unquoted scalar contains ': '" % label)
            if " #" in val:
                problems.append("%s: unquoted scalar contains ' #'" % label)
            if val[:1] in "[]{}&*!|>%@`,":
                problems.append("%s: unquoted scalar starts with %r" % (label, val[:1]))
        elif out is top:
            opened.append(key)
        out[key] = val
    # a key with nothing after the colon is a problem, as before, unless it opened a map
    problems.extend("%s: no value and no map under it" % k for k in opened
                    if not isinstance(top[k], dict))
    return top, problems


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

    def test_strict_parser_takes_one_map_and_refuses_every_other_indent(self):
        def parse(*lines):
            return strict_frontmatter("---\nname: x\n%s\n---\nbody\n" % "\n".join(lines))
        fm, problems = parse("experimental:", "  cacheTtl: 1h", "  other: \"a: b\"", "model: opus")
        self.assertEqual(problems, [])
        self.assertEqual(fm, {"name": "x", "experimental": {"cacheTtl": "1h", "other": "a: b"},
                              "model": "opus"})
        for bad in (("model: opus", "  cacheTtl: 1h"),            # under a key that has a value
                    ("experimental:", "\tcacheTtl: 1h"),          # a tab
                    ("experimental:", "    cacheTtl: 1h"),        # four spaces
                    ("experimental:", " cacheTtl: 1h"),           # one space
                    ("experimental:", "  deeper:", "  x: 1"),     # a map inside a map
                    ("experimental:", "  cacheTtl: 1h: 2"),       # ': ' in a plain scalar, one level down
                    ("experimental:", "  cacheTtl: 1h", "  cacheTtl: 5m"),
                    ("experimental: {cacheTtl: 1h}",),            # a flow map: not the shape in use
                    ("experimental:",),                           # nothing after the colon, no map
                    ("experimental:", "model: opus"),
                    ("x:", "  cacheTtl: 1h"),                     # a map under another key
                    ("experimental:", "  cacheTtl: 1h", "x:", "  y: 1"),   # a second map
                    ("tools: Read", "x:", "  tools: Skill"),      # a child named like a top-level key
                    ("  cacheTtl: 1h",)):                         # right under `name: x`
            with self.subTest(lines=bad):
                self.assertTrue(parse(*bad)[1], bad)

    def test_a_map_child_never_shadows_a_top_level_key(self):
        """test_agent_naming_a_skill_can_load_skills reads `tools` with the loose parser: it must
        see the top-level list the client sees, not a line indented under another key."""
        for indent in ("  ", " ", "\t"):
            with self.subTest(indent=indent):
                fm = frontmatter("---\nname: x\ntools: Read\nx:\n%stools: Skill\n---\nbody\n" % indent)
                self.assertEqual(fm, {"name": "x", "tools": "Read", "x": ""})

    def test_only_strong_worker_asks_for_the_one_hour_prompt_cache(self):
        """The one-hour cache lifetime is one agent's setting (docs/ROSTER.md, "The one-hour prompt
        cache"); the client reads `cacheTtl` only inside the `experimental` map of a subagent file."""
        for p in sorted(AGENTS.glob("*.md")):
            with self.subTest(agent=p.name):
                text = p.read_text(encoding="utf-8")
                fm, _ = strict_frontmatter(text)
                head = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S).group(1)
                if p.stem == "strong-worker":
                    self.assertEqual(fm.get("experimental"), {"cacheTtl": "1h"})
                    self.assertEqual(head.count("cacheTtl"), 1)
                else:
                    self.assertNotIn("experimental", fm)
                    self.assertNotIn("cacheTtl", head)

    def test_set_is_complete(self):
        self.assertEqual({p.stem for p in AGENTS.glob("*.md")}, EXPECTED)

    def test_no_chatgpt_fallback_in_agents_or_knowledge(self):
        """A generator outage stops the run; an external-prompt fallback is a repo choice, not the kit's."""
        root = AGENTS.parents[3]
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
        manifest = json.loads((AGENTS.parents[3] / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["agents"]),
                         sorted("./hosts/claude/shell/agents/%s" % p.name for p in AGENTS.glob("*.md")))
        # the skills are content/skills itself: no rendered copy (decision 29, docs/content.md)
        self.assertEqual(manifest["skills"], ["./content/skills/"])
        self.assertTrue((AGENTS.parents[3] / "content" / "skills").is_dir())
        self.assertFalse((AGENTS.parents[3] / "hosts" / "claude" / "shell" / "skills").exists())

    def test_agent_naming_a_skill_can_load_skills(self):
        # An explicit `tools:` list without Skill stops the agent invoking any skill (also on the
        # main thread under `claude --agent`), so a body that says to use one is unreachable.
        skills = [d.name for d in (AGENTS.parents[3] / "content" / "skills").iterdir() if d.is_dir()]
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
