"""Which folder each host's agent worktrees live in (docs/new-worktree.md "Hosts").

  folder(cfg, host, registry_path)    the repo path `host`'s worktrees go in
  folders(cfg, registry_path)         every registered host's folder, repo.worktrees_dir first

The hosts come from hosts.json through kitlib.host.registry, never from a host's own module, so
kitlib reads host data without importing a host. The precedence, for a host:
  1. `.kit.json` "repo".worktrees_by_host.<host>, the repository's own choice;
  2. "repo".worktrees_dir for the registry's default host: that key predates hosts, its default
     (.claude/worktrees) is the default host's folder, and a repository that set it keeps it;
  3. the host's hosts.json "profile".worktrees_directory.
"""
import os

from kitlib.host import registry


def folder(cfg, host, registry_path=None):
    """The repo path `host` (a registry name) puts its worktrees in. RegistryError for a name
    hosts.json lacks, or a hosts.json that cannot be read."""
    entry = registry.host(host, registry_path)
    override = (cfg.repo.get('worktrees_by_host') or {}).get(entry.name)
    if override:
        return override
    if entry.default:
        return cfg.repo['worktrees_dir']
    return entry.profile.worktrees_directory


def folders(cfg, registry_path=None):
    """Every host's folder, for cleanup that must find any host's worktree: repo.worktrees_dir
    first (bare slugs and messages name it), then each registered host's, duplicates dropped."""
    found = [cfg.repo['worktrees_dir']]
    found.extend(folder(cfg, name, registry_path) for name in registry.names(registry_path))
    return tuple(dict.fromkeys(os.path.normpath(f) for f in found))
