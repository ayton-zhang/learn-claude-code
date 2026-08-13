# Summary

## Project Overview

**learn-claude-code** is a 0-to-1 harness engineering learning repository. Its core thesis:

> **Agency comes from the model (training), not from code. An agent product = Model + Harness.**
> The model is the driver; the harness is the vehicle.

### Key Concepts

- **Agency** (perceive, reason, act) comes from model training — proven by DeepMind DQN (2013), OpenAI Five (2019), AlphaStar (2019), Tencent Jueyu (2019), and LLM coding agents (2024-2025).
- **An "agent" is NOT** drag-and-drop workflow builders, no-code platforms, or prompt-chain orchestration with if-else branches — those are "Rube Goldberg machines," not agents.
- **Building an agent means one of two things:**
  1. **Training a model** (adjusting weights via RL, fine-tuning, RLHF).
  2. **Building a harness** (the operational environment around a model).

### The Harness Formula

```
Harness = Tools + Knowledge + Observation + Action Interfaces + Permissions
```

- **Tools:** file I/O, shell, network, database, browser
- **Knowledge:** docs, domain references, API specs, style guides
- **Observation:** git diff, error logs, browser state, sensor data
- **Action:** CLI commands, API calls, UI interactions
- **Permissions:** sandbox isolation, approval workflows, trust boundaries

### Core Agent Loop Pattern

The single constant pattern — every lesson layers one mechanism on top of this loop, and the loop itself never changes:

```python
def agent_loop(messages):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM,
            messages=messages, tools=TOOLS,
        )
        messages.append({"role": "assistant",
                         "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = TOOL_HANDLERS[block.name](**block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
        messages.append({"role": "user", "content": results})
```

## Course Structure: 20 Progressive Lessons

| # | Topic | Key Concepts |
|---|---|---|
| s01 | Agent Loop | `messages` / `while True` / `stop_reason` |
| s02 | Tool Use | `TOOL_HANDLERS` / dispatch map / concurrency |
| s03 | Permission System | `PermissionRule` / approval pipeline |
| s04 | Hook System | `PreToolUse` / `PostToolUse` / extension points |
| s05 | TodoWrite | `TodoItem` / plan-then-execute |
| s06 | Subagent | `fresh messages[]` / context isolation |
| s07 | Skill Loading | `SkillManifest` / on-demand injection |
| s08 | Context Compact | snipCompact / microCompact / toolResultBudget / autoCompact |
| s09 | Memory System | selection / extraction / consolidation |
| s10 | System Prompt | runtime assembly / section concatenation |
| s11 | Error Recovery | token escalation / fallback model / retry strategies |
| s12 | Task System | `TaskRecord` / `blockedBy` / disk persistence |
| s13 | Background Tasks | threaded execution / notification queue |
| s14 | Cron Scheduler | durable scheduling / session-scoped triggers |
| s15 | Agent Teams | `MessageBus` / inbox / permission bubbling |
| s16 | Team Protocols | shutdown handshake / plan approval |
| s17 | Autonomous Agents | idle cycle / auto-claim / self-organization |
| s18 | Worktree Isolation | `WorktreeRecord` / task-directory binding |
| s19 | MCP Plugin | multi-transport / channel routing / tool pool assembly |
| s20 | Comprehensive Agent | all mechanisms around one loop |

**Learning path:** act → handle complex work → remember and recover → run long tasks → collaborate → extend and assemble.

### Track Status
- **Current track:** root-level `s01`–`s20` folders (each has README + translations + `code.py`).
- **Legacy transition track:** `docs/`, `agents/`, `web/` app (older 12-lesson version, temporarily kept).

## Dependencies (requirements.txt)

| Package | Constraint |
|---|---|
| `anthropic` | >=0.25.0 |
| `httpx[socks]` | >=0.27.0 |
| `python-dotenv` | >=1.0.0 |
| `pyyaml` | >=6.0 |

## Quick Start

```sh
git clone https://github.com/shareAI-lab/learn-claude-code
cd learn-claude-code
pip install -r requirements.txt
cp .env.example .env   # configure ANTHROPIC_API_KEY

python s01_agent_loop/code.py        # Start here -- one loop + bash
python s08_context_compact/code.py   # Context compaction (complex)
python s20_comprehensive/code.py     # Endpoint: all mechanisms in one loop
```

## Downstream Projects

- **Kode Agent CLI** (`@shareai-lab/kode`) — open-source coding agent CLI with skill/LSP support, Windows compatible, works with open models.
- **Kode Agent SDK** (`shareAI-lab/kode-agent-sdk`) — embed agent capabilities in applications.
- **claw0** — sister teaching repo for always-on assistants (heartbeat + cron + IM + memory + soul).

## Scope Notes

Some production mechanisms are intentionally simplified/omitted for teaching: full hook bus events, rule-based permission governance, session resume/fork, full MCP runtime details. The JSONL mailbox protocol is a teaching implementation.

## License

MIT

---

*Generated summary of README.md and requirements.txt.*
