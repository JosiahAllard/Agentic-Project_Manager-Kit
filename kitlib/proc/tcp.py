"""The machine's established TCP connections, each with the pid that owns it: which local process
is at the other end of a connection on this machine (kitlib.proc.daemons asks it of a build
daemon).

An end is (family, address bytes, port): family 4 or 6, the address in network order, the port
a number. An IPv4-mapped IPv6 address (::ffff:a.b.c.d) is named as the IPv4 end it is, so both
ends of one connection are named alike whichever table each sits in.
"""
import ctypes
import struct

ESTABLISHED = 5                         # MIB_TCP_STATE_ESTAB
_V4_ROW = struct.Struct("<6I")          # MIB_TCPROW_OWNER_PID
_V6_ROW = struct.Struct("<16sII16sIIII")  # MIB_TCP6ROW_OWNER_PID
_MAPPED = b"\0" * 10 + b"\xff\xff"


def _port(raw):
    """A table port: network byte order in the low 16 bits of a DWORD."""
    raw &= 0xFFFF
    return ((raw & 0xFF) << 8) | (raw >> 8)


def _end6(addr, port):
    if addr[:12] == _MAPPED:
        return (4, addr[12:], _port(port))
    return (6, addr, _port(port))


def parse_v4(buf):
    """[(local, remote, pid)] of the established rows of a MIB_TCPTABLE_OWNER_PID buffer."""
    n = struct.unpack_from("<I", buf, 0)[0]
    out = []
    for i in range(n):
        state, la, lp, ra, rp, pid = _V4_ROW.unpack_from(buf, 4 + i * _V4_ROW.size)
        if state == ESTABLISHED:
            out.append(((4, struct.pack("<I", la), _port(lp)),
                        (4, struct.pack("<I", ra), _port(rp)), int(pid)))
    return out


def parse_v6(buf):
    """[(local, remote, pid)] of the established rows of a MIB_TCP6TABLE_OWNER_PID buffer."""
    n = struct.unpack_from("<I", buf, 0)[0]
    out = []
    for i in range(n):
        la, _ls, lp, ra, _rs, rp, state, pid = _V6_ROW.unpack_from(buf, 4 + i * _V6_ROW.size)
        if state == ESTABLISHED:
            out.append((_end6(la, lp), _end6(ra, rp), int(pid)))
    return out


def is_loopback(end):
    family, addr = end[0], end[1]
    if family == 4:
        return addr[:1] == b"\x7f"
    return addr == b"\0" * 15 + b"\x01"


def _table(family):
    from . import win32                 # Windows only
    size = win32.DW(0)
    buf = None
    for _ in range(5):                  # the table can grow between the two calls
        err = win32.IPHLP.GetExtendedTcpTable(buf, ctypes.byref(size), False, family,
                                              win32.TCP_TABLE_OWNER_PID_ALL, 0)
        if err == 0 and buf is not None:
            return buf.raw[:size.value]
        if err not in (0, win32.ERROR_INSUFFICIENT_BUFFER):
            raise OSError("GetExtendedTcpTable: windows error %d" % err)
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = win32.DW(len(buf))
    raise OSError("GetExtendedTcpTable: the table kept growing")


def connections():
    """[(local, remote, pid)] of every established TCP connection now, IPv4 and IPv6. Raises
    OSError when a table cannot be read."""
    from . import win32
    return parse_v4(_table(win32.AF_INET)) + parse_v6(_table(win32.AF_INET6))


def peers(pids, conns):
    """[(pid, peer)] for each established connection of a process in `pids` whose other end is
    on this machine: peer is the pid owning that other end, or None when no row owns it (the
    connection is closing, or the table cannot tell). The other end is on this machine when a
    row owns it, when its address is a loopback one, or when its address is the local address
    of any row in `conns` (one of this machine's own interface addresses): a missing row there
    is None, never another host. Only a connection to an address no row has as its local one
    is to another host, and it is not listed."""
    pids = set(pids)
    owner, here = {}, set()
    for local, remote, pid in conns:
        owner[(local, remote)] = pid
        here.add(local[:2])                 # (family, address): this machine's own
    out = []
    for local, remote, pid in conns:
        if pid not in pids:
            continue
        peer = owner.get((remote, local))
        if peer is None and not is_loopback(remote) and remote[:2] not in here:
            continue
        out.append((pid, peer))
    return out
