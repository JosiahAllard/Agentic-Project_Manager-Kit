"""The kit's default agent profile, content/default-profile.md (decision 26, docs/multi-host-plan.md
section 7: the repo profile is opt-in). A role reads it when the repository has no profile of its
own, so it has a heading for every profile section any role names, read from the role files
rather than listed here; it is host-neutral, since no renderer fills tokens in it; and every role
that falls back on a missing profile names it, none of them stopping for want of one."""
import os
import re
import unittest

from kitlib import content
from tests.host.test_leaks import HOST_WORDS

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROFILE = os.path.join(KIT_ROOT, 'content', 'default-profile.md')
FALLBACK = 'No profile: use `<kit>/content/default-profile.md` instead'
# Where a role names profile sections: after the repo profile's token, or after "the profile's".
MENTION = re.compile(r'\{\{profile_path\}\}|\bprofile\'s\b')
BOLD = re.compile(r'\*\*([^*]+)\*\*')


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def named_sections(body):
    """Every bold name that follows a profile mention in the same sentence of a role body: the
    sections a role reads ("Read `{{profile_path}}` sections **A**, **B**", "the profile's **C**
    section"). Line breaks are joined first, as a sentence wraps."""
    out = set()
    for sentence in re.split(r'(?<=[.:;])\s', re.sub(r'\s+', ' ', body)):
        m = MENTION.search(sentence)
        if m:
            out.update(name.strip() for name in BOLD.findall(sentence[m.end():]))
    return out


def headings(text):
    return [re.sub(r'^#+\s*', '', l).strip() for l in text.splitlines() if re.match(r'^#{2,3} ', l)]


class DefaultProfile(unittest.TestCase):
    def setUp(self):
        self.roles = content.load_roles(KIT_ROOT)

    def test_it_exists(self):
        self.assertTrue(os.path.isfile(PROFILE), 'the kit ships content/default-profile.md')

    def test_the_section_reader_finds_the_sections_roles_name(self):
        # The scan itself: a known role's sections are found, a non-profile bold is not.
        body = ('- **Which tier(s)**, by the names in the profile\'s **Test runner** section.\n'
                'Read `{{profile_path}}` sections **Branches and\nworktrees**, **Records**.')
        self.assertEqual(named_sections(body), {'Test runner', 'Branches and worktrees', 'Records'})

    def test_it_has_a_heading_for_every_section_a_role_names(self):
        have = [h.lower() for h in headings(read(PROFILE))]
        named = {}
        for role in self.roles:
            for name in named_sections(role.body):
                named.setdefault(name, []).append(role.name)
        self.assertIn('Build and test', named, 'the scan found no section names in the roles')
        missing = ['%s (named by %s)' % (n, ', '.join(sorted(r))) for n, r in sorted(named.items())
                   if not any(h.startswith(n.lower()) for h in have)]
        self.assertEqual(missing, [], 'content/default-profile.md lacks a section a role reads')

    def test_it_names_no_host_and_holds_no_token(self):
        text = read(PROFILE)
        self.assertEqual(HOST_WORDS.findall(text), [], 'write host values in neutral words')
        self.assertNotIn('{{', text, 'no renderer fills tokens in the default profile')

    def test_every_role_that_reads_the_profile_falls_back_to_it(self):
        for role in self.roles:
            with self.subTest(role=role.name):
                body = re.sub(r'\s+', ' ', role.body)
                self.assertNotRegex(body, r'No profile[^.]*: stop', 'a missing profile no longer stops')
                if re.search(r'Read (the )?`\{\{profile_path\}\}` sections', body):
                    self.assertIn(FALLBACK, body)


if __name__ == '__main__':
    unittest.main()
