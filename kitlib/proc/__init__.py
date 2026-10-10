"""kitlib.proc (ARCHITECTURE.md): the one implementation of process work.

    win32     the shared ctypes bindings
    process   snapshots, creation times, ancestry, verified kills, affinity
    cpu       processor topology over every processor group
    job       job objects: pinned, measured, killed as a tree
    shield    a pid shield for a suspended child, and the pid hold

Windows only.
"""
