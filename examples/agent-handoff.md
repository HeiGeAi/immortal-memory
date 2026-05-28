# Agent Handoff Example

Paste this into another local AI agent:

```text
You can use my local Immortal Memory system. First read ~/.immortal/agent/ENTRY.md.
Then run:
immortal-memory agent-context "<current task>" --print
Use the returned context as task-local memory. Do not read raw vault files unless I explicitly ask.
```
