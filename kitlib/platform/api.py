"""The seam's contract (docs/linux-support-plan.md section 2.1): each primitive's name, signature
and meaning, and no OS code. Every backend provides every name: kitlib.platform.windows by
re-exporting today's kitlib.proc function unchanged, kitlib.platform.linux by a stub that raises
PlatformUnsupported until its slice builds it. A process is named by (pid, ctime), ctime its
creation time in the backend's own units, so a reused pid is never taken for the process.

Process (linux: slice 4)
  alive(pid, ctime)                 False only when that process is surely gone
  proc_ctime(pid)                   the creation time of pid, or None
  own_ctime()                       this process's creation time
  ancestry(pid, ctime, parents=None, limit=64, through_exited=False)
                                    ([(pid, ctime)] of its ancestors, whether the last has exited)
  descendants(pid, ctime, snap=None)
                                    [(pid, ctime)] of every live process under it
  command_line(pid, ctime=None)     its command line, or None
  kill_tree(pid, ctime, code=1, snap=None)
                                    end it and every live process under it, each verified by
                                    its ctime; {pid: result}

Hardware (linux: slice 6)
  threads()                         every active logical processor's global id, ascending
  all_mask()                        the mask of threads()
  l3_domains()                      [global mask] of each L3 cache domain; [] when not known
  memory_mb()                       {total_mb, free_mb} of physical memory now, or None

Affinity (linux: slice 5)
  affinity_of(pid, ctime)           the process's affinity mask, or None
  set_affinity(pid, ctime, mask)    pin the process to `mask`
"""

PROCESS = ('alive', 'proc_ctime', 'own_ctime', 'ancestry', 'descendants', 'command_line',
           'kill_tree')
HARDWARE = ('threads', 'all_mask', 'l3_domains', 'memory_mb')
AFFINITY = ('affinity_of', 'set_affinity')

# Each area's names and the slice of docs/linux-support-plan.md section 8 that builds it on Linux.
AREAS = {'process': (PROCESS, 4), 'hardware': (HARDWARE, 6), 'affinity': (AFFINITY, 5)}
NAMES = PROCESS + HARDWARE + AFFINITY
