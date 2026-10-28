"""The load node's side of `node.py status`: the forced command of the main machine's key.

On the node (Ubuntu, the kit copied to /opt/pm-kit), the main machine's public key is in the node
account's ~/.ssh/authorized_keys as

    from="<main machine's LAN address>",command="/usr/bin/python3 -I -B /opt/pm-kit/optional/loadnode/loadnode.py serve",restrict ssh-ed25519 AAAA... pmkit-main

so whatever the main machine asks to run, sshd runs this instead, with the words it asked for in
SSH_ORIGINAL_COMMAND. serve() answers exactly one: `status`, the node's machine lock and hardware
as one JSON line (kitlib.loadnode.reply). Anything else, its own argument other than `serve`, or no
SSH_ORIGINAL_COMMAND at all, is refused with exit 2 and a fixed message that never repeats what was
sent. Nothing here starts a process or runs what it is sent; reading the lock is the only work.

Exit codes: 0 the status reply; 1 the machine lock could not be read (an ok false reply); 2 refused.
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
REFUSED = 'refused: only status is served'
EXIT_OK, EXIT_LOCK, EXIT_REFUSED = 0, 1, 2


def _lock_snapshot():
    from kitlib.lock import api               # only for a request that is served
    return api.snapshot(history_days=0)


def serve(argv, environ, snapshot_fn=None, version=None):
    """(exit code, reply dict) for this process's arguments `argv` (sys.argv[1:]) and
    environment `environ`. `snapshot_fn()` gives the machine lock snapshot (default
    kitlib.lock.api.snapshot); it is called only for a served request."""
    if list(argv) != ['serve'] or environ.get('SSH_ORIGINAL_COMMAND') != SERVED:
        return EXIT_REFUSED, rp.refusal(REFUSED)
    try:
        snap = (snapshot_fn or _lock_snapshot)()
        reply = rp.build(snap, version or rp.kit_version(KIT_ROOT))
    except Exception as e:                    # any failure is the node's, told as its kind only
        return EXIT_LOCK, rp.refusal('the machine lock cannot be read here (%s)' % type(e).__name__)
    return EXIT_OK, reply


def main(argv=None, environ=None, out=None):
    code, reply = serve(sys.argv[1:] if argv is None else argv,
                        os.environ if environ is None else environ)
    (out or sys.stdout).write(rp.dumps(reply) + '\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
