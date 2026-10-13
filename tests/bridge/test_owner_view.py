"""Opening the human chooser neither grants routes nor stops the broker."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest

from tests.bridge.registered import use_codex_registered
from unittest.mock import patch

from optional.bridge.win.lifecycle import Supervisor
from optional.bridge.win.frontend import service_target
from optional.bridge.win.rpc import request
from optional.bridge.win.runtime import load_config,run_service
from optional.bridge.win.owner_view import open_connections


class OwnerViewTests(unittest.TestCase):
    def setUp(self):
        # Their configurations name Codex, served only once hosts.json registers it.
        use_codex_registered(self)
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-owner-view-')
        self.root=Path(self.temp.name)/'state'
        self.kit=Path(__file__).resolve().parents[2]

    def tearDown(self): self.temp.cleanup()

    def test_verified_private_request_only_signals_owner_window(self):
        with Supervisor(self.root,self.kit,sys.executable,{'codex':sys.executable}) as running:
            worker=threading.Thread(target=running.run)
            worker.start()
            try:
                reply=request(service_target(running.marker),{'protocol':1,'view':'connections'})
                self.assertEqual(reply,{'protocol':1,'response':{'requested':True}})
                self.assertTrue(running.owner_view.is_set())
                self.assertEqual(running.controller.pairs.registered(),())
                self.assertTrue(running.leader.owns())
                running.owner_view.clear()
                open_connections(running.marker)
                self.assertTrue(running.owner_view.is_set())
                running.owner_view.clear()
                rejected=request(service_target(running.marker),
                    {'protocol':1,'view':'connections','owner_approved':True})
                self.assertIn('error',rejected)
                self.assertFalse(running.owner_view.is_set())
            finally:
                running.stop.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())

    def test_same_broker_survives_closing_and_reopening_real_owner_windows(self):
        config_path=Path(self.temp.name)/'runtime.json'
        config_path.write_text(json.dumps({'protocol':1,'kit_root':str(self.kit),
            'python':sys.executable,'state_root':str(self.root),'host_id':'owner-view-test',
            'native_hosts':{'codex':sys.executable},'profiles':{'codex':{'profile_root':self.temp.name}}}))
        config=load_config(config_path)
        stop=threading.Event()
        closed=threading.Event()
        observed=[]
        failures=[]
        owner_thread=threading.get_ident()
        class Provider:
            def __init__(provider,controller,**settings): pass
            def close(provider): pass
        def window(root,owner,**settings):
            root.withdraw()  # Owned real Tk event loop; no user window or grants.
            observed.append(threading.get_ident())
            if len(observed)==1:
                def close():
                    root.destroy()
                    closed.set()
                root.after(20,close)
            else: root.after(20,stop.set)
        def probe():
            import time
            marker=self.root/'service.json'
            deadline=time.monotonic()+5
            try:
                while not marker.exists() and time.monotonic()<deadline: stop.wait(.01)
                target=service_target(marker)
                first=request(target,{'protocol':1,'view':'connections'})
                self.assertTrue(first['response']['requested'])
                self.assertTrue(closed.wait(5))
                self.assertFalse(stop.is_set())
                self.assertEqual(service_target(marker),target)
                second=request(target,{'protocol':1,'view':'connections'})
                self.assertTrue(second['response']['requested'])
                if not stop.wait(5): raise AssertionError('second owner window unavailable')
            except Exception as error: failures.append(error)
            finally: stop.set()
        worker=threading.Thread(target=probe)
        worker.start()
        with patch('optional.bridge.ui.window.Window',new=window):
            try: run_service(config,{'codex':Provider},stop=stop)
            finally:
                stop.set()
                worker.join(timeout=6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures,[])
        self.assertEqual(observed,[owner_thread,owner_thread])
        self.assertFalse((self.root/'service.json').exists())
