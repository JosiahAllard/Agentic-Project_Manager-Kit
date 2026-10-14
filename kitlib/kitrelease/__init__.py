"""One tracked, reviewed shared runtime release for thin host plugins.

Git is used only by export. Verification and relocation need the externally pinned
review identity, not a Git checkout. This library never activates hosts or services.
"""
import hashlib
from pathlib import Path

from .bootstrap import REQUIRED_FILE, RESOURCES, ROOTS, SUBTREE, ReleaseError, verify


def _result(root, files, commit):
    from kitlib import packageexport as P
    identity = hashlib.sha256(P.encoded(P.metadata(files, commit, SUBTREE))).hexdigest()
    return verify(root, identity, commit)


def export(source, commit, destination):
    """Publish allowlisted bytes from exactly one full reviewed source commit."""
    from kitlib import packageexport as P
    source = P.no_links(source)
    files = P.read_paths(source, commit, ROOTS + RESOURCES)
    if REQUIRED_FILE not in files or not any(name.startswith('tools/') for name in files):
        raise ReleaseError('reviewed source lacks mandatory runtime resources')
    # Reject tracked credentials/state/cache paths before publication too.
    from .bootstrap import _allowed
    if not all(_allowed(name) for name in files):
        raise ReleaseError('reviewed source contains non-runtime paths in runtime roots')
    root = P.publish(files, commit, SUBTREE, destination)
    return _result(root, files, commit)


def copy_release(source_root, expected_identity, destination):
    """Relocate a complete verified snapshot through the shared atomic publisher."""
    from kitlib import packageexport as P
    result = verify(source_root, expected_identity)
    source_root = Path(result['root'])
    destination = P.no_links(destination)
    if destination != source_root and destination.is_relative_to(source_root):
        raise ReleaseError('relocation destination would modify the immutable source release')
    marker = P.verify(source_root)
    files = {name: P.no_links(source_root / name).read_bytes() for name in marker['files']}
    if P.metadata(files, result['commit'], SUBTREE) != marker:
        raise ReleaseError('release changed while copying verified bytes')
    verify(source_root, expected_identity, result['commit'])
    root = P.publish(files, result['commit'], SUBTREE, destination)
    return verify(root, expected_identity, result['commit'])


def launch_bindings(root, expected_identity, python):
    """Absolute launch ingredients only; preparation must be regenerated after relocation."""
    result = verify(root, expected_identity)
    from .bootstrap import _no_links
    python = _no_links(python)
    if not python.is_file():
        raise ReleaseError('exact Python executable is unavailable')
    root = Path(result['root'])
    result.update({'python': str(python), 'python_args': ['-I', '-B'],
                   'env': {'PM_KIT_ROOT': str(root), 'PM_KIT_RELEASE_ID': expected_identity}})
    for key, relative in (('front', 'optional/bridge/mcp_frontend.py'),
                          ('service', 'optional/bridge/tools/bridge_service.py')):
        path = root / relative
        if path.is_file():
            result[key] = str(path)
    return result
