"""The owner's email in the machine config (docs/machine-setup.md "Owner email"; plan decision 27):
the machine config's `owner_email` (kitlib.config.user), which every repository whose `.kit.json`
sets no "owner".email reads (docs/kit-json.md "With no file"). With none, no commit is provable as
the owner's, so every owner-only path refuses every edit.

Where the email comes from, in order:
  - `owner_email` (--owner-email): set when the machine config holds another value or none; the
    plan names the value it replaces;
  - the machine config's own value: kept, nothing to do;
  - `git config user.email`, read in the kit checkout (Host.git_email): only suggested, never
    stored on its own. The note names it and asks the owner to confirm it with --owner-email, and
    tools/machine_setup.py offers it as the answer to its console question on --apply (needs_prompt,
    suggestion), so it is stored only when the owner gives it: a git identity can be any address,
    an agent's among them;
  - none: a note. tools/machine_setup.py asks for it on --apply when it runs in a console
    (needs_prompt).
The write goes through kitlib.machinesetup.machineconfig (the file's other keys and layout kept).
Nothing here prints or exits.
"""
from kitlib.config import ConfigError
from kitlib.config import user as kuser
from kitlib.config.types import Email

from . import machineconfig

KEY = 'owner_email'
SECTION = 'owner'


def valid(email):
    """None when `email` is an email address the machine config accepts, else why not."""
    errs = []
    Email().check(email, KEY, errs)
    return '; '.join(errs) or None


def stored(home):
    """(the machine config's owner_email or None, why it could not be read or None)."""
    try:
        return kuser.owner_email(home), None
    except ConfigError as e:
        return None, str(e)


def found(ctx):
    """`git config user.email` in the kit checkout, stripped, or None (Host.git_email)."""
    get = getattr(ctx.host, 'git_email', None)
    try:
        value = get(ctx.kit) if get else None
    except (OSError, RuntimeError):
        return None
    value = (value or '').strip()
    return value or None


def needs_prompt(ctx):
    """True when the run has no email to store: none given, none stored, and the machine config
    can be read. tools/machine_setup.py then asks for one (offering suggestion())."""
    have, err = stored(ctx.home)
    return not err and not have and not getattr(ctx, 'owner_email', None)


def suggestion(ctx):
    """`git config user.email` when it is an email address, else None: what the console question
    offers. It is stored only when the owner confirms it."""
    value = found(ctx)
    return value if value and not valid(value) else None


def survey(plan, ctx):
    given = (getattr(ctx, 'owner_email', None) or '').strip()
    have, err = stored(ctx.home)
    if err:
        plan.refuse('cannot read the machine config for %s: %s' % (KEY, err))
        return
    if given:
        bad = valid(given)
        if bad:
            plan.refuse('--owner-email: %s' % bad)
            return
        if have == given.lower():
            return
        source = '--owner-email'
    elif have:
        return
    else:
        git = found(ctx)
        if git and not valid(git):
            plan.note(SECTION, 'no owner email stored: git config user.email is %s. If that is the email '
                               'your own commits are authored with, confirm it: pass --owner-email %s (or '
                               'answer the question --apply asks in a console). It is not stored unconfirmed: '
                               'owner-only changes are proven by it. Until one is set, every owner-only path '
                               'refuses every edit (docs/kit-json.md "With no file")' % (git, git))
            return
        plan.note(SECTION, 'no owner email: none in the machine config and %s; pass --owner-email '
                           '<the email your own commits are authored with> (--apply in a console asks '
                           'for it). Until it is set, every owner-only path refuses every edit (docs/'
                           'kit-json.md "With no file")'
                  % ('git config user.email is not an email address (%r)' % git if git
                     else 'git config user.email is unset'))
        return
    snap, why = machineconfig.snapshot(ctx)
    if snap is None:
        plan.refuse('cannot write %s to the machine config: %s' % (KEY, why))
        return
    value = given.strip()
    was = 'was %s' % have if have else 'unset before'
    plan.add(SECTION, 'machine config %s: %s = %s (from %s; %s)' % (snap.path, KEY, value, source, was),
             lambda: write_owner_email(ctx.home, snap, value, replace=bool(have)),
             undo=('set %s back to %s in %s' % (KEY, have, snap.path)) if have
             else ('remove the %s key from %s' % (KEY, snap.path)) if snap.raw is not None
             else 'delete %s' % snap.path)


def write_owner_email(home, snap, email, replace=False):
    """Set owner_email to `email` in the machine config file snap.path (a Snapshot; None: read the
    writable machine config now). RuntimeError when it cannot (machineconfig.set_key)."""
    bad = valid(email)
    if bad:
        raise ValueError(bad)
    if snap is None:
        path = kuser.writable_file(home)
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except FileNotFoundError:
            raw = None
        snap = machineconfig.Snapshot(path, raw)
    machineconfig.set_key(home, snap, KEY, email.strip(), replace=replace)
