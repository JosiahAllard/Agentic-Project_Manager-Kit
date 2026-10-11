"""The scope language and collision (ported in behaviour from an earlier tools/backlog.py). A
scope member is `none`, a literal repo-relative path, or a path with a `/**` suffix. A comparison
that cannot be decided raises UndecidableScope and is a collision to every caller; nothing here
returns "clear" for an input it did not understand."""
import re

NONE = 'none'
WILDCARD_CHARS = '*?[]{}'
GLOB_SUFFIX = '/**'


class UndecidableScope(Exception):
    """A comparison against a member this grammar cannot parse, or an empty scope set."""


def path_shape_error(p, what='path', tab_ok=False):
    """None if `p` is a repo-root-relative forward-slash path, else why not. `tab_ok`: a tab inside
    the path is an ordinary character - for reading members a record already holds (collision,
    the offer), so an old tab blocks no session; a write's new tab is still a `check` finding."""
    if not p:
        return 'an empty %s' % what
    if p != p.strip():
        return 'a %s with leading or trailing whitespace' % what
    if any((ord(ch) < 32 and not (tab_ok and ch == '\t')) or ord(ch) == 127 for ch in p):
        return ('a control character in the %s %r - a line break would split a record line'
                % (what, p))
    if ',' in p:
        return 'a comma in a %s - a set is one `%s:` line per member' % (what, what)
    if '\\' in p:
        return 'a backslash in a %s - paths are forward-slash, repo-root relative' % what
    if p.startswith('/'):
        return 'an absolute %s - paths are repo-root relative' % what
    if re.match(r'^[A-Za-z]:', p):
        return 'a drive letter in a %s - paths are repo-root relative' % what
    for part in p.split('/'):
        if part == '':
            return 'an empty path component in `%s` (a doubled or trailing slash)' % p
        if part in ('.', '..'):
            return 'a `%s` component in `%s` - paths do not walk upwards' % (part, p)
    return None


def scope_member_error(member, tab_ok=False):
    """None if `member` is a legal scope member, else why not (`tab_ok`: see path_shape_error)."""
    if member == NONE:
        return None
    if not member:
        return 'an empty scope'
    if member.casefold() == NONE:
        return '`%s` casefolds into the `none` sentinel but is not it; write `none`' % member
    body = member
    if body.endswith(GLOB_SUFFIX):
        body = body[:-len(GLOB_SUFFIX)]
        if not body:
            return '`%s` has no prefix - a prefix glob is `<path>/**`' % member
    if any(ch in WILDCARD_CHARS for ch in body):
        return ('`%s` is not a scope: the only wildcard is a trailing `/**`; other globs are '
                'parse errors, not patterns' % member)
    return path_shape_error(body, 'scope', tab_ok)


def scope_components(member):
    """The casefolded component list `members_collide` compares."""
    err = scope_member_error(member, tab_ok=True)
    if err is not None:
        raise UndecidableScope('%s: %s' % (member, err))
    if member == NONE:
        raise UndecidableScope('`none` has no components; it collides with nothing')
    body = member[:-len(GLOB_SUFFIX)] if member.endswith(GLOB_SUFFIX) else member
    return body.casefold().strip('/').split('/')


def members_collide(a, b):
    """True iff one member's component list is a prefix of the other's. Component-wise, so
    `Foo.java` and `Foobar.java` are clear; over-approximate by design. A tab a member already
    holds is an ordinary character here."""
    for m in (a, b):
        err = scope_member_error(m, tab_ok=True)
        if err is not None:
            raise UndecidableScope('%s: %s' % (m, err))
    if a == NONE or b == NONE:
        return False
    ca, cb = scope_components(a), scope_components(b)
    n = min(len(ca), len(cb))
    return ca[:n] == cb[:n]


def scopes_collide(scopes_a, scopes_b):
    """True iff any member of one set collides with any member of the other. Every member is
    validated, even after a hit; an empty set raises."""
    for name, scopes in (('a', scopes_a), ('b', scopes_b)):
        if not scopes:
            raise UndecidableScope('scope set %s is empty; an item that touches no tracked file '
                                   'says `scope: none`' % name)
    hit = False
    for a in scopes_a:
        for b in scopes_b:
            if members_collide(a, b):
                hit = True
    return hit


def colliding_pairs(scopes_a, scopes_b):
    return [(a, b) for a in scopes_a for b in scopes_b if members_collide(a, b)]


def area_from_scope(scopes):
    """`area` is the first component of the first scope that is not `none`, else `none`."""
    for s in scopes:
        if s != NONE:
            return s.split('/')[0]
    return NONE
