id: EX-LINK-PRESENCE-LIVECHECK
area: link
priority: 3
priority_by: agent
effort: s
state: open
blocked_on: -
scope: worker/link/src/main/java/org/example/link/PresenceConfig.java
summary: one worker run (owner's go) confirms presence.enable=false holds live: no 'Initializing presence service.' in client.log; Procmon shows no presence.dll load, no registry write under the presence key, no presence named pipe

Header copied from a repository's docs/backlog (the incident behind backlog_lint).
