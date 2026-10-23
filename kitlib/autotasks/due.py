"""When a hub schedule is due (kitlib.autotasks): pure functions over a schedule document of the
hub's `schedules` collection (kitlib.hubsite.api, SCHEDULE_TASKS, CADENCES, DAYS) and local times.

A schedule names a slot: `daily` at `at` (HH:MM, local time) every day, `weekly` at `at` on each
day in `days`. The last slot is the latest one at or before now. It is due when it is later than the
schedule's baseline: the later of the last start recorded for it (the tick's state file) and the
time the owner set it (`set_at`). A slot missed while the machine was off is due once at the next
tick: only the last slot counts, never each one missed. A schedule with no baseline at all (no
record and no readable `set_at`) is not due: the tick records now as its baseline instead.

Times here are naive local datetimes; an ISO text with an offset is turned into local time first.
Nothing here reads a clock, a file or the hub.
"""
import datetime

DAILY = 'daily'
WEEKLY = 'weekly'
OFF = 'off'
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')   # kitlib.hubsite.api.DAYS, weekday() order

# decide()'s verdicts
DUE = 'due'
NOT_DUE = 'not-due'
DISABLED = 'off'
BAD = 'bad'
FIRST = 'first'


def parse_at(text):
    """(hour, minute) for an HH:MM text, None for anything else."""
    if not isinstance(text, str) or len(text) != 5 or text[2] != ':':
        return None
    h, m = text[:2], text[3:]
    if not (h.isdigit() and m.isdigit()):
        return None
    h, m = int(h), int(m)
    if h > 23 or m > 59:
        return None
    return h, m


def local(text):
    """The naive local datetime an ISO text names (one with an offset is turned into local time;
    one without is taken as local already), None for anything that is no time."""
    if not isinstance(text, str) or not text:
        return None
    try:
        at = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if at.tzinfo is not None:
        at = at.astimezone().replace(tzinfo=None)
    return at


def last_slot(cadence, at, days, now):
    """The latest slot at or before `now` (naive local) of a `cadence` schedule at `at`
    ((hour, minute)) on `days` (names from DAYS, weekly only). None when there is none: an
    unknown cadence, or a weekly schedule with no day it knows."""
    h, m = at
    today = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if cadence == DAILY:
        return today if today <= now else today - datetime.timedelta(days=1)
    if cadence == WEEKLY:
        want = set(DAYS.index(d) for d in (days or ()) if d in DAYS)
        for back in range(8):
            slot = today - datetime.timedelta(days=back)
            if slot.weekday() in want and slot <= now:
                return slot
    return None


def decide(schedule, last, now):
    """(verdict, detail) for the hub `schedule` document given `last`, the naive local datetime
    of its baseline in the state file (None: none), at `now` (naive local):

      DISABLED  cadence off
      BAD       detail says why: an unknown cadence, an `at` that is no HH:MM, a weekly schedule
                with no day
      FIRST     no baseline and no readable set_at: record `now`, start nothing
      NOT_DUE   detail the last slot (None: no slot yet)
      DUE       detail the last slot
    """
    cadence = schedule.get('cadence')
    if cadence == OFF:
        return DISABLED, None
    if cadence not in (DAILY, WEEKLY):
        return BAD, 'unknown cadence %r' % (cadence,)
    at = parse_at(schedule.get('at'))
    if at is None:
        return BAD, 'at %r is no HH:MM' % (schedule.get('at'),)
    slot = last_slot(cadence, at, schedule.get('days'), now)
    if slot is None:
        return BAD, 'weekly with no day it knows (%r)' % (schedule.get('days'),)
    marks = [t for t in (last, local(schedule.get('set_at'))) if t is not None]
    if not marks:
        return FIRST, slot
    return (DUE if slot > max(marks) else NOT_DUE), slot
