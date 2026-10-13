"""kitlib.proc (ARCHITECTURE.md): the one implementation of process work.

    win32     the shared ctypes bindings
    process   snapshots, creation times, ancestry, verified kills, affinity
    cpu       processor topology over every processor group
    job       job objects: pinned, measured, killed as a tree
    shield    a pid shield for a suspended child, and the pid hold
    pipe      named pipes, pipe and file ACLs, process SIDs and images (the bridge's base)
    errors    ProcError, which pipe raises
    console   has_console, and the CreateProcess hook `import kitlib` installs: a process with
              no console gets one with no window before its first bare start

Windows only.
"""
