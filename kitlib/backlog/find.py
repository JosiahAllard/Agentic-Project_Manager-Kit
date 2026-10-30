"""`backlog.py find TEXT`: the records, open and ended, whose id, summary or body holds TEXT.

An open record is read from the snapshot; an ended one (closed, retired or deleted) from the text
it had in the commit before its deleting commit (store.removed_records, one git log and one
cat-file for all of them). No fetch happens here: the caller opened the store as every read verb
does."""
import re

CAP = 30
_ENDS = {'closes': 'closed', 'retires': 'retired', 'deletes': 'deleted'}
_SUMMARY = re.compile(r'^summary:[ \t]*(.*)$', re.M)


def _split(text):
    """`(summary, body)` of a record's text: the body follows the first blank line."""
    head, _sep, body = (text or '').replace('\r\n', '\n').partition('\n\n')
    m = _SUMMARY.search(head)
    return (m.group(1).strip() if m else ''), body


def _hits(needle, rid, text):
    summary, body = _split(text)
    return summary, any(needle in s.lower() for s in (rid, summary, body))


def search(snap, store, needle, cap=CAP):
    """`(lines, omitted)`: one line per hit, at most `cap`, open records (by id) first, then ended
    ones newest first; `omitted` counts the hits left out."""
    needle = needle.strip().lower()
    hits = []
    for rec in sorted(snap.records, key=lambda r: r.stem):
        summary, hit = _hits(needle, rec.stem, rec.text)
        if hit:
            hits.append('%-28s open   %s' % (rec.stem, rec.get('summary') or summary))
    for rid, gone, text in store.removed(snap):
        if snap.get(rid) is not None:
            continue                                 # added back: the open record is the hit
        summary, hit = _hits(needle, rid, text)
        if hit:
            how = _ENDS.get(gone.subject.split(' ', 1)[0], 'removed')
            hits.append('%-28s ended  %s  (%s by %s %s)'
                        % (rid, summary, how, gone.sha, gone.when[:10]))
    return hits[:cap], max(0, len(hits) - cap)
