"""kitlib.roster: render docs/ROSTER.md's tables from content/roles and each host's tier -> model map.

The role files are the one roster (docs/multi-host-plan.md section 7, decision 17): a role is added
or removed with one file and a render. ROSTER.md is prose the owner writes, with two rendered
regions, each between a start marker and END:
  <!-- rendered: roles -->      | role | tier | effort | use for |, one row per role file, by name:
                                its `tier`, its `effort` (`-` for none) and its `summary` (else its
                                description)
  <!-- rendered: tiers -->      | tier | <host> | ... |, one row per tier, one column per host
                                (the tier maps passed in: kitlib names no host)
`render` returns the text with both regions made what they say and every other byte kept (the
file's line ending included); kitlib.content renders it with the host files, so the drift check
covers it (tools/render_content.py). Pure text work: no printing, no exit.
"""
import re

from kitlib.host.content import TIERS, ContentError

ROSTER_REL = 'docs/ROSTER.md'
NONE = '-'
ROLES = 'roles'
TIER_TABLE = 'tiers'
START = '<!-- rendered: %s -->'
END = '<!-- end rendered -->'


def _cell(text):
    """`text` as one table cell: on one line, a `|` escaped."""
    return ' '.join(text.split()).replace('|', '\\|')


def roles_table(content):
    """The role table's lines for Content `content`, by role name."""
    out = ['| role | tier | effort | use for |', '|---|---|---|---|']
    for r in sorted(content.roles, key=lambda r: r.name):
        out.append('| %s | %s | %s | %s |' % (r.name, r.tier, r.effort or NONE, _cell(r.summary or r.description)))
    return out


def tiers_table(tier_maps):
    """The tier table's lines for `tier_maps` ({host: {tier: model}}), hosts by name."""
    hosts = sorted(tier_maps)
    out = ['| tier | %s |' % ' | '.join(hosts), '|---|%s' % ('---|' * len(hosts))]
    for tier in TIERS:
        out.append('| %s | %s |' % (tier, ' | '.join(_cell(tier_maps[h].get(tier, NONE)) for h in hosts)))
    return out


def render(text, content, tier_maps, source=ROSTER_REL):
    """`text` (ROSTER.md) with its rendered regions remade; ContentError naming `source` when a
    region's markers are missing, repeated or out of order."""
    nl = '\r\n' if '\r\n' in text else '\n'
    for name, lines in ((ROLES, roles_table(content)), (TIER_TABLE, tiers_table(tier_maps))):
        start = START % name
        pat = re.compile(r'^%s[ \t]*\r?\n.*?^%s(?=[ \t]*\r?$)' % (re.escape(start), re.escape(END)), re.M | re.S)
        found = pat.findall(text)
        if len(found) != 1:
            raise ContentError('%s: needs exactly one %s ... %s region' % (source, start, END))
        block = nl.join([start] + lines + [END])
        text = pat.sub(lambda _: block, text, count=1)
    return text
