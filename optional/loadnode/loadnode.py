"""The load node's forced command: the node's side of `node.py status` and `node.py run`.

On the node (Ubuntu, the kit copied to /opt/pm-kit), the main machine's public key is in the node
account's authorized keys as

    from="<main machine's LAN address>",command="/usr/bin/python3 -I -B /opt/pm-kit/optional/loadnode/loadnode.py serve",restrict ssh-ed25519 AAAA... pmkit-main

so whatever the main machine asks to run, sshd runs this instead, with the words it asked for in
SSH_ORIGINAL_COMMAND. serve() answers `status`: the node's machine lock and hardware as one JSON
line (kitlib.loadnode.reply), with the words it serves and its live jobs. The run words (`start`,
`gate`, `follow`, `fetch`, `clean`, `stop` and the one git push form) go to kitlib.loadnode.box,
which checks their parameters before it does anything. Anything else, an argument other than `serve`
(and `--base <absolute folder>`, the jobs' folder for a test node), or no SSH_ORIGINAL_COMMAND at
all, is refused with exit 2 and a fixed message that never repeats what was sent.

`loadnode.py job <job> [--base <folder>]` is the job runner box's `start` and `gate` start
detached; it is never reached over ssh (the forced command's arguments are fixed) and is refused
when SSH_ORIGINAL_COMMAND is set.

Exit codes: 0 the reply; 1 the machine lock could not be read (an ok false reply), or a run word
refused for a reason it names; 2 refused.
"""
import os
import sys

# The kit this folder is in: optional/loadnode/ is two folders below its root. Python's -I leaves
# the script's own folder off sys.path, so the kit root is put there by hand.
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if KIT_ROOT not in sys.path:
    sys.path.insert(0, KIT_ROOT)

import kitlib  # noqa: E402,F401  (every kit entry point imports kitlib first)
from kitlib.loadnode import reply as rp  # noqa: E402

SERVED = 'status'
REFUSED = 'refused: not a command this node serves'
EXIT_OK, EXIT_LOCK, EXIT_REFUSED = 0, 1, 2


def _lock_snapshot():
    from kitlib.lock import api               # only for a request that is served
    return api.snapshot(history_days=0)


def split_base(argv):
    """(the arguments without `--base X`, X or None); (None, None) when --base is not followed by
    one absolute folder or is given twice."""
    argv = list(argv)
    if '--base' not in argv:
        return argv, None
    i = argv.index('--base')
    if i + 1 >= len(argv) or '--base' in argv[i + 1:]:
        return None, None
    base = argv[i + 1]
    if not os.path.isabs(base) or '\x00' in base:
        return None, None
    return argv[:i] + argv[i + 2:], base


def _box(base):
    from kitlib.loadnode import box
    return box.Box(base, runner=[sys.executable, '-I', '-B', os.path.realpath(__file__)])


def serve(argv, environ, snapshot_fn=None, version=None, box=None):
    """(exit code, reply dict) for `status` with this process's arguments `argv`
    (sys.argv[1:]) and environment `environ`. `snapshot_fn()` gives the machine lock snapshot
    (default kitlib.lock.api.snapshot); it is called only for a served request. box: the
    kitlib.loadnode.box.Box whose live jobs the reply counts (default: the base's)."""
    rest, base = split_base(argv)
    if rest != ['serve'] or environ.get('SSH_ORIGINAL_COMMAND') != SERVED:
        return EXIT_REFUSED, rp.refusal(REFUSED)
    try:
        snap = (snapshot_fn or _lock_snapshot)()
        from kitlib.loadnode import box as boxmod
        jobs = (box or _box(base)).live_count()
        reply = rp.build(snap, version or rp.kit_version(KIT_ROOT), verbs=boxmod.VERBS, jobs=jobs)
    except Exception as e:                    # any failure is the node's, told as its kind only
        return EXIT_LOCK, rp.refusal('the machine lock cannot be read here (%s)' % type(e).__name__)
    return EXIT_OK, reply


def main(argv=None, environ=None, out=None, stdin=None, box=None, exec_fn=None, binout=None):
    """Run as sshd runs it. out: where the status line goes (text); stdin and binout: the run
    words' binary streams (default this process's)."""
    argv = sys.argv[1:] if argv is None else list(argv)
    environ = os.environ if environ is None else environ
    rest, base = split_base(argv)
    cmd = environ.get('SSH_ORIGINAL_COMMAND')
    if rest and rest[0] == 'job' and len(rest) == 2 and cmd is None:
        from kitlib.loadnode import box as boxmod
        return boxmod.job_main(rest[1], base, runner=[sys.executable, '-I', '-B',
                                                      os.path.realpath(__file__)])
    if rest == ['serve'] and cmd is not None and cmd != SERVED:
        from kitlib.loadnode import box as boxmod
        bout = binout or sys.stdout.buffer

        def write(data):
            bout.write(data)
            bout.flush()
        code = boxmod.serve(cmd, stdin or sys.stdin.buffer, write, box or _box(base), exec_fn)
        if code is not None:
            return code
    code, reply = serve(argv, environ, box=box)
    (out or sys.stdout).write(rp.dumps(reply) + '\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
