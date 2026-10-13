"""Kernel singleton ownership plus exclusive private bridge endpoint."""
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from optional.bridge.win.leader import ServiceLeader,LeaderError


class ServiceLeaderTests(unittest.TestCase):
    def test_real_ownership_rejects_same_process_recursion_and_releases_for_next_owner(self):
        scope=uuid.uuid4().hex
        with ServiceLeader(scope) as first:
            self.assertTrue(first.owns())
            with self.assertRaises(LeaderError): ServiceLeader(scope)
        self.assertFalse(first.owns())
        with ServiceLeader(scope) as second:
            self.assertTrue(second.owns())

    def test_worker_can_check_supervisor_ownership_but_cannot_release_its_mutex(self):
        with ServiceLeader(uuid.uuid4().hex) as leader:
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(leader.owns).result())
                with self.assertRaises(LeaderError): pool.submit(leader.close).result()
            self.assertTrue(leader.owns())

    def test_retained_private_endpoint_blocks_takeover_even_after_mutex_is_released(self):
        from kitlib.proc.pipe import PrivateListener
        scope=uuid.uuid4().hex
        listener=PrivateListener(r'\\.\pipe\LOCAL\pm-kit-bridge-'+scope)
        try:
            with self.assertRaises(LeaderError): ServiceLeader(scope)
        finally: listener.close()
        with ServiceLeader(scope) as leader: self.assertTrue(leader.owns())
