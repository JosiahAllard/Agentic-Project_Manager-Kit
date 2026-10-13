"""Actual private directory, marker and singleton supervisor lifecycle."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from optional.bridge.win.lifecycle import Supervisor,private_root
from optional.bridge.win.frontend import service_target
from optional.bridge.win.leader import LeaderError
from kitlib.proc.process import own_ctime


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-lifecycle-')
        self.root=Path(self.temp.name)/'private-state'
        self.kit=Path(__file__).resolve().parents[2]

    def tearDown(self): self.temp.cleanup()

    def supervisor(self):
        return Supervisor(self.root,self.kit,sys.executable,{'codex':sys.executable})

    def test_marker_names_live_owned_service_then_disappears_on_clean_stop(self):
        with self.supervisor() as running:
            target=service_target(self.root/'service.json')
            self.assertEqual((target.pid,target.creation),(os.getpid(),own_ctime()))
            self.assertEqual(target.name,running.leader.endpoint)
            self.assertTrue(running.leader.owns())
            self.assertTrue((self.root/'pairings.sqlite').is_file())
            self.assertTrue((self.root/'outbox.sqlite').is_file())
        self.assertFalse((self.root/'service.json').exists())

    def test_same_state_cannot_start_second_supervisor_or_replace_live_marker(self):
        with self.supervisor() as first:
            before=(self.root/'service.json').read_bytes()
            with self.assertRaises(LeaderError): self.supervisor()
            self.assertTrue(first.leader.owns())
            self.assertEqual((self.root/'service.json').read_bytes(),before)

    def test_unprotected_existing_directory_is_rejected_without_changing_its_acl(self):
        from optional.bridge.core.messages import DeliveryError
        self.root.mkdir()
        with self.assertRaises(DeliveryError): private_root(self.root)
        self.assertFalse((self.root/'service.json').exists())

    def test_close_does_not_delete_a_different_marker(self):
        running=self.supervisor()
        marker=self.root/'service.json'
        marker.write_text(json.dumps({'protocol':1,'pipe':running.leader.endpoint,
            'pid':os.getpid(),'creation':own_ctime()+1}),encoding='utf-8')
        running.close()
        self.assertTrue(marker.exists())

    def test_wrong_thread_close_leaves_owner_able_to_close_all_resources(self):
        running=self.supervisor()
        errors=[]
        def close_elsewhere():
            try: running.close()
            except LeaderError as error: errors.append(error.code)
        worker=threading.Thread(target=close_elsewhere)
        worker.start()
        worker.join(timeout=3)
        try:
            self.assertEqual(errors,['supervisor-owner-thread-required'])
            self.assertFalse(running.closed)
            self.assertTrue(running.leader.owns())
            self.assertTrue((self.root/'service.json').exists())
        finally: running.close()
        self.assertFalse(running.leader.owns())
        self.assertFalse((self.root/'service.json').exists())

    def test_failed_component_assembly_does_not_publish_or_retain_leadership(self):
        with self.assertRaises(FileNotFoundError):
            Supervisor(self.root,self.kit,sys.executable,{'codex':self.root/'missing-host.exe'})
        self.assertFalse((self.root/'service.json').exists())
        with self.supervisor() as running: self.assertTrue(running.leader.owns())
