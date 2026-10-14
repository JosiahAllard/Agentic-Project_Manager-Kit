"""Controller-only session registration over an existing authoritative runtime service (from the
Codex branch's session_registry.py; it binds runtime.RoleBindings, so it stays with Codex until a second
host has a runtime binding to register).

The supplied resolver is RoleBindings.resolve. Lease and registration callbacks belong
to the existing runtime integration, never to model arguments, environment marks,
thread/loaded/list, or a new plugin store. Without them liveness stays unknown.
"""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import time

from .kit import catalog


_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('invalid runtime session identifier')
    return value


def path_key(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('session scope must be an absolute path')
    return os.path.normcase(str(Path(value).resolve()))


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('invalid runtime lease timestamp')
    return value


@dataclass(frozen=True)
class SessionIdentity:
    thread: str
    session: str
    cwd: str
    role: str | None
    parent: str | None
    repository: str | None = None


@dataclass(frozen=True)
class RuntimeLease:
    """Runtime-provider assertion of process liveness, scoped to one registration.

Generation must change on restart/re-registration. The provider must derive state
from its authoritative live process/lease registration, not native loaded status.
"""
    identity: SessionIdentity
    generation: str
    expires_at: float
    state: str


@dataclass(frozen=True)
class RuntimeInventory:
    authority: str
    observed_at: float
    complete: bool
    leases: tuple


class SessionRegistry:
    """Local binding mirror; shared registration and leases use injected callbacks.

There is no persistence, discovery heuristic, polling thread or cleanup of another
session. A complete fresh runtime inventory is required to rule out duplicates.
"""
    def __init__(self, resolve_binding, *, lease_reader=None, registration_writer=None,
                 authority=None, clock=time.time, classify=None):
        if not callable(resolve_binding) or not callable(clock):
            raise ValueError('trusted binding resolver and clock are required')
        if lease_reader is not None and (not callable(lease_reader) or authority is None):
            raise ValueError('lease reader requires an explicit runtime authority')
        if registration_writer is not None and (not callable(registration_writer) or lease_reader is None):
            raise ValueError('runtime registration requires authoritative lease reads')
        self.resolve_binding, self.lease_reader = resolve_binding, lease_reader
        self.registration_writer, self.authority, self.clock = registration_writer, authority, clock
        from kitlib.git import repo
        self.classify = classify or repo.classify
        if authority is not None:
            identifier(authority)
        self.identities = {}

    def _identity(self, binding, repository=None):
        role = binding.role
        if role is not None and role not in catalog():
            raise ValueError('unknown registered kit role')
        if role in {'pm', 'coordinator'} and binding.parent is not None:
            raise ValueError('PM/coordinator registration requires a controller root')
        if role == 'coordinator':
            from kitlib.git import repo
            place = self.classify(binding.cwd)
            if place.kind not in {repo.MAIN, repo.LINKED} or not place.common:
                raise ValueError('coordinator checkout has no verified Git repository identity')
            canonical = path_key(place.common)
            if repository is not None and path_key(repository) != canonical:
                raise ValueError('coordinator repository scope differs from bound checkout Git identity')
            repository = canonical
        return SessionIdentity(identifier(binding.thread), identifier(binding.session),
                path_key(binding.cwd), role,
                None if binding.parent is None else identifier(binding.parent),
                None if repository is None else path_key(repository))

    def _inventory(self):
        if self.lease_reader is None:
            return None
        value = self.lease_reader()
        now = _timestamp(self.clock())
        if (not isinstance(value, RuntimeInventory) or value.authority != self.authority
                or not isinstance(value.complete, bool) or not isinstance(value.leases, tuple)
                or len(value.leases) > 4096):
            raise ValueError('invalid authoritative runtime inventory')
        observed = _timestamp(value.observed_at)
        if not 0 <= now - observed <= 10:
            raise ValueError('authoritative runtime inventory is stale')
        seen = set()
        for lease in value.leases:
            if not isinstance(lease, RuntimeLease) or not isinstance(lease.identity, SessionIdentity):
                raise ValueError('invalid runtime lease')
            identity = lease.identity
            validated = self._identity(identity, identity.repository)
            if validated != identity or identity.thread in seen:
                raise ValueError('ambiguous runtime registration identity')
            seen.add(identity.thread)
            identifier(lease.generation)
            if lease.state not in {'live', 'dead'} or _timestamp(lease.expires_at) > now + 120:
                raise ValueError('invalid bounded runtime lease')
        return value

    def register(self, binding, *, repository=None):
        """Trusted controller binds native identity; a callback publishes if connected."""
        identity = self._identity(binding, repository)
        if self._identity(self.resolve_binding(identity.thread), repository) != identity:
            raise ValueError('native identity changed before session registration')
        prior = self.identities.get(identity.thread)
        if prior is not None:
            if prior != identity:
                raise ValueError('registered identity changed; create a reviewed new registration')
            return self.status(identity.thread)
        if len(self.identities) >= 4096:
            raise ValueError('session registration limit reached')
        if self.registration_writer is not None:
            # The provider must atomically reject duplicate coordinators. A local
            # inventory pre-check cannot close a cross-controller registration race.
            self.registration_writer(identity)
        self.identities[identity.thread] = identity
        return self.status(identity.thread)

    def status(self, thread):
        identity = self.identities.get(identifier(thread))
        if identity is None:
            raise ValueError('session has no reviewed registration')
        result = {'thread': identity.thread, 'session': identity.session, 'cwd': identity.cwd,
                  'role': identity.role, 'liveness': 'unknown', 'generation': None}
        try:
            if self._identity(self.resolve_binding(thread), identity.repository) != identity:
                raise ValueError('native registration identity changed')
            inventory = self._inventory()
            if inventory is None:
                result['reason'] = 'authoritative runtime lease reader is not connected'
                return result
            leases = [lease for lease in inventory.leases if lease.identity.thread == thread]
            if not leases:
                result['reason'] = 'runtime has no registration lease for this thread'
                return result
            lease = leases[0]
            if lease.identity != identity:
                raise ValueError('runtime lease does not match the reviewed registration')
            result['generation'] = lease.generation
            result['liveness'] = ('live' if lease.state == 'live' and lease.expires_at > self.clock() else 'stale')
            return result
        except Exception as error:
            result['reason'] = 'runtime liveness unavailable: %s' % error
            return result

    def coordinator_check(self, repository, *, exclude=None):
        """Never turn missing/partial inventory into evidence that no peer exists."""
        repository = path_key(repository)
        if exclude is not None:
            identifier(exclude)
        try:
            inventory = self._inventory()
            if inventory is None or not inventory.complete:
                raise ValueError('complete authoritative runtime inventory is unavailable')
            from kitlib.git import repo
            known = {lease.identity.repository for lease in inventory.leases
                     if lease.identity.role == 'coordinator'}
            for registered in self.identities.values():
                actual = self.resolve_binding(registered.thread)
                if self._identity(actual, registered.repository) != registered:
                    raise ValueError('bound repository registration changed')
                place = self.classify(actual.cwd)
                if place.kind in {repo.MAIN, repo.LINKED} and place.common:
                    known.add(path_key(place.common))
            if repository not in known:
                # Checkout/main-root aliases are accepted only when shared Git
                # resolves them to a repository already known to this controller.
                place = self.classify(repository)
                if (place.kind not in {repo.MAIN, repo.LINKED} or not place.common
                        or path_key(place.common) not in known):
                    raise ValueError('requested coordinator scope has no known Git repository identity')
                repository = path_key(place.common)
            candidates = [lease for lease in inventory.leases
                          if lease.identity.role == 'coordinator'
                          and lease.identity.repository == repository and lease.identity.thread != exclude]
            live = sorted(lease.identity.thread for lease in candidates
                          if lease.state == 'live' and lease.expires_at > self.clock())
            stale = sorted(lease.identity.thread for lease in candidates if lease.identity.thread not in live)
            return {'status': 'duplicate' if live else ('stale' if stale else 'clear'), 'live': live, 'stale': stale}
        except Exception as error:
            return {'status': 'unknown', 'live': [], 'stale': [], 'reason': str(error)}

    def snapshot(self):
        return tuple(self.status(thread) for thread in sorted(self.identities))
