id: BIND-DISCORD-LIVECHECK
area: bind
priority: 3
priority_by: agent
effort: s
state: open
blocked_on: -
scope: worker/binding/src/main/java/org/alpha/binding/runelite/RuneLiteRuntimeConfig.java
summary: one worker run (owner's go) confirms runelite.discord.enable=false holds live: no 'Initializing Discord RPC service.' in client.log; Procmon shows no discord-rpc.dll load, no HKCU\Software\Classes\discord-<appid> write, no discord-ipc pipe

Header copied from a repository's docs/backlog (the incident behind backlog_lint).
