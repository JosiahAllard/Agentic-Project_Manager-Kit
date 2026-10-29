"""Ceilings held to the base (docs/module-map.md "Raises").

The ceilings file is a plain file, so an agent may lower a ceiling; raising one needs the owner.
`--check` reads the map and the ceilings file at the merge base of the base rev and HEAD (for a
landing's merged tree that is the base itself) and refuses a ceiling above the base's, unless the
map, an owner path, holds a `raises` entry for that ratchet that allows it:

  allowed = max(the base's ceiling (0 when it has none), the raise's ceiling)

A ratchet the base's map does not have is new: the owner added it, so its ceiling is not compared.
With no map at the base (the first rollout) nothing is compared and a note says so.
"""
import collections

from kitlib.config.types import ConfigError, parse_json
from kitlib.git import batch as gitbatch
from kitlib.git import refs as gitrefs

from .load import MapError, ceilings_of

# rev: the commit read (the merge base); ratchets: the ids its map has; ceilings: its ceilings.
Base = collections.namedtuple('Base', 'rev ratchets ceilings')


def _parse(blob, where):
    try:
        return parse_json(blob.decode('utf-8-sig', errors='replace'), where)
    except ConfigError as e:
        raise MapError('at the base: %s' % e)


def read_base(root, rev, map_rel, ceilings_rel):
    """`(Base or None, notes)`: the map's ratchet ids and the ceilings at the merge base of `rev`
    and HEAD. None (with a note) when the map is not there. MapError when git cannot answer."""
    mb, why = gitrefs.merge_base(rev, 'HEAD', root)
    if mb is None:
        raise MapError('cannot read the base %s: %s' % (rev, why))
    if not mb:
        raise MapError('the base %s shares no commit with HEAD' % rev)
    specs = ['%s:%s' % (mb, map_rel)]
    if ceilings_rel:
        specs.append('%s:%s' % (mb, ceilings_rel))
    got, why = gitbatch.cat_file(specs, root)
    if got is None:
        raise MapError('cannot read the base %s: %s' % (rev, why))
    short = mb[:10]
    blob = got.get(specs[0])
    if blob is None or blob[0] != 'blob':
        return None, ['note: the base %s (%s) has no module map %s: ceilings are not compared '
                      'with it' % (rev, short, map_rel)]
    data = _parse(blob[1], '%s:%s' % (short, map_rel))
    rows = data.get('ratchets') if isinstance(data, dict) else None
    ids = {r['id'] for r in (rows if isinstance(rows, list) else [])
           if isinstance(r, dict) and isinstance(r.get('id'), str)}
    ceilings = {}
    cblob = got.get(specs[1]) if ceilings_rel else None
    if cblob is not None and cblob[0] == 'blob':
        where = '%s:%s' % (short, ceilings_rel)
        ceilings = ceilings_of(_parse(cblob[1], where), where)
    return Base('%s (%s)' % (rev, short), ids, ceilings), []


def compare(mm, ceilings, base):
    """`(failed, lines)`: each ratchet's ceiling held to the base's, or its owner's raise."""
    failed, lines = False, []
    for r in mm.ratchets:
        rid = r['id']
        now = ceilings.get(rid, 0)
        if rid not in base.ratchets:
            lines.append('note: ratchet %s is new in the map: its ceiling %d is not compared with '
                         'the base' % (rid, now))
            continue
        was = base.ceilings.get(rid, 0)
        up = mm.raise_for(rid)
        allowed = max(was, up['ceiling']) if up else was
        if now > allowed:
            failed = True
            lines.append('RAISED  ratchet %s  ceiling %d, the base %s has %d%s: only the owner '
                         'raises a ceiling, with a "raises" entry in the map'
                         % (rid, now, base.rev, was,
                            ' and the raise allows %d' % up['ceiling'] if up else ''))
        if up and up['ceiling'] <= was:
            lines.append('note: the raise for %s (ceiling %d) is spent: the base already has %d; '
                         'the owner may remove it' % (rid, up['ceiling'], was))
    return failed, lines
