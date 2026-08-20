#!/usr/bin/env python3
"""
s04: Hooks — move extension logic out of the loop, onto hooks.

  User types query
       │
       ▼
  ┌──────────────────┐
  │ UserPromptSubmit │ ── trigger_hooks() before LLM
  └────────┬─────────┘
           ▼
  ┌────────────┐     ┌─────────────────────────────┐
  │  messages  │────▶│  LLM (stop_reason=tool_use?)│
  └────────────┘     │   No ──▶ Stop hooks ──▶ exit │
                     │   Yes ──▶ tool_use block ──┐ │
                     └────────────────────────────┘ │
                                                    ▼
                                          ┌──────────────────┐
                                          │ trigger_hooks()   │
                                          │  PreToolUse:      │
                                          │   permission_hook │
                                          │   log_hook        │
                                          └───────┬──────────┘
                                                  │ (not blocked)
                                          ┌───────▼──────────┐
                                          │ TOOL_HANDLERS[x]  │
                                          └───────┬──────────┘
                                                  │
                                          ┌───────▼──────────┐
                                          │ trigger_hooks()   │
                                          │  PostToolUse:     │
                                          │   large_output    │
                                          └───────┬──────────┘
                                                  │
                                          results ──▶ back to messages

Changes from s03:
  + HOOKS registry (event -> list of callbacks)
  + register_hook() / trigger_hooks()
  + context_inject_hook (UserPromptSubmit)
  + permission_hook, log_hook (PreToolUse)
  + large_output_hook (PostToolUse)
  + summary_hook (Stop)
  - check_permission() removed from loop body
    (logic moved into permission_hook, triggered via PreToolUse)

Run: python s04_hooks/code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import os, subprocess
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


# ═══════════════════════════════════════════════════════════
#  FROM s02-s03 : Tool Implementations
# ═══════════════════════════════════════════════════════════

def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        file_path = (WORKDIR / path).resolve()
        lines = file_path.read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = (WORKDIR / path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = (WORKDIR / path).resolve()
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s04: Hook System (s03 permission logic now via hooks)
# ═══════════════════════════════════════════════════════════

# Hook 注册表：键是生命周期事件名，值是按注册顺序排列的回调函数列表。
# 同一个事件允许挂多个 hook，例如 PreToolUse 先执行 permission_hook，再执行 log_hook。
# 四个事件对应不同阶段：用户提交问题、工具执行前、工具执行后，以及 Agent 即将停止。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

# 把一个回调函数挂到指定事件上；这是 hook 系统对外提供的“注册”入口。
def register_hook(event: str, callback):
    # `HOOKS[event]` 取出该事件对应的列表，`append` 把 callback 放到列表末尾。
    # 因此回调的执行顺序由注册顺序决定；如果 event 不存在，这里会抛出 KeyError。
    HOOKS[event].append(callback)

# 触发某个事件，并把事件相关数据依次传给该事件的所有回调。
def trigger_hooks(event: str, *args):
    # `*args` 接收可变数量的位置参数：UserPromptSubmit 传 query，PreToolUse 传 block，
    # PostToolUse 可能传 block 和 output，Stop 传 messages，所以不同事件可以共享同一个触发器。
    for callback in HOOKS[event]:
        # `callback(*args)` 再次使用 `*` 解包，把同一组参数传给当前回调函数。
        result = callback(*args)
        # 约定：返回 None 表示“本 hook 不阻止流程”；返回任意非 None 值表示要拦截。
        # 一旦某个 hook 拦截，就立即返回结果，不再执行后面的 hook，形成短路控制流。
        if result is not None:  # teaching shortcut: block this tool call
            return result
    # 所有 hook 都返回 None，说明事件顺利通过，没有阻止或修改流程。
    return None


# s03 的权限检查逻辑现在被封装成 PreToolUse hook：工具真正执行前先经过这里。
# DENY_LIST 命中时直接拒绝，不给用户确认机会；DESTRUCTIVE 命中时则暂停并询问用户。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

def permission_hook(block):
    """PreToolUse: s03 check_permission() logic moved here."""
    # block 是 Anthropic 返回的 tool_use block；block.name 标识工具，block.input 保存工具参数。
    # 返回字符串会被 trigger_hooks() 视为拦截原因，返回 None 则表示允许继续。
    if block.name == "bash":
        # 第一层：硬拒绝规则。这里逐个检查命令是否包含绝对禁止的危险片段。
        for pattern in DENY_LIST:
            # `get("command", "")` 在参数缺失时提供空字符串，避免权限检查阶段 KeyError。
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                # 返回原因字符串；trigger_hooks() 收到非 None 后会停止后续 hook，并阻止工具执行。
                return "Permission denied by deny list"
        # 第二层：可疑但不一定绝对禁止的命令，需要交给用户做最终决定。
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                # input() 会暂停当前程序，等待用户在终端输入 y/yes 或其他内容并回车。
                choice = input("   Allow? [y/N] ").strip().lower()
                # 只有明确输入 y 或 yes 才放行；默认分支是拒绝，属于“默认安全”。
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    # 文件工具共享路径安全检查；元组用于一次性判断多个工具名。
    if block.name in ("read_file", "write_file", "edit_file"):
        # 取出模型提供的 path；缺少 path 时使用空字符串，后面会按工作区路径继续检查。
        path = block.input.get("path", "")
        # resolve() 规范化 `..` 等路径片段；is_relative_to() 判断规范化后的路径是否仍在 WORKDIR 下。
        # 取反后，只有越出工作区时才进入需要用户确认的分支。
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Access outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    # 没有命中拒绝规则，也没有被用户拒绝：返回 None，让下一个 hook 或工具执行阶段继续。
    return None

def log_hook(block):
    """PreToolUse: log every tool call."""
    # 这是一个只观察、不拦截的 PreToolUse hook：记录工具名和少量参数，方便调试 Agent 行为。
    # `block.input.values()` 取得所有参数值，`list(... )[:2]` 只保留前两个，避免日志过长。
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    # 返回 None，明确告诉 trigger_hooks()：日志记录完成，但不阻止工具执行。
    return None

def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    # 这是工具已经执行完成后的 PostToolUse hook；此时 output 是工具刚刚产生的结果。
    # 先转成字符串再计算长度，兼容工具返回非字符串对象的情况；这里只提醒，不截断或修改 output。
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    # 无论是否打印提醒，都返回 None，让后续流程继续把 output 放回消息历史。
    return None

# UserPromptSubmit hook: log user input before it reaches the LLM
def context_inject_hook(query: str):
    # 这个 hook 在用户问题发送给模型之前触发；当前实现只打印工作目录，并没有真正修改 query。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    # 返回 None 表示不阻止用户问题继续进入 messages 和 LLM 调用。
    return None

# Stop hook: print summary when loop is about to exit
def summary_hook(messages: list):
    # Stop 阶段接收完整 messages；下面的生成器表达式统计历史中 tool_result 字典的数量。
    # `m.get("content")` 先取消息内容；只有 content 是 list 时才遍历，避免对普通字符串逐字符统计。
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    # 这里只输出统计信息，不改变 messages，也不阻止 Agent 结束。
    return None

# 模块加载时完成 hook 注册；之后 agent_loop() 只需 trigger_hooks(event, ...) 即可触发它们。
# 同一事件的注册顺序很重要：permission_hook 先判断是否拦截，只有放行后 log_hook 才会执行。
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — same structure as s03, but no hard-coded check
#  s03: if not check_permission(block): ...
#  s04: if trigger_hooks("PreToolUse", block): ...
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # s04 change: hook replaces hard-coded check_permission()
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            trigger_hooks("PostToolUse", block, output)  # s04: post hook

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s04: Hooks — extension logic on hooks, loop stays clean")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            default_query = "Create a file called test.txt"
            query = input(f"\033[36ms04 >> {default_query} \033[0m") or default_query
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
