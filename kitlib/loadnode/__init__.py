"""Load nodes: other Linux boxes on the owner's LAN that the main machine may later send heavy
work to. This first slice only lists them and asks each for its status.

registry.py reads the owner's list of nodes (`<PM repo>/.pm-kit/load-nodes.json`, git-ignored).
remote.py asks a node over ssh, the host key pinned, with one fixed word as the command.
reply.py builds the node's answer from its machine lock (on the node) and checks and shows that
answer (on the main machine), where it is untrusted. The node side is optional/loadnode/loadnode.py,
the forced command of the main machine's key on the node. tools/node.py is the command line.
"""
