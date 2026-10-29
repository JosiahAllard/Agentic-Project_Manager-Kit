"""kitlib.hubjobs.reminders: the hub server's reminders job (docs/pm-start.md "Config").

  make(sources) -> jobs.Job     the job; None when the machine config names no reminders_file
  RemindersJob(path, ...)       its run(ctx): one look at the reminders file

Every EVERY_S the job reads the reminders file (pm_start.reminders_file: [{"due": "YYYY-MM-DD",
"text": "..."}], the file the PM start checklist reads) and takes the entries due today or
earlier. Its health note is the due count and the first one's text (the job's "show"). The
reminders not pushed before are announced to the PM with one `due` job push (detail: how many
are new). kitlib.hubsite.jobs allows only a token as detail, so the text is not in the push, and
a newer push of a job replaces one still owed, hence one push per run for all the new ones.

Pushed once, also across a restart: the keys (a hash of due and text) of the reminders pushed are
kept in reminders-pushed.json next to the reminders file, written atomically (kitlib.fs.replace)
AFTER the push is handed over, so a crash between the two repeats a push and never loses one. A
key whose reminder left the file is dropped. A state file that cannot be read counts as empty.
The job never edits the reminders file: removing a reminder is the PM's call.

A malformed entry (not an object, no text, a due that is not a date) is skipped and does not
stop the others; the run then raises, naming how many were skipped, so health shows it. A
reminders file that cannot be read or is not a JSON list is a failure too.
"""
import datetime
import hashlib
import json
import os

from kitlib.fs import replace as fsreplace
from kitlib.hubsite import jobs

NAME = 'reminders'
EVERY_S = 300
STATE_FILE = 'reminders-pushed.json'
EVENT = 'due'
NOTE_MAX = 120


def make(sources):
    from kitlib import pmstart
    from kitlib.config import user
    cfg = pmstart.load_config(user.readable_file(getattr(sources, 'home', None)))
    path = (cfg or {}).get('reminders_file')
    if not path:
        return None
    return jobs.Job(NAME, EVERY_S, RemindersJob(os.path.expanduser(path)))


def key(item):
    return hashlib.sha1(('%s\n%s' % (item['due'], item['text'])).encode('utf-8')).hexdigest()[:16]


def _valid(it):
    if not (isinstance(it, dict) and isinstance(it.get('due'), str) and isinstance(it.get('text'), str)):
        return False
    try:
        datetime.date.fromisoformat(it['due'])
    except ValueError:
        return False
    return True


class RemindersJob(object):
    """run(ctx) is the job. `today()` -> a date is the test's seam."""

    def __init__(self, path, today=None):
        self.path = path
        self.state = os.path.join(os.path.dirname(os.path.abspath(path)), STATE_FILE)
        self.today = today or datetime.date.today

    def __call__(self, ctx):
        with open(self.path, encoding='utf-8-sig') as f:
            items = json.load(f)
        if not isinstance(items, list):
            raise ValueError('%s is not a JSON list' % self.path)
        good = [it for it in items if _valid(it)]
        bad = len(items) - len(good)
        limit = self.today().isoformat()
        due = sorted((it for it in good if it['due'] <= limit), key=lambda i: i['due'])
        pushed = self._pushed()
        new = [it for it in due if key(it) not in pushed]
        if new:
            ctx.event(EVENT, str(len(new)))
        # Written after the push; also when only the keys of removed reminders changed.
        keep = sorted((pushed & set(key(it) for it in good)) | set(key(it) for it in new))
        if new or len(keep) != len(pushed):
            self._write(keep)
        if bad:
            raise ValueError('%d malformed reminder%s skipped in %s'
                             % (bad, '' if bad == 1 else 's', self.path))
        if not due:
            return 'none due'
        return '%d due, %d new: %s' % (len(due), len(new), due[0]['text'][:NOTE_MAX])

    def _pushed(self):
        try:
            with open(self.state, encoding='utf-8') as f:
                data = json.load(f)
            return set(k for k in data if isinstance(k, str))
        except (OSError, ValueError, TypeError):
            return set()

    def _write(self, keys):
        tmp = '%s.%d.tmp' % (self.state, os.getpid())
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(keys, f)
        try:
            fsreplace.replace(tmp, self.state)
        except OSError:
            fsreplace.remove(tmp)
            raise
