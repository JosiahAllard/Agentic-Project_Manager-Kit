"""Exact plugin MCP scope and callable names for Claude's native interface.

Claude's MCP/hooks references document registered plugin:<plugin>:<server> and
callable mcp__plugin_<plugin>_<server>__<tool> names. An owned native CLI canary
also measured the exact scoped PreToolUse server name and plugin source below.
That measured schema does not establish approved execution or other host versions.
No namespace string grants caller, connection, executor or permission authority.
"""
from dataclasses import dataclass
import re

from optional.bridge.core.mcp import TOOLS
from optional.bridge.core.messages import DeliveryError


_ALLOWED='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-'


def _component(value):
    if (type(value) is not str or not 0<len(value)<=128 or '__' in value
            or any(character not in _ALLOWED for character in value)):
        raise DeliveryError('invalid-claude-plugin-namespace')
    return value


@dataclass(frozen=True)
class PluginMcp:
    """Keep explicit registration components separate from normalized tool names."""
    plugin_name:str
    server_name:str

    def __post_init__(self):
        _component(self.plugin_name)
        _component(self.server_name)

    @property
    def registered_name(self):
        return 'plugin:'+self.plugin_name+':'+self.server_name

    @property
    def tool_prefix(self):
        # Component names are restricted above. Only the documented inserted
        # colon separators require normalization into underscores.
        return 'mcp__plugin_'+self.plugin_name+'_'+self.server_name+'__'

    @property
    def tools(self):
        # A fresh map keeps a consumer's mutation from changing later facts.
        return {self.tool_prefix+tool:{'name':self.registered_name,'source':'plugin'} for tool in TOOLS}

    @property
    def matcher(self):
        return '^'+re.escape(self.tool_prefix)+'(?:'+'|'.join(re.escape(tool) for tool in TOOLS)+')$'
