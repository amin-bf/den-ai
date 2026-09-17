# Local AI toolchain

A coding agent (Claude) that hands selected work to models on this machine, which share
one GPU.

## Language

### GPU sharing

**Broker**:
The one local server every GPU user goes through. It decides what holds the GPU.
_Avoid_: proxy, gateway, daemon

**Side**:
One kind of GPU work, LLM or image. Only one side is loaded at a time.
_Avoid_: backend, engine

**Mode**:
The sides that are available: `llm`, `image`, `both` or `off`.
_Avoid_: profile, state

**Swap**:
Unloading one side and loading the other.
_Avoid_: switch (that word means a mode change)

**In-flight request**:
A request the broker has passed on and that hasn't finished yet. A swap or mode change waits for it.
_Avoid_: busy, job

**Caller**:
Who sent a request: `cli`, `claude` or `pi`.
_Avoid_: client, user

### Delegation

**Task**:
A kind of work Claude may delegate to the LLM (summarize, extract, classify, draft), with its own system prompt.
_Avoid_: job, skill

**Delegation**:
One call where Claude runs a task on the local LLM, logged with an id and Claude's verdict.
_Avoid_: request (that's the HTTP level)
