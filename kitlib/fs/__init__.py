"""kitlib.fs: file primitives that belong to no one feature.

- treelock: TreeLock, an OS byte-range lock on one file (the daily full run's tree lock, the hub
  store's lock, and under writelock every per-file write lock).
- writelock: one writer of a file at a time, across processes and threads (the rulings ledger,
  the pending-steps file).
- replace: os.replace retried while Windows refuses it because a reader or a scanner has the
  target open.
- rmtree: rmtree_force, shutil.rmtree that also removes read-only files (git's objects), and
  plain_dir, whether a path is a folder itself (not a link or junction) before a sweep removes it.

Nothing here prints or exits, and nothing here imports a feature package.
"""
