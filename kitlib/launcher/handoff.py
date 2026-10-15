"""Account hand-off and recycle (docs/launcher.md "Hand-off").

Run after a session has replied "ready to hand off: <state file>" (or "ready to restart: <state
file>" after a work-set wind-down): it starts the successor through start.start and records the
move in sessions.json (and coordinators.json for a repo's main coordinator). The old session keeps
running idle; once the successor has checked in, the PM stops it (recycle prints the line).

  --session KEY --account N|auto     a successor <stem>-a<N> on another account
  --all --account N|auto             every entry, agent sessions first, then plain ones
  --session KEY --recycle            a fresh successor on the same account (or --account):
                                     <stem>-a<N>-r<k>, k counting up from 2
  --session KEY --stem NEW ...       either of the above under a new stem: the successor is
                                     NEW-a<N> (no -r<k>: the old name has another stem), and
                                     sessions.json's stem becomes NEW
  --session KEY --state FILE ...     the note the successor is told to read, in place of the entry's
                                     'state' (the path the session replied with)
Refused: an account that is not signed in, and (without --recycle) the account the session is
already on (--all skips that entry instead), an entry without a stem (--all skips it), --stem
with --all, --state with --all, a stem that is not a name, and coordinators.json still under its old name (read
before anything starts). Files are rewritten only after the start succeeds.
"""
import re

from kitlib.launcher import accounts as A
from kitlib.launcher import start as S
from kitlib.launcher import state as files        # sessions.json and coordinators.json
from kitlib.launcher.settings import LaunchError

_GEN = re.compile(r'-r(\d+)(\s|$)')
STEM = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')


def successor_name(entry, target, recycle, stem=None):
    """<stem>-a<N>, with -r<k> on a recycle under the same stem. A new stem gets no -r<k>:
    its name cannot equal the old one's."""
    if stem and stem != entry['stem']:
        return '%s-a%s' % (stem, target)
    new = '%s-a%s' % (entry['stem'], target)
    if recycle:
        m = _GEN.search(entry.get('name') or '')
        new += '-r%d' % (int(m.group(1)) + 1 if m else 2)
    return new


def successor_prompt(entry, old, new, state=None):
    """The successor's first message. `state` names the hand-off note, overriding entry['state']."""
    state = state or entry.get('state')
    if entry.get('agent'):
        return ("Start up now: run your start-up routine, read %s (the hand-off note from %s), check in with the PM "
                "(the session named %s), then act on the note. Before re-dispatching work on any worktree, confirm "
                "the old session's agents there have stopped (no process with that worktree as cwd, no file changes "
                "for 5 minutes); if one is still running, wait and tell the PM." % (state, old, S.PM_NAME))
    return ('You are %s. Read your brief %s and %s, then check in with the PM (the session named %s).'
            % (new, entry.get('brief'), state, S.PM_NAME))


def handoff(settings, host, session=None, account=None, all_=False, recycle=False, whatif=False, stem=None,
            state=None, home_worktree=False):
    if not all_ and not session:
        raise LaunchError('Give --session <key of sessions.json>, or --all.')
    if all_ and recycle:
        raise LaunchError('--recycle takes one --session, not --all.')
    if state is not None and all_:
        raise LaunchError('--state takes one --session, not --all.')
    if stem is not None:
        if all_:
            raise LaunchError('--stem takes one --session, not --all.')
        if not STEM.match(stem):
            raise LaunchError("--stem '%s' must be 1-64 characters of letters, digits, '_' and '-', "
                              "starting with a letter or digit." % stem)
    if not account:
        if not recycle:
            raise LaunchError('Give --account N|auto.')
        account = 'current'
    out = []
    sessions = files.read_sessions(settings)
    coordinators = files.read_coordinators(settings)  # before anything starts: a refusal here starts nothing
    if account == 'current':
        entry = sessions.get(session)
        if not isinstance(entry, dict):
            raise LaunchError("sessions.json has no entry '%s'." % session)
        target = str(entry.get('account'))
    elif account == 'auto':
        target, _ = A.pick(settings, host.now())
        out.append('auto picked account %s' % target)
    else:
        target = str(account)
    settings.account_dirs(target)                      # an account launcher.accounts knows
    if not A.account_signed_in(settings, target):
        raise LaunchError('Account %s is not signed in (no %s in %s).'
                          % (target, A.CREDENTIALS, ' or '.join(settings.account_dirs(target))), out)
    if all_:
        real = files.entries(sessions)
        keys = [k for k, v in real if v.get('agent')] + [k for k, v in real if not v.get('agent')]
    else:
        keys = [session]
    if home_worktree:                                  # every successor checked before any starts
        for key in keys:
            S.check_home_entry(settings, sessions.get(key))
    done, stops = [], []
    for key in keys:
        entry = sessions.get(key)
        if not isinstance(entry, dict):
            raise LaunchError("sessions.json has no entry '%s'." % key, out)
        if not entry.get('stem'):
            if all_:
                out.append('SKIP %s : no stem, so no successor name' % key)
                continue
            raise LaunchError("sessions.json entry '%s' has no stem, so no successor name." % key, out)
        if str(entry.get('account')) == target and not recycle:
            if all_:
                out.append('SKIP %s : already on account %s' % (key, target))
                continue
            raise LaunchError('%s is already on account %s (%s); pick another account.'
                              % (key, target, entry.get('name')), out)
        old = entry.get('name')
        new = successor_name(entry, target, recycle, stem)
        args = S.entry_start_args(settings, key, entry, target, successor_prompt(entry, old, new, state),
                                  predecessor=old, name=new)
        out.append('== %s : %s (account %s) -> %s (account %s)' % (key, old, entry.get('account'), new, target))
        try:
            out += S.start(settings, host, whatif=whatif, home_worktree=home_worktree, **args)
        except LaunchError as e:
            e.lines = out + e.lines
            raise
        main = bool(entry.get('agent')) and key == entry.get('repo')
        if whatif:
            out.append('Would set sessions.json %s : account=%s name=%s%s'
                       % (key, target, new, ' stem=%s' % stem if stem else ''))
            if main:
                out.append('Would set coordinators.json %s = %s' % (key, target))
            continue
        entry['account'] = int(target)
        entry['name'] = new
        if stem:
            entry['stem'] = stem
        files.write_json(files.path(settings, files.SESSIONS), sessions)
        if main and coordinators is not None:
            coordinators[entry['repo']] = int(target)
            files.write_json(files.path(settings, files.COORDINATORS), coordinators)
        done.append(key)
        if recycle:
            stops.append(S.stop_cmd(old, successor=new))
    if whatif:
        out.append('WhatIf: nothing started, no files changed.')
    else:
        out.append('Handed off: %s -> account %s. The old windows stay open idle until stopped.'
                   % (', '.join(done), target))
    if stops:
        out.append('After each successor has checked in, the PM stops the old session (--dry-run first):')
        out += ['  ' + s for s in stops]
    return out
