"""The installed configuration, and the service's assembly of the installed host halves.

The configuration names one profile per host kind; each kind must be a host the bridge serves
(optional/bridge/registry.py), and its settings must match that half's bridge-profile.json.
factories(config) turns the halves (optional/bridge/port.BridgeHost) into the provider and
delivery factories Runtime takes, so nothing here names a host.
"""
from dataclasses import dataclass,field
from pathlib import Path
import threading
import sys

from kitlib.ident import identifier
from optional.bridge.core.messages import DeliveryError
from optional.bridge.core.pairing import ChatBinding
from optional.bridge import paths, registry
from .evidence import document,regular,stable_bytes
from .hosts import native_hosts


@dataclass(frozen=True)
class Config:
    state_root:Path=field(repr=False)
    kit_root:Path=field(repr=False)
    python:Path=field(repr=False)
    host_id:str
    native_hosts:dict=field(repr=False)
    profiles:dict=field(repr=False)


def _absolute(value, *, exists=True):
    if type(value) is not str or not Path(value).is_absolute():
        raise DeliveryError('invalid-runtime-path')
    path=Path(value)
    if exists: regular(path,directory=path.is_dir())
    return path.resolve(strict=exists)


def _profiles(values,kit):
    result={}
    for kind,profile in values.items():
        # Schemas are installed native-provider data, never a configuration URL
        # or an executable/import named by a model or profile.
        # Only a host hosts.json registers, with a half: an unregistered half is never read.
        if kind not in registry.names(kit=kit):raise DeliveryError('native-provider-unavailable')
        regular(registry.half_dir(kind,kit),directory=True)
        required,optional,directories,names=registry.profile_schema(
            document(stable_bytes(registry.profile_path(kind,kit),4096)))
        if not required<=set(profile) or set(profile)-required-optional:
            raise DeliveryError('invalid-native-provider-config')
        settings={}
        for name,value in profile.items():
            if name in names:
                if (type(value) is not str or not value or len(value)>128 or '__' in value
                        or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for character in value)):
                    raise DeliveryError('invalid-native-server-name')
                settings[name]=value
            else:
                path=_absolute(value)
                regular(path,directory=name in directories)
                settings[name]=str(path)
        result[kind]=settings
    return result


def parse_config(data):
    """The same bounded, inert configuration check for preparation and startup."""
    try:
        import json
        if type(data) is not dict or len(json.dumps(data,allow_nan=False).encode('utf-8'))>64*1024:
            raise DeliveryError('invalid-runtime-config')
        expected={'protocol','kit_root','python','state_root','host_id','native_hosts','profiles'}
        if (set(data)!=expected or type(data['protocol']) is not int or data['protocol']!=1
                or type(data['native_hosts']) is not dict or not 0<len(data['native_hosts'])<=16
                or type(data['profiles']) is not dict or set(data['profiles'])!=set(data['native_hosts'])
                or any(type(value) is not dict for value in data['profiles'].values())):
            raise DeliveryError('invalid-runtime-config')
        kinds=native_hosts(data['native_hosts'])
        kit=_absolute(data['kit_root'])
        regular(kit,directory=True)
        if kit!=paths.KIT:
            raise DeliveryError('runtime-kit-root-mismatch')
        python=_absolute(data['python'])
        regular(python)
        if python!=Path(sys.executable).resolve(strict=True):
            raise DeliveryError('runtime-python-mismatch')
        return Config(_absolute(data['state_root'],exists=False),kit,python,
            identifier(data['host_id']),kinds,_profiles(data['profiles'],kit))
    except DeliveryError: raise
    except Exception: raise DeliveryError('invalid-runtime-config') from None


def load_config(path):
    try: return parse_config(document(stable_bytes(Path(path),64*1024)))
    except DeliveryError: raise
    except Exception: raise DeliveryError('invalid-runtime-config') from None


class Runtime:
    """Code-supplied factories only; configuration cannot name modules or grants."""
    def __init__(self,controller,config,factories, *, delivery_factories=None):
        self.controller=controller
        self.providers={}
        self.adapters=[]
        self.closed=False
        try:
            for kind,settings in config.profiles.items():
                factory=factories.get(kind)
                if not callable(factory): raise DeliveryError('native-provider-unavailable')
                if set(settings)&{'controller','host_id','module','source_factory','owner_approved'}:
                    raise DeliveryError('invalid-native-provider-config')
                self.providers[kind]=factory(controller,host_id=config.host_id,**settings)
            for kind,builder in (delivery_factories or {}).items():
                provider=self.providers.get(kind)
                if provider is None or not callable(builder):
                    raise DeliveryError('native-delivery-builder-unavailable')
                adapter=builder(provider,self)
                self.adapters.append(adapter)
                controller.adapter(adapter)
        except Exception:
            self.close()
            raise

    def source_factory(self,native):
        if self.closed: raise DeliveryError('native-provider-unavailable')
        provider=self.providers.get(native.kind)
        if provider is None: raise DeliveryError('native-provider-unavailable')
        return provider(native)

    def hook_sink(self,peer,payload):
        """Route a native hook frame to the one installed provider with a HookSink."""
        if self.closed: raise DeliveryError('native-provider-unavailable')
        sinks=[sink for sink in (getattr(provider,'hook_sink',None) for provider in tuple(self.providers.values()))
            if callable(sink)]
        if len(sinks)!=1: raise DeliveryError('hook-producer-unavailable')
        sinks[0](peer,payload)

    def sender_mode(self,binding):
        """Read one native supplier; no default permission or executor authority."""
        if self.closed or type(binding) is not ChatBinding or not self.controller._live(binding):
            raise DeliveryError('native-sender-mode-unverified')
        modes=[]
        for provider in tuple(self.providers.values()):
            reader=getattr(provider,'sender_mode',None)
            if not callable(reader): continue
            try: mode=reader(binding)
            except Exception: continue
            if type(mode) is not str or mode not in ('prompting','bypass'):
                raise DeliveryError('native-sender-mode-unverified')
            modes.append(mode)
        if len(modes)!=1 or self.closed or not self.controller._live(binding):
            raise DeliveryError('native-sender-mode-unverified')
        return modes[0]

    def refresh(self):
        for provider in self.providers.values():
            refresh=getattr(provider,'refresh',None)
            if callable(refresh): refresh()

    def close(self):
        if self.closed: return
        self.closed=True
        first=None
        for provider in (*reversed(self.adapters),*reversed(tuple(self.providers.values()))):
            cleanup=getattr(provider,'close',None)
            if callable(cleanup):
                try: cleanup()
                except Exception as error:
                    if first is None: first=error
        self.providers.clear()
        self.adapters.clear()
        if first is not None: raise DeliveryError('native-provider-cleanup-unconfirmed') from None


def factories(config,hosts=None):
    """(provider factories, delivery factories) for every profile in `config`, from the served
    halves (`hosts`: {name: BridgeHost}, default registry.installed()). A profile whose host has
    no half refuses the whole start."""
    hosts=registry.installed() if hosts is None else hosts
    providers,deliveries={},{}
    for kind in config.profiles:
        host=hosts.get(kind)
        if host is None: raise DeliveryError('native-provider-unavailable')
        images=config.native_hosts[kind]
        providers[kind]=(lambda host,images:lambda controller,**settings:
            host.provider(controller,images,**settings))(host,images)
        if host.delivery is not None: deliveries[kind]=host.delivery
    return providers,deliveries


def _owner_window(supervisor,stopped):
    import tkinter as tk
    from optional.bridge.ui.window import Window
    window=tk.Tk()
    timer=None
    try:
        Window(window,supervisor.controller.owner,refresh=supervisor.runtime.refresh)
        menu=tk.Menu(window)
        menu.add_command(label='Stop bridge',command=stopped.set)
        window.configure(menu=menu)
        def poll():
            nonlocal timer
            timer=None
            if stopped.is_set(): window.destroy()
            else:
                if supervisor.owner_view.is_set():
                    supervisor.owner_view.clear()
                    window.deiconify()
                    window.lift()
                timer=window.after(100,poll)
        timer=window.after(100,poll)
        window.mainloop()
    finally:
        if timer is not None:
            try: window.after_cancel(timer)
            except tk.TclError: pass
        try: window.destroy()
        except tk.TclError: pass


def run_service(config,factories, *, delivery_factories=None,stop=None,connections=False):
    """Own the singleton on this thread; serve and dispatch on bounded workers."""
    from .lifecycle import Supervisor
    stopped=threading.Event() if stop is None else stop
    failures=[]
    with Supervisor(config.state_root,config.kit_root,config.python,config.native_hosts,
            runtime_builder=lambda controller:Runtime(controller,config,factories,
                delivery_factories=delivery_factories)) as supervisor:
        def work(action):
            try: action()
            except Exception:
                failures.append(True)
                stopped.set()
                supervisor.stop.set()
        def dispatch():
            while not stopped.wait(.2):
                supervisor.controller.dispatch_pending(continue_check=lambda:not stopped.is_set())
        receiver=threading.Thread(target=work,args=(supervisor.run,),name='bridge-requests',daemon=True)
        dispatcher=threading.Thread(target=work,args=(dispatch,),name='bridge-dispatch',daemon=True)
        threads=[receiver,dispatcher]
        try:
            for worker in threads: worker.start()
            if connections: supervisor.owner_view.set()
            while not stopped.wait(.1):
                if supervisor.owner_view.is_set():
                    supervisor.owner_view.clear()
                    try: _owner_window(supervisor,stopped)
                    except Exception:
                        # A missing/failed GUI must not destroy the broker or
                        # its routes. A later owner request may open it again.
                        continue
        finally:
            stopped.set()
            supervisor.stop.set()
            for worker in threads:
                if worker.ident is not None: worker.join(timeout=12)
            if any(worker.is_alive() for worker in threads):
                raise DeliveryError('service-worker-cleanup-unconfirmed')
        if failures: raise DeliveryError('service-worker-unavailable')
