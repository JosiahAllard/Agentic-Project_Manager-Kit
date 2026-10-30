"""hook.py: the one hook entry of every host (ARCHITECTURE.md "Hosts"; docs/new-host.md; plan
decision 20). A host adds no hook script: its manifest runs this file with its name and the event.

  python boot/hook.py <host> <event> [<part>] < payload.json
      <host> a hosts.json key, <event> one of its hook_events, <part> a named start check its
      adapter offers (start_text), for a start event registered more than once
  python boot/hook.py <host> --settings-entry
      the adapter's settings.json entry that runs this file (for a host whose install writes one)

Before any kit code runs, with the standard library alone:
  1. The host's entry is read from HOSTS, the hosts.json in the folder above this file's boot/:
     the kit's own (every host's plugin root is laid out as the kit, so this is the kit's own
     boot/; docs/new-host.md `vendored` covers a host that would ship a copy). Never from the kit
     about to boot, which a pin has not checked yet.
  2. Fast path: a call of the host's hook.tool_event to a tool its hook.guarded_tools does not
     name (skips) is allowed, exit 0 with nothing written, before the kit boots: in every session
     (fast_path "all") or only in one no launcher marked ("unmarked": MARK unset or blank, as
     kitlib.sessionmark reads it; a test holds the two equal).
  3. boot/kitboot.py beside this file binds the kit (docs/kitboot.md): PM_KIT_ROOT when set, else
     the checkout this file is in (kit_here; its copy case serves only a host that would ship one).
Then the adapter the entry names is imported and kitlib.host.hookmain.dispatch runs the event: the
guard, failing closed, for the tool event (the adapter's DEADLINE, if any, counting from this
file's start), the start context for any other. When the kit will not boot or import, or the answer
cannot be written, a tool event exits BLOCK, which every host so far reads as a block (the reason
on stderr); a start event says in plain text that the kit did not load and exits 0.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.abspath(__file__)
HOSTS = os.path.join(os.path.dirname(HERE), 'hosts.json')
SETTINGS = '--settings-entry'
BLOCK = 2
MARK = 'PM_KIT_ROLE'


def entry_of(host, hosts=HOSTS):
    """The hosts.json entry of `host`, read with json alone; raises on a missing file or host."""
    with open(hosts, encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(host, str) or not isinstance(data, dict) or not isinstance(data.get(host), dict):
        raise KeyError('no host %r in %s' % (host, hosts))
    return data[host]


def tool_of(payload, field):
    """The value at the dotted key `field` of `payload`, else None."""
    for key in field.split('.'):
        payload = payload.get(key) if isinstance(payload, dict) else None
    return payload


def skips(hook, payload, environ):
    """True for a call boot needs no kit for: a tool hook.guarded_tools does not name (an entry
    ending in * is a prefix), in a session hook.fast_path lets through. Hook data of the wrong shape
    (guarded_tools not a non-empty list of names) skips nothing: every call goes to the guard."""
    guarded = hook['guarded_tools']
    if not isinstance(guarded, (list, tuple)) or not guarded or not all(isinstance(g, str) and g for g in guarded):
        return False
    tool = tool_of(payload, hook['tool_field'])
    if not isinstance(tool, str) or any(tool.startswith(g[:-1]) if g.endswith('*') else tool == g
                                        for g in guarded):
        return False
    return hook['fast_path'] == 'all' or hook['fast_path'] == 'unmarked' and not (environ.get(MARK) or '').strip()


def kit_here(entry, root=None):
    """The kitboot.py `here` for kitboot.boot when PM_KIT_ROOT is unset: None when the folder above
    boot/ is a kit (kit.json; this file's own checkout, as for every host today), else, for a copy
    a host's plugin would ship (docs/new-host.md `vendored`; none does now), the checkout as many
    folders above it as the entry's shell has parts, when that holds kit.json; else None, and
    kitboot then refuses unless PM_KIT_ROOT names a kit."""
    root = root or os.path.dirname(HERE)
    if os.path.isfile(os.path.join(root, 'kit.json')):
        return None
    up = os.path.join(root, *['..'] * len(entry['shell'].split('/')))
    return os.path.join(up, 'boot', 'kitboot.py') if os.path.isfile(os.path.join(up, 'kit.json')) else None


def main(argv, stdin, stdout, environ=os.environ, hosts=HOSTS):
    began, argv = time.monotonic(), list(argv)
    if not 2 <= len(argv) <= 3 or argv[1] == SETTINGS and len(argv) != 2:
        raise ValueError('usage: hook.py <host> <event> [<part>] | hook.py <host> %s' % SETTINGS)
    entry, event, part = entry_of(argv[0], hosts), argv[1], (argv[2:] or [None])[0]
    text = '' if event == SETTINGS or stdin is None or stdin.isatty() else stdin.read()
    if event == entry['hook']['tool_event'] and part is None:
        try:
            if skips(entry['hook'], json.loads(text), environ):
                return 0                               # TodoWrite, WebFetch...: nothing to decide
        except Exception:
            pass                                       # hookmain reads it again and denies
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import kitboot
    kit = kitboot.boot(here=kit_here(entry))
    import importlib
    adapter = importlib.import_module(entry['adapter']).ADAPTER
    from kitlib.host import hookmain
    if event == SETTINGS:
        stdout.write(json.dumps(adapter.settings_entry(ENTRY), indent=2) + '\n')
        return 0
    return hookmain.dispatch(adapter, event, kit, text, stdout, ENTRY, argv, part, environ, began)


def starts(argv, hosts=HOSTS):
    """True when argv names a start event of its host (any hook event but the tool event)."""
    try:
        entry = entry_of(argv[0], hosts)
        return argv[1] != entry['hook']['tool_event'] and argv[1] in entry['capabilities']['hook_events']
    except Exception:
        return False


def setup_hint():
    """For a hook command line this entry cannot use (a settings entry written by hand or before
    plan step 14): the machine setup that rewrites it, when this file is in a kit checkout."""
    tool = os.path.join(os.path.dirname(HERE), 'tools', 'machine_setup.py')
    if not os.path.isfile(tool):
        return ''
    return ('; the hook command line may predate this kit: re-run python "%s" (dry run, then --apply; '
            'docs/machine-setup.md)' % tool.replace(os.sep, '/'))


def run(argv, stdin, stdout, stderr, environ=os.environ, hosts=HOSTS):
    """main(), failing as the event must: a start event says why in plain text (exit 0), any other
    exits BLOCK with the reason on stderr. A SystemExit on the way is a failure too, never an exit
    code a tool call could read as an allow."""
    try:
        return main(argv, stdin, stdout, environ, hosts)
    except BaseException as e:                         # nothing written: block, or say why
        try:
            if starts(argv, hosts):
                stdout.write('PM kit%s: the kit did not load: %s: %s\n'
                             % (' (%s)' % argv[2] if len(argv) > 2 else '', type(e).__name__, e))
                return 0
            hint = setup_hint() if isinstance(e, (ValueError, KeyError)) else ''
            stderr.write('kit hook: %s: %s%s\n' % (type(e).__name__, e, hint))
        except BaseException:
            pass
        return 0 if starts(argv, hosts) else BLOCK


if __name__ == '__main__':
    sys.exit(run(sys.argv[1:], sys.stdin, sys.stdout, sys.stderr))
