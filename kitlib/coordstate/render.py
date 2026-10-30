"""The state note `coord_state.py show` renders (docs/coord-state.md "The state note"): a
coordinator's event log (kitlib.coordstate.log) joined with what the repository already records,
so nothing a tool can see is written by hand.

  - Landings: the latest `land_submit` of each branch, joined with that branch's land result
    record (kitlib.gate.result, kind "land") at the submitted tip and, once it landed, with the
    outcome lines of the submission's output file (kitlib.gate.submit.outcome_block). The landing
    feed's WARNING (kitlib.hubsite.landings) is said there, after the last `result record:` line,
    and never in the record; so a landing is TO_REPORT when it ended without landing, or landed
    with that WARNING, or its output cannot tell, until a `reported` event names it. A landing
    that landed with no WARNING and a LANDED line is on the hub feed (LANDED_POSTED). Between the
    final record and the feed post the output has neither: IN_FLIGHT.
  - Verdicts queued by this session and still waiting to land: the queue (kitlib.gate.verdicts,
    entries whose `session` is it) read as it stands, leaving out the ones verdicts.expired names
    against main as last fetched. Nothing is dropped from the queue here.
  - The latest backlog writes.
  - The judgments file, verbatim: the one part the coordinator writes.

The note is held to CAP bytes by dropping the oldest lines of the least needed sections first
(backlog writes, reported landings, landings on the feed).

  fold(events)                       (landings, backlog events): one item per branch
  feed_state(lines)                  POSTED, PENDING, UNKNOWN or the feed WARNING line itself
  judge(item, record, why, block)    the item's group and what it says
  build(root, events, read, block)   every landing judged: {group: [items]}, backlog events
  landing(root, events, branch, tip, read, block)
                                     the latest landing of `branch` judged, or None
  main_as_fetched(root)              (main as last fetched, None) or (None, why); no network
  queued_for(common_dir, session, main, main_why)
                                     (entries, a caveat or None), or (None, why)
  text(...)                          the note
  keep(groups, backlog)              the `_n` of every event the note shows: a compaction keeps
                                     those
"""
import datetime
import os

from kitlib.coordstate.log import NOTE_HEAD

CAP = 8 * 1024
SHOWN_DONE = 5
SHOWN_BACKLOG = 15
SUMMARY_MAX = 160
TO_REPORT, IN_FLIGHT, LANDED_POSTED, REPORTED = 'to-report', 'in-flight', 'landed', 'reported'
POSTED, PENDING, UNKNOWN = 'posted', 'pending', 'unknown'
FEED_WARNING = 'WARNING: the landing'
FEED_WORDS = 'hub feed'
OUTPUT_WARNING = "WARNING: the landing's output"
LANDED_LINE = 'LANDED '


def fold(events):
    """`(landings, backlog)`: the latest submission of each branch, oldest first, each a dict
    (branch, tip, utc, reviewed_by, output, batch, reported, events: the `_n` it came from), a
    `reported` event after it with no tip or a prefix of its tip marking it; and the backlog
    events, oldest first. A malformed event is passed over."""
    latest, backlog = {}, []
    for ev in events:
        kind = ev.get('kind')
        if kind == 'land_submit':
            branches = ev.get('branches') if isinstance(ev.get('branches'), list) else []
            tips = ev.get('tips') if isinstance(ev.get('tips'), list) else []
            for i, branch in enumerate(branches):
                if not isinstance(branch, str):
                    continue
                tip = tips[i] if i < len(tips) and isinstance(tips[i], str) else None
                latest.pop(branch, None)
                latest[branch] = {'branch': branch, 'tip': tip, 'utc': ev.get('utc'),
                                  'reviewed_by': ev.get('reviewed_by'), 'output': ev.get('output'),
                                  'batch': [b for b in branches if b != branch and isinstance(b, str)],
                                  'reported': None, 'events': [ev.get('_n')]}
        elif kind == 'reported':
            item = latest.get(ev.get('branch'))
            if item is None or not at_tip(item, ev.get('tip')):
                continue
            item['reported'] = ev.get('utc') or '?'
            item['events'].append(ev.get('_n'))
        elif kind == 'backlog':
            backlog.append(ev)
    return list(latest.values()), backlog


def at_tip(item, tip):
    """Whether `item` is the landing a mark names: no `tip` names the latest, else its tip starts
    with `tip`."""
    if not isinstance(tip, str) or not tip:
        return True
    return (item['tip'] or '').lower().startswith(tip.lower())


def feed_state(lines):
    """What a landed submission's outcome lines say of the hub feed: the feed WARNING line when
    one is there, UNKNOWN when the output could not be read, POSTED when a LANDED line is there
    with no WARNING, else PENDING (the feed post or the outcome is still to be said)."""
    lines = list(lines or ())
    for line in lines:
        if line.startswith(FEED_WARNING) and FEED_WORDS in line:
            return line
    if any(line.startswith(OUTPUT_WARNING) for line in lines):
        return UNKNOWN
    if any(line.startswith(LANDED_LINE) for line in lines):
        return POSTED
    return PENDING


def _short(text, most=SUMMARY_MAX):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= most else text[:most - 3] + '...'


def judge(item, record, why, block):
    """Set `item`'s group and say (what its line tells) from its land record `record` (or None
    and `why`) and `block`, a callable giving the outcome lines of its output file. A `reported`
    mark counts only on a final record: what was told before the end says nothing of how it
    ended."""
    from kitlib.gate import result, submit
    tip = item.get('tip') or ''
    final = None
    if record is not None and tip and result.at_tip(record, item['branch'], 'land', tip):
        final = record if record.get('verdict') not in (None, result.RUNNING) else None
        if final is None:
            item['say'] = 'running since %s' % (record.get('started_at') or '?')
    elif record is not None:
        item['say'] = 'no record of this tip yet (the land record is of %s)' % (
            str(record.get('tip') or '?')[:10])
    else:
        item['say'] = 'no land record yet' + ('' if why in (None, 'none') else ' (%s)' % _short(why))
    if final is None:
        group = IN_FLIGHT
    else:
        verdict = final.get('verdict')
        said = '%s: %s' % (verdict, _short(final.get('summary')))
        if verdict in submit.LANDED:
            feed = feed_state(block(item['output'])) if item.get('output') else UNKNOWN
            if feed == POSTED:
                group, said = LANDED_POSTED, said + '; on the hub feed'
            elif feed == PENDING:
                group, said = IN_FLIGHT, said + '; the feed post is not said yet'
            elif feed == UNKNOWN:
                group, said = TO_REPORT, said + '; the hub feed cannot be told from its output'
            else:
                group, said = TO_REPORT, said + '; ' + _short(feed, 240)
        else:
            group = TO_REPORT
        item['say'] = said
    if item.get('reported') and final is not None:
        group = REPORTED
    item['group'] = group
    return item


def _judged(root, item, read, block):
    try:
        record, why = read(root, item['branch'], 'land')
    except Exception as e:          # noqa: BLE001 - one unreadable record is said, not fatal
        record, why = None, str(e)
    return judge(item, record, why, block)


def build(root, events, read=None, block=None):
    """`({group: [items, oldest first]}, backlog events)` for `events` in the repository at
    `root`. `read(root, branch, kind)` and `block(output)` default to kitlib.gate's."""
    from kitlib.gate import result, submit
    read = read or result.read
    block = block or submit.outcome_block
    items, backlog = fold(events)
    groups = {TO_REPORT: [], IN_FLIGHT: [], LANDED_POSTED: [], REPORTED: []}
    for item in items:
        _judged(root, item, read, block)
        groups[item['group']].append(item)
    return groups, backlog


def landing(root, events, branch, tip=None, read=None, block=None):
    """The latest submitted landing of `branch`, judged, when `tip` names it; else None."""
    from kitlib.gate import result, submit
    for item in fold(events)[0]:
        if item['branch'] == branch and at_tip(item, tip):
            return _judged(root, item, read or result.read, block or submit.outcome_block)
    return None


def main_as_fetched(root):
    """`(<remote>/<main branch> as last fetched, None)`, or `(None, why)`; no network."""
    from kitlib import config as kconfig
    from kitlib.git import refs
    try:
        cfg = kconfig.load(root)
    except kconfig.ConfigError as e:
        return None, 'the kit configuration cannot be read: %s' % e
    sha, why = refs.remote_tip(cfg.remote, cfg.main_branch, root)
    return (sha, None) if sha else (None, '%s/%s: %s' % (cfg.remote, cfg.main_branch, why))


def queued_for(common_dir, session, main=None, main_why=None):
    """`(entries, caveat)`: the verdict queue's entries this session queued that still wait to
    land, oldest first, read only. The caveat, else None, says landed entries can be among them
    because `main` (main as last fetched) is unknown. `(None, why)` when the queue cannot be
    read."""
    from kitlib.gate import verdicts
    try:
        mine = [e for e in verdicts.entries(common_dir) if e.get('session') == session]
        waiting = [e for e in mine if not verdicts.expired(common_dir, e, main)]
    except Exception as e:          # noqa: BLE001 - VerdictError or an OSError: said in the note
        return None, str(e)
    if main:
        return waiting, None
    return waiting, 'main as fetched cannot be read (%s), so landed entries can be listed' % main_why


def _landing_line(item):
    batch = ' (batch with %s)' % ', '.join(item['batch']) if item.get('batch') else ''
    by = ' reviewed by %s' % item['reviewed_by'] if item.get('reviewed_by') else ''
    reported = '; reported %s' % item['reported'] if item.get('reported') else ''
    return '- %s at %s%s: submitted %s%s; %s%s' % (
        item['branch'], (item.get('tip') or '?')[:10], batch, item.get('utc') or '?', by,
        item.get('say') or '?', reported)


def _backlog_line(ev):
    who = ' as %s' % ev['branch'] if ev.get('branch') else ''
    return '- %s %s %s%s' % (ev.get('utc') or '?', ev.get('verb') or '?', ev.get('id') or '-', who)


def _queued_line(e):
    return '- %s %s %s %s %s queued %s' % (str(e.get('state') or 'queued').upper(), e.get('branch'),
                                          str(e.get('tip') or '?')[:10], e.get('risk'),
                                          e.get('verdict'), e.get('queued_utc'))


def text(session, source, groups, backlog, queued, queued_why, judgments, skipped=0, now=None):
    """The state note, at most CAP bytes when the judgments leave room (module docstring)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = now.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    head = [NOTE_HEAD + session, '',
            'Rendered %s by coord_state.py show from %s%s and this repository\'s records. Edit '
            'judgments-%s.md, never this file: show rewrites it.'
            % (stamp, os.path.basename(source),
               ' (%d unreadable line(s) skipped)' % skipped if skipped else '', session), '']
    body = (judgments or '').rstrip('\n')
    sections = [['## Judgments (judgments-%s.md)' % session, body or '(none written yet)']]
    report = groups.get(TO_REPORT, [])
    sections.append(['## Landings to report to the PM (%d)' % len(report)]
                    + [_landing_line(i) for i in report]
                    + (['Once told: coord_state.py reported <branch>.'] if report else []))
    flight = groups.get(IN_FLIGHT, [])
    sections.append(['## Landings in flight (%d)' % len(flight)] + [_landing_line(i) for i in flight])
    if queued is None:
        sections.append(['## Verdicts queued by %s' % session,
                         'the verdict queue cannot be read: %s' % _short(queued_why, 300)])
    else:
        sections.append(['## Verdicts queued by %s (%d)' % (session, len(queued))]
                        + [_queued_line(e) for e in queued]
                        + ([_short(queued_why, 300)] if queued_why else []))
    trimmable = []
    for title, group in (('Landed and on the hub feed', LANDED_POSTED), ('Reported', REPORTED)):
        items = groups.get(group, [])
        shown = items[-SHOWN_DONE:]
        trimmable.append(['## %s (latest %d of %d)' % (title, len(shown), len(items))]
                         + [_landing_line(i) for i in shown])
    shown = backlog[-SHOWN_BACKLOG:]
    trimmable.append(['## Backlog writes (latest %d of %d)' % (len(shown), len(backlog))]
                     + [_backlog_line(e) for e in shown])

    def joined():
        parts = head[:]
        for sec in sections + trimmable:
            parts.extend(sec + [''])
        return '\n'.join(parts)
    out = joined()
    for sec in reversed(trimmable):          # backlog first, then reported, then on the feed
        while len(out.encode('utf-8')) > CAP and len(sec) > 1:
            del sec[1]
            out = joined()
    return out


def keep(groups, backlog):
    """The `_n` of every event the note shows: all to report and in flight, the latest
    SHOWN_DONE landed and reported, the latest SHOWN_BACKLOG backlog writes."""
    ns = set()
    for group in (TO_REPORT, IN_FLIGHT):
        for item in groups.get(group, []):
            ns.update(item['events'])
    for group in (LANDED_POSTED, REPORTED):
        for item in groups.get(group, [])[-SHOWN_DONE:]:
            ns.update(item['events'])
    ns.update(ev.get('_n') for ev in backlog[-SHOWN_BACKLOG:])
    ns.discard(None)
    return ns
