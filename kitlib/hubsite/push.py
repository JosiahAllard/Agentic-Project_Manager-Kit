"""Push notifications: the hub tells the session that asked, and the PM, when the owner acts
(docs/action-hub.md "Push notifications"). Not the store's git push (settings `push`).

One PushWorker thread per hub (serve.build, when Sources.push and Sources.push_dir are set). It
watches the store's events and sends a short fixed text through the host's delivery object; the
item itself is read through the hub. Nothing here imports a host: the delivery object comes in.

  deliver(recipient, text) -> Sent(state, name, session, ambiguous)   one message to the live
                       session a recipient key stands for; state is WRITTEN, NOT_WRITTEN,
                       UNCERTAIN or NO_SESSION. name and session say which session (None when
                       none); ambiguous: several live sessions had that name.
  is_pm(recipient)     the recipient is the PM's own session

What fires (a fingerprint per item: a question's (status, answer.at), a to-do's (status,
done_at); only a changed fingerprint fires, so a pm_ack, which leaves both alone, never does):
  answered    a question answered or answered again
  done        a to-do marked done or done again
  reopened    a done to-do back to open: informational, acknowledged by the first written
  added       a to-do the owner added (from: you): to the PM only
Recipients: the item's `from` (a launcher key, or a to-do's free name) unless it is the PM's
(AGENT_FROM, OWNER_FROM, empty, or is_pm), and the PM (PM_KEY) always, once.

Each recipient is sent each item until it is acknowledged:
  from-session, question   Api.handed_to names its key (its own `hub pending --for` or `wait`
                           picked it up), or the question is no longer answered
  from-session, to-do      the same, or pm_ack, or the to-do is no longer done
  PM, question             the question is no longer answered (logged)
  PM, to-do                pm_ack, or no longer done (an owner-added one: pm_ack)
  either                   the item is withdrawn, or a newer fingerprint replaces the push
A recipient that already held the item when it fired (a re-answer, or a re-done to-do read
before todos_done forgot the old hand-over) is acknowledged by the first written try, never by
handed_to, which cannot tell the old hand-over from a new one.

Retries: one message per recipient per round lists every item due for it (MAX_ITEMS, then a
"+N more" line). After a try the item is due again SCHEDULE_S later (2, 5, 15, 30, 60 min), then
hourly, until GIVE_UP_S after its first try: then one gave-up line and it is dropped. Every
outcome waits for the next slot; UNCERTAIN is never re-sent at once. A deliver that raises
counts as UNCERTAIN.

Files, in `folder` (the launcher's state_dir):
  push-outbox.json         {"<kind>:<id>": {kind, id, event, fingerprint, status, repo, from,
                           first_at, recipients: {key: {attempt, next_at, last, once}}}}, times in
                           epoch seconds, replaced whole after each round that changed it. At
                           start an entry whose item changed since is fired afresh; one whose
                           item is gone is dropped; nothing else already pending is sent again.
  push-undelivered.jsonl   one line per try that was not written, and per give-up:
                           {ts, kind, id, status, recipient, name, attempt, reason}; reason is the
                           state, `ambiguous` (an extra line, written or not) or `gave-up`. Never
                           a token or any item text. Rotated once to .1 at LOG_MAX.

The text is fixed: a header `[hub push] <kind> <id> <status> repo:<repo> from:<from>
route:<recipient> attempt:<k>` (each field cut to [A-Za-z0-9._:-], 64 characters) and one line of
what to do. It never carries a title, a note or an answer: those are free text and the
recipient takes the message as a prompt.

Threads: start() reads the baseline and the outbox, then starts the thread; stop(timeout) asks it
to end and joins it (False when it did not end in time: a deliver can block up to its own
timeout). Only that thread touches the worker's state; at most one deliver is in flight, and the
stop flag is checked before each. poll() runs one round on the caller's thread, for tests (never
while the thread runs).
"""
import collections
import datetime
import json
import os
import re
import threading
import time

from kitlib.hubsite.api import AGENT_FROM, OWNER_FROM
from kitlib.hubsite.store import HubError

PM_KEY = AGENT_FROM
WRITTEN, NOT_WRITTEN, UNCERTAIN, NO_SESSION = 'written', 'not-written', 'uncertain', 'no-live-session'
AMBIGUOUS, GAVE_UP = 'ambiguous', 'gave-up'
SCHEDULE_S = (120, 300, 900, 1800, 3600)    # after the 1st, 2nd, ... try
HOURLY_S = 3600                             # after the rest
GIVE_UP_S = 24 * 3600                       # from the first try
WAIT_SLICE_S = 1.0                          # longest store wait between stop and retry checks
STOP_S = 5                                  # Hub.close's join
MAX_ITEMS = 20                              # items in one message
LOG_MAX = 1 << 20                           # push-undelivered.jsonl rotates past this
OUTBOX = 'push-outbox.json'
UNDELIVERED = 'push-undelivered.jsonl'
COLLECTIONS = {'questions': 'question', 'todos': 'todo'}
_FIELD = re.compile(r'[^A-Za-z0-9._:-]')

Sent = collections.namedtuple('Sent', 'state name session ambiguous')


def field(value):
    """A header field: only [A-Za-z0-9._:-], at most 64 characters, '-' for nothing."""
    text = _FIELD.sub('_', value if isinstance(value, str) else str(value or ''))[:64]
    return text or '-'


def fingerprint(kind, doc):
    if kind == 'question':
        ans = doc.get('answer') if isinstance(doc.get('answer'), dict) else {}
        return [doc.get('status'), ans.get('at')]
    return [doc.get('status'), doc.get('done_at')]


def trigger(kind, doc, old):
    """The event a change from fingerprint `old` (None: an item not seen before) to doc fires,
    or None."""
    status = doc.get('status')
    if kind == 'question':
        return 'answered' if status == 'answered' else None
    if status == 'done':
        return 'done'
    if status == 'open' and old is not None and old[0] == 'done':
        return 'reopened'
    if status == 'open' and old is None and doc.get('from') == OWNER_FROM and not doc.get('pm_ack'):
        return 'added'
    return None


def schedule(attempt):
    """Seconds after try number `attempt` (1, 2, ...) until the next."""
    return SCHEDULE_S[attempt - 1] if 0 < attempt <= len(SCHEDULE_S) else HOURLY_S


def guidance(kind, doc_id, event, recipient):
    if event == 'reopened':
        return 'Reopened by the owner: for your information, no ack needed (hub view %s).' % doc_id
    if event == 'added':
        return 'The owner added it: hub view %s, then hub todo ack %s' % (doc_id, doc_id)
    if kind == 'todo':
        return 'A done mark is not proof: verify each step on disk, then: hub todo ack %s' % doc_id
    return ('Read it: hub pending --for %s (or hub view %s). Picking it up acknowledges this push.'
            % (recipient, doc_id))


class PushWorker(object):
    def __init__(self, store, api, push, folder, clock=None, log=None):
        self.store, self.api, self.push, self.folder = store, api, push, folder
        self.clock = clock or time.time
        self.log = log or (lambda text: None)
        self._stop = threading.Event()
        self._thread = None
        self._rev = 0
        self._fps = {}
        self._outbox = {}
        self._dirty = False

    # ------------------------------------------------------------------ life
    def start(self):
        """Read the baseline and the outbox, then start the thread."""
        self.begin()
        self._thread = threading.Thread(target=self._run, name='hub-push', daemon=True)
        self._thread.start()

    def begin(self):
        """The baseline (every item's fingerprint now) and the outbox, with any outbox entry
        whose item changed since fired afresh: start()'s first half, and a test's."""
        rev, docs = self.store.snapshot()
        self._rev = rev
        self._fps = self._prints(docs)
        self._outbox = self._load()
        for key, entry in list(self._outbox.items()):
            now_fp = self._fps.get(key)
            if now_fp is None:
                self._drop(key)
            elif now_fp != entry.get('fingerprint'):
                kind, doc_id = key.split(':', 1)
                self._fire(kind, doc_id, docs[_coll(kind)][doc_id], entry.get('fingerprint'))
        self._save()

    def stop(self, timeout=STOP_S):
        """Ask the thread to end and join it; True when it ended (or never started)."""
        self._stop.set()
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    changes = self.store.wait(self._rev, WAIT_SLICE_S)
                    if self._stop.is_set():
                        break
                    self._round(changes)
                except HubError as e:
                    if e.code == 'store':
                        break                       # the store closed under us: the hub is closing
                    self.log('push: %s' % e)
                    self._stop.wait(WAIT_SLICE_S)
                except Exception as e:              # noqa: BLE001 - the worker never dies of one round
                    self.log('push: round failed: %s: %s' % (type(e).__name__, e))
                    self._stop.wait(WAIT_SLICE_S)
        finally:
            self._save()

    def poll(self):
        """One round now, on the caller's thread (tests)."""
        self._round(self.store.changes_since(self._rev))
        self._save()

    # ------------------------------------------------------------------ one round
    def _round(self, changes):
        if changes.reset:
            rev, docs = self.store.snapshot()
            fresh = self._prints(docs)
            for key, fp in fresh.items():
                if fp != self._fps.get(key):
                    kind, doc_id = key.split(':', 1)
                    self._fire(kind, doc_id, docs[_coll(kind)][doc_id], self._fps.get(key))
            for key in set(self._fps) - set(fresh):
                self._drop(key)
            self._fps, self._rev = fresh, rev
        else:
            seen = set()
            for ev in changes.events:
                kind = COLLECTIONS.get(ev.collection)
                if kind is None or (kind, ev.id) in seen:
                    continue
                seen.add((kind, ev.id))
                self._changed(kind, ev.id)
            self._rev = changes.revision
            if not changes.events:
                self.store.get('questions', '')     # HubError('store') once the store has closed
        self._acks()
        self._send_due()

    def _changed(self, kind, doc_id):
        key = '%s:%s' % (kind, doc_id)
        doc = self.store.get(_coll(kind), doc_id)
        if doc is None:
            self._fps.pop(key, None)
            self._drop(key)
            return
        old, fp = self._fps.get(key), fingerprint(kind, doc)
        self._fps[key] = fp
        if fp != old:
            self._fire(kind, doc_id, doc, old)

    def _fire(self, kind, doc_id, doc, old):
        key = '%s:%s' % (kind, doc_id)
        event = trigger(kind, doc, old)
        if event is None:
            self._drop(key)             # a change that fires nothing replaces what was owed
            return
        sender = doc.get('from') if isinstance(doc.get('from'), str) else ''
        keys = [PM_KEY]
        if event != 'added' and sender and sender not in (PM_KEY, OWNER_FROM) and not self._is_pm(sender):
            keys.insert(0, sender)
        now = self.clock()
        recipients = {}
        for r in keys:
            held = r != PM_KEY and self._handed(kind, doc_id) == r
            recipients[r] = {'attempt': 0, 'next_at': now, 'last': None,
                             'once': event == 'reopened' or held}
        self._outbox[key] = {'kind': kind, 'id': doc_id, 'event': event,
                             'fingerprint': fingerprint(kind, doc), 'status': doc.get('status'),
                             'repo': doc.get('repo') if isinstance(doc.get('repo'), str) else '',
                             'from': sender, 'first_at': None, 'recipients': recipients}
        self._dirty = True

    def _is_pm(self, key):
        try:
            return bool(self.push.is_pm(key))
        except Exception as e:          # noqa: BLE001 - a host fault: send it as its own key
            self.log('push: is_pm(%s) failed: %s' % (field(key), type(e).__name__))
            return False

    def _handed(self, kind, doc_id):
        fn = getattr(self.api, 'handed_to', None)
        return fn(kind, doc_id) if fn is not None else None

    def _drop(self, key):
        if self._outbox.pop(key, None) is not None:
            self._dirty = True

    def _acked(self, entry, recipient, doc):
        """True when `recipient` has nothing more to hear of entry (module docstring)."""
        if doc is None or fingerprint(entry['kind'], doc) != entry['fingerprint']:
            return True
        if entry['event'] == 'reopened':
            return False                # only a written try acknowledges it
        kind = entry['kind']
        # a recipient that held the item when it fired (`once`) is acknowledged by a written try,
        # never by handed_to: that hand-over may be the old one (todos_done forgets it only after
        # the store change this worker may already have seen)
        picked = (recipient != PM_KEY and not entry['recipients'][recipient].get('once')
                  and self._handed(kind, entry['id']) == recipient)
        if kind == 'question':
            return doc.get('status') != 'answered' or picked
        if doc.get('pm_ack'):
            return True
        if entry['event'] == 'done' and doc.get('status') != 'done':
            return True
        return picked

    def _acks(self):
        for key, entry in list(self._outbox.items()):
            doc = self.store.get(_coll(entry['kind']), entry['id'])
            for r in list(entry['recipients']):
                if self._acked(entry, r, doc):
                    del entry['recipients'][r]
                    self._dirty = True
            if not entry['recipients']:
                self._drop(key)

    def _send_due(self):
        now = self.clock()
        due = collections.OrderedDict()
        for key, entry in sorted(self._outbox.items(), key=lambda kv: (kv[1]['first_at'] or now, kv[0])):
            for r, st in list(entry['recipients'].items()):
                if entry['first_at'] is not None and now - entry['first_at'] >= GIVE_UP_S:
                    self._note(entry, r, None, st['attempt'], GAVE_UP)
                    del entry['recipients'][r]
                    self._dirty = True
                    continue
                if st['next_at'] <= now:
                    due.setdefault(r, []).append(key)
            if not entry['recipients']:
                self._drop(key)
        for r, keys in due.items():
            if self._stop.is_set():
                return
            self._send(r, keys, now)

    def _send(self, recipient, keys, now):
        shown, rest = keys[:MAX_ITEMS], keys[MAX_ITEMS:]
        lines = []
        for key in shown:
            entry, st = self._outbox[key], self._outbox[key]['recipients'][recipient]
            lines.append('[hub push] %s %s %s repo:%s from:%s route:%s attempt:%d' % (
                entry['kind'], field(entry['id']), field(entry['status']), field(entry['repo']),
                field(entry['from']), field(recipient), st['attempt'] + 1))
            lines.append(guidance(entry['kind'], field(entry['id']), entry['event'], field(recipient)))
        if rest:
            lines.append('+%d more: hub pending --for %s' % (len(rest), field(recipient)))
        try:
            sent = self.push.deliver(recipient, '\n'.join(lines))
            if not isinstance(sent, Sent):
                sent = Sent(*sent)
        except Exception as e:          # noqa: BLE001 - a host fault is an unknown outcome
            self.log('push: deliver to %s failed: %s' % (field(recipient), type(e).__name__))
            sent = Sent(UNCERTAIN, None, None, False)
        state = sent.state if sent.state in (WRITTEN, NOT_WRITTEN, UNCERTAIN, NO_SESSION) else UNCERTAIN
        for key in shown:
            entry = self._outbox[key]
            st = entry['recipients'][recipient]
            st['attempt'] += 1
            st['last'] = state
            st['next_at'] = now + schedule(st['attempt'])
            if entry['first_at'] is None:
                entry['first_at'] = now
            if state != WRITTEN:
                self._note(entry, recipient, sent.name, st['attempt'], state)
            if sent.ambiguous:
                self._note(entry, recipient, sent.name, st['attempt'], AMBIGUOUS)
            if state == WRITTEN and st.get('once'):
                del entry['recipients'][recipient]
            if not entry['recipients']:
                self._drop(key)
        self._dirty = True

    # ------------------------------------------------------------------ files
    def _prints(self, docs):
        out = {}
        for coll, kind in COLLECTIONS.items():
            for doc_id, d in docs.get(coll, {}).items():
                out['%s:%s' % (kind, doc_id)] = fingerprint(kind, d)
        return out

    def _load(self):
        path = os.path.join(self.folder, OUTBOX)
        try:
            with open(path, encoding='utf-8-sig') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            self.log('push: %s unreadable, starting empty: %s' % (OUTBOX, type(e).__name__))
            return {}
        if not isinstance(data, dict):
            return {}
        good = {}
        for key, e in data.items():
            if _entry_ok(key, e):
                good[key] = e
        return good

    def _save(self):
        if not self._dirty:
            return
        path = os.path.join(self.folder, OUTBOX)
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(self._outbox, indent=1, sort_keys=True) + '\n')
            os.replace(tmp, path)
            self._dirty = False
        except OSError as e:
            self.log('push: could not write %s: %s' % (OUTBOX, e))

    def _note(self, entry, recipient, name, attempt, reason):
        line = {'ts': datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc)
                .isoformat(timespec='seconds'),
                'kind': entry['kind'], 'id': field(entry['id']), 'status': field(entry['status']),
                'recipient': field(recipient), 'name': field(name) if name else None,
                'attempt': attempt, 'reason': reason}
        path = os.path.join(self.folder, UNDELIVERED)
        try:
            if os.path.getsize(path) >= LOG_MAX:
                os.replace(path, path + '.1')
        except OSError:
            pass
        try:
            with open(path, 'a', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(line, sort_keys=True) + '\n')
        except OSError as e:
            self.log('push: could not write %s: %s' % (UNDELIVERED, e))


def _coll(kind):
    return 'questions' if kind == 'question' else 'todos'


def _entry_ok(key, e):
    """An outbox entry as this module writes it (a hand-edited or older one is dropped)."""
    if not isinstance(key, str) or not isinstance(e, dict) or ':' not in key:
        return False
    kind, doc_id = key.split(':', 1)
    if kind not in COLLECTIONS.values() or e.get('kind') != kind or e.get('id') != doc_id:
        return False
    if not isinstance(e.get('fingerprint'), list) or not isinstance(e.get('recipients'), dict):
        return False
    if e.get('event') not in ('answered', 'done', 'reopened', 'added'):
        return False
    for st in e['recipients'].values():
        if (not isinstance(st, dict) or type(st.get('attempt')) is not int
                or not isinstance(st.get('next_at'), (int, float)) or isinstance(st.get('next_at'), bool)):
            return False
    return e.get('first_at') is None or isinstance(e.get('first_at'), (int, float))
