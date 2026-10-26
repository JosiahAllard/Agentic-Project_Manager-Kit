"""Tests for kitlib.proc.tcp: the connection table's rows parsed, both ends of a loopback
connection paired, and a real connection this test opens (port 0) found with its owner."""
import os
import socket
import struct
import unittest

from kitlib.proc import tcp
from tests.platforms import windows_only


def v4_row(state, la, lp, ra, rp, pid):
    port = lambda p: struct.unpack("<H", struct.pack(">H", p))[0]   # noqa: E731
    return struct.pack("<6I", state, struct.unpack("<I", la)[0], port(lp),
                       struct.unpack("<I", ra)[0], port(rp), pid)


def v6_row(state, la, lp, ra, rp, pid):
    port = lambda p: struct.unpack("<H", struct.pack(">H", p))[0]   # noqa: E731
    return struct.pack("<16sII16sIIII", la, 0, port(lp), ra, 0, port(rp), state, pid)


LO4 = b"\x7f\0\0\x01"
LO6 = b"\0" * 15 + b"\x01"
MAPPED = b"\0" * 10 + b"\xff\xff" + LO4


class Parse(unittest.TestCase):
    def test_v4_rows_keep_the_established_ones_with_their_ports(self):
        buf = struct.pack("<I", 3) + v4_row(5, LO4, 5000, LO4, 6000, 10) \
            + v4_row(2, LO4, 5000, b"\0" * 4, 0, 10) + v4_row(5, LO4, 6000, LO4, 5000, 30)
        self.assertEqual(tcp.parse_v4(buf), [((4, LO4, 5000), (4, LO4, 6000), 10),
                                             ((4, LO4, 6000), (4, LO4, 5000), 30)])

    def test_v6_rows_and_mapped_ipv4_ends(self):
        buf = struct.pack("<I", 2) + v6_row(5, LO6, 5000, LO6, 6000, 10) \
            + v6_row(5, MAPPED, 7000, MAPPED, 8000, 11)
        self.assertEqual(tcp.parse_v6(buf), [((6, LO6, 5000), (6, LO6, 6000), 10),
                                             ((4, LO4, 7000), (4, LO4, 8000), 11)])

    def test_loopback(self):
        self.assertTrue(tcp.is_loopback((4, b"\x7f\x05\0\x01", 1)))
        self.assertTrue(tcp.is_loopback((6, LO6, 1)))
        self.assertFalse(tcp.is_loopback((4, b"\x0a\0\0\x01", 1)))
        self.assertFalse(tcp.is_loopback((6, b"\xfe\x80" + b"\0" * 13 + b"\x01", 1)))

    def test_peers_pairs_the_two_rows_of_a_connection(self):
        a, b = (4, LO4, 5000), (4, LO4, 6000)
        self.assertEqual(tcp.peers({10}, [(a, b, 10), (b, a, 30)]), [(10, 30)])
        self.assertEqual(tcp.peers({10}, [(a, b, 10)]), [(10, None)])
        far = (4, b"\x0a\0\0\x05", 443)
        self.assertEqual(tcp.peers({10}, [(a, far, 10)]), [])

    def test_a_local_interface_address_with_no_peer_row_is_this_machine(self):
        """A remote end at one of this machine's own non-loopback addresses (the local end of a
        row in the table) with no row owning it is None, never another host."""
        lan, lan2, far = b"\x0a\0\0\x02", b"\x0a\0\0\x07", b"\x0a\0\0\x05"
        a = (4, lan, 5000)
        self.assertEqual(tcp.peers({10}, [(a, (4, lan, 6000), 10)]), [(10, None)])
        seen = ((4, lan2, 7000), (4, far, 443), 70)     # lan2 is local: some row holds it
        self.assertEqual(tcp.peers({10}, [(a, (4, lan2, 6000), 10), seen]), [(10, None)])
        v6 = b"\xfe\x80" + b"\0" * 13 + b"\x02"
        self.assertEqual(tcp.peers({10}, [((6, v6, 5000), (6, v6, 6000), 10)]), [(10, None)])
        self.assertEqual(tcp.peers({10}, [(a, (4, far, 443), 10)]), [], "another host")


@windows_only("the connection table is Windows only")
class RealTable(unittest.TestCase):
    def test_a_connection_this_test_opens_is_found_with_its_owner(self):
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        cli = socket.create_connection(srv.getsockname(), timeout=10)
        self.addCleanup(cli.close)
        acc, _ = srv.accept()
        self.addCleanup(acc.close)
        conns = tcp.connections()
        port = srv.getsockname()[1]
        mine = [(lo, re) for lo, re, pid in conns if pid == os.getpid()
                and port in (lo[2], re[2])]
        self.assertEqual(len(mine), 2, mine)
        ours = [c for c in conns if port in (c[0][2], c[1][2])]
        self.assertEqual(tcp.peers({os.getpid()}, ours), [(os.getpid(), os.getpid())] * 2)


if __name__ == "__main__":
    unittest.main()
