"""The Windows backend of the seam: today's kitlib.proc primitives, re-exported unchanged
(docs/linux-support-plan.md section 2.1, rule 2), so a caller moved onto kitlib.platform runs the
same code it ran before. Imported only by kitlib.platform.backend()."""
from kitlib.proc.cpu import all_mask, l3_domains, threads
from kitlib.proc.process import (affinity_of, alive, ancestry, command_line, descendants,
                                 kill_tree, memory_mb, own_ctime, proc_ctime, set_affinity)

__all__ = ['alive', 'proc_ctime', 'own_ctime', 'ancestry', 'descendants', 'command_line',
           'kill_tree', 'threads', 'all_mask', 'l3_domains', 'memory_mb', 'affinity_of',
           'set_affinity']
