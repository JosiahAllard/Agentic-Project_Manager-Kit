"""The nodes' mirror of main, refreshed after each landing (docs/load-nodes.md "A landing's merged
gate on a node", "The mirror of main"). Main's side only: kitlib.gate.land calls refresh once a
landing has landed and holds nothing.

  refresh  for each registry node that opted the repository in, push the landed merge to its
           mirror's refs/pmkit/main/<main> (box.MAIN_REF), never forced, all nodes at once under
           one time cap

The node's mirror refuses deletes and non-fast-forwards (box.Box.mirror_for_push), and main only
moves forward, so a push that goes keeps the mirror at main's tip and the next job's push small.
Nothing depends on it: every failure (refused, not a fast-forward, a node that cannot be reached,
no answer within the cap) is one WARNING line, and refresh never raises for one. Nothing here
prints: `say(text)` gets every line.
"""
import os
import threading
import time

from kitlib.loadnode import box, job, registry, remote

# The most a refresh adds to a landing's return. Each push's git is stopped at TIMEOUT_S, and the
# landing waits JOIN_SLACK_S more for it to say so. A fast-forward push of one landing's new
# objects over the LAN takes seconds; a node that is down fails at ssh's ConnectTimeout
# (remote.CONNECT_S). An estimate, not measured: no live node has been timed yet.
TIMEOUT_S = 60
JOIN_SLACK_S = 5


def refresh(root, repo, main, sha, store=None, say=None, open_link=None, timeout_s=TIMEOUT_S,
            registry_file=None, git=None, clock=time.monotonic):
    """Push `sha` (the landed merge, a full hash) from the checkout `root` to the mirror of
    `repo` (runner.repo_name) on each registry node whose `repos` include it, as
    refs/pmkit/main/<main>, never forced; one thread per node, every push stopped at `timeout_s`.
    Says one line per node: that it went, or a WARNING why not. Silent, asking no node, when
    there is no registry or no node opted the repository in; registry_file None on a test store
    (`store.testing`) is no registry. store: the landing's machine-lock store (None: the
    machine's). open_link(node) is a context giving a remote.Link (default remote.Link) and git
    job.push_refs's: the tests' stand-ins."""
    say = say or (lambda text: None)
    if store is None:
        from kitlib.lock.store import Store
        store = Store()
    path = registry_file
    if path is None and not getattr(store, 'testing', False):
        path = registry.path()
    if not path or not os.path.isfile(path):
        return
    try:
        nodes = registry.load(path)
    except registry.RegistryError as e:
        say("WARNING: the load nodes' mirrors of %s were not refreshed: the load node registry "
            'cannot be used: %s' % (main, e))
        return
    nodes = [n for n in nodes if repo in n.repos]
    if not nodes:
        return
    refspec = '%s:%s%s' % (sha, box.MAIN_REF, main)
    why = {}                        # node name -> None (it went) or why not

    def push(node):
        try:
            with (open_link or remote.Link)(node) as link:
                rc, err = job.push_refs(node, link, repo, root, [refspec], git, timeout_s)
            why[node.name] = None if rc == 0 else (job._push_why(err) or 'git exit %s' % rc)
        except Exception as e:      # noqa: BLE001 - one WARNING line, never the landing's
            why[node.name] = '%s: %s' % (type(e).__name__, e)

    threads = [threading.Thread(target=push, args=(n,), daemon=True) for n in nodes]
    for t in threads:
        t.start()
    deadline = clock() + timeout_s + JOIN_SLACK_S
    for t in threads:
        t.join(max(0, deadline - clock()))
    for n in nodes:
        if n.name not in why:
            say("WARNING: %s was not pushed to load node %s's mirror of %s: no answer in %s s"
                % (sha[:10], n.name, main, timeout_s))
        elif why[n.name] is None:
            say("pushed %s to load node %s's mirror of %s" % (sha[:10], n.name, main))
        else:
            say("WARNING: %s was not pushed to load node %s's mirror of %s: %s"
                % (sha[:10], n.name, main, why[n.name]))
