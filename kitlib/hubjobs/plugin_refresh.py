"""kitlib.hubjobs.plugin_refresh: the plugin refresh job. Its code stays in
kitlib.hubsite.plugin_job (docs/plugin-source.md "Hub refresh" names it there); this module is
what discovery finds."""
from kitlib.hubsite.plugin_job import make  # noqa: F401 - the job's maker, found by kitlib.hubjobs
