#!/usr/bin/env python3
"""
s05: TodoWrite — add a planning tool on top of s04 hooks.

  +---------+      +-------+      +------------------+
  |  User   | ---> |  LLM  | ---> | TOOL_HANDLERS    |
  | prompt  |      |       |      |  bash            |
  +---------+      +---+---+      |  read_file       |
                        ^         |  write_file      |
                        | result  |  edit_file       |
                        +---------+  glob            |
                                      todo_write ← NEW
                                   +------------------+
                                        |
                         in-memory current_todos
                                        |
                        if rounds_since_todo >= 3:
                          inject <reminder>

Changes from s04:
  + todo_write tool + run_todo_write() implementation
  + Nag reminder (inject reminder after 3 rounds without todo update)
  + SYSTEM prompt includes "plan before execute" guidance
  + rounds_since_todo counter in agent_loop
  Loop unchanged: new tool auto-dispatches via TOOL_HANDLERS.

Run: python s05_todo_write/code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import ast, json, os, subprocess
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
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
CURRENT_TODOS: list[dict] = []

# s05 change: SYSTEM prompt adds planning guidance
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s04 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

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
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
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


# ═══════════════════════════════════════════════════════════
#  NEW in s05: todo_write tool — plan only, no execution
# ═══════════════════════════════════════════════════════════

# 这个辅助函数负责把模型传来的各种形式统一成“任务字典列表”，并提前检查数据是否合法。
# 成功时返回 `(任务列表, None)`；失败时返回 `(None, 错误信息)`，调用者可以统一处理结果。
def _normalize_todos(todos):
    # 模型通常会传入 Python list，但某些兼容层可能把数组编码成字符串，因此这里兼容两种入口。
    if isinstance(todos, str):
        try:
            # `json.loads` 把 JSON 字符串解析成 Python 对象；例如 `'[{"content": "..."}]'` 会变成 list。
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                # 兼容 Python 字面量形式的字符串，例如 `"[{'content': '...', 'status': 'pending'}]"`。
                # `literal_eval` 只解析字符串、列表、字典等字面量，比直接使用 eval 更安全。
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                # 两种解析方式都失败，返回错误元组；这里不抛异常，避免工具调用直接中断 Agent 循环。
                return None, "Error: todos must be a list or JSON array string"
    # 无论输入原本是什么类型，规范化后都必须是 list，后面才能逐项检查任务。
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    # `enumerate` 同时提供任务下标 i 和任务对象 t；下标会被放进错误信息，方便定位哪一项有问题。
    for i, t in enumerate(todos):
        # 每个任务必须是 dict，因为后面要通过 t["content"] 和 t["status"] 读取字段。
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        # `content` 是任务文字，`status` 是任务状态；两者缺一不可。
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        # 只接受这三个状态，保证后面的图标字典一定能找到对应的显示符号。
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    # 返回原任务列表和 None；None 在这里表示“没有错误”，与上面的错误字符串形成统一返回格式。
    return todos, None

def run_todo_write(todos: list) -> str:
    # `CURRENT_TODOS` 是本次会话的内存状态；函数内部要重新赋值它，所以必须声明为 global。
    global CURRENT_TODOS
    # 这里是元组解包：把规范化函数返回的 `(任务列表, 错误信息)` 分别放进两个变量。
    todos, error = _normalize_todos(todos)
    # 空字符串、None 等值会被视为“没有错误”；真正的错误信息是非空字符串，因此可以直接判断。
    if error:
        return error
    # 校验通过后更新全局任务列表，后续 Agent 轮次或 Stop hook 都可以读取最新状态。
    CURRENT_TODOS = todos
    # 先创建输出行列表；开头的换行和 ANSI 转义序列只负责终端显示效果，不影响任务数据本身。
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    # 按任务原始顺序生成一行一项的终端展示内容。
    for t in CURRENT_TODOS:
        # 根据状态选择图标和颜色：pending 用空白，进行中用蓝色箭头，完成用绿色对勾。
        # 这里的 status 已在 _normalize_todos 中校验过，所以字典查找不会遇到未知键。
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        # f-string 把任务状态对应的 icon 和任务文字 content 拼成一行可读的清单。
        lines.append(f"  [{icon}] {t['content']}")
    # `join` 用换行符把多行列表合成一个字符串，print 一次性显示完整任务面板。
    print("\n".join(lines))
    # 返回给工具调度器的简短结果；它会作为 tool_result 反馈给模型，而不是替代上面的终端展示。
    return f"Updated {len(CURRENT_TODOS)} tasks"

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
    # s05: new tool
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): Hook System
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

# s04 hooks preserved
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

def permission_hook(block):
    """PreToolUse: deny list check."""
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None

def log_hook(block):
    """PreToolUse: log tool calls."""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

def context_inject_hook(query: str):
    """UserPromptSubmit: log working directory."""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    """Stop: print tool call count."""
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — same as s04 + nag reminder counter
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    # 这个计数器只属于当前一次用户请求：它记录连续多少轮模型调用没有更新 todo 列表。
    # 每次进入 agent_loop() 都从 0 开始，while 循环内部会持续维护它的状态。
    rounds_since_todo = 0
    while True:
        # s05: nag reminder — inject if model hasn't updated todos for 3 rounds
        # 在发起下一次模型请求前检查是否连续 3 轮没有 todo 更新。
        # `messages` 非空是为了确保提醒有上下文可附加；提醒本身使用 user 消息身份送回模型。
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            # 提醒已经注入，本轮重新计数；如果之后仍不更新，过 3 轮还会再次提醒。
            rounds_since_todo = 0

        # 把当前完整消息历史、系统提示和工具定义发送给模型，等待它决定直接回答还是调用工具。
        # 这是同步调用：程序会等待 API 返回，拿到 response 后才继续执行下一行。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 先保存 assistant 的完整响应，尤其是其中的 text/tool_use block，
        # 这样下一次 API 请求才能看到模型刚才做出的决定。
        messages.append({"role": "assistant", "content": response.content})

        # 没有 tool_use 时，模型认为当前任务可以结束；但 Stop hook 仍有机会要求继续。
        if response.stop_reason != "tool_use":
            # Stop hook 接收完整消息历史，可以打印总结，也可以返回一段“继续执行”的内容。
            force = trigger_hooks("Stop", messages)
            if force:
                # `force` 非空表示 hook 强制继续：把它作为新的 user 消息加入上下文，回到 while 开头。
                messages.append({"role": "user", "content": force})
                continue
            # Stop hook 没有要求继续，当前 agent_loop 正常结束。
            return

        # 本轮至少发生了一次工具调用，因此算作一轮“模型采取行动”的迭代。
        # 注意：这里每次 response 只加 1，即使 response.content 中包含多个 tool_use block。
        rounds_since_todo += 1
        # 保存本轮所有工具调用的结果；循环结束后会一次性作为 user 消息回传给模型。
        results = []
        for block in response.content:
            # response.content 还可能包含 text/thinking 等 block；只有 tool_use 才需要本地执行。
            if block.type != "tool_use":
                continue

            # PreToolUse hook 在真正调用 Python handler 前运行，可检查权限并返回拦截原因。
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                # 即使工具被拦截，也要为这个 tool_use_id 构造 tool_result，
                # 让模型知道这次工具调用的结果是“被拒绝”，而不是让消息协议断裂。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                # 跳过当前工具，继续处理同一个 response 中可能存在的其他 tool_use block。
                continue

            # 根据模型返回的工具名找到本地处理函数，再把 input 字典解包成关键字参数执行。
            # 例如 name=todo_write、input={"todos": [...]} 会调用 run_todo_write(todos=[...])。
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            # 工具执行完成后触发 PostToolUse hook；它可以观察 output，例如检查输出是否过大。
            trigger_hooks("PostToolUse", block, output)

            # s05: reset nag counter when todo_write is called
            # 只要模型成功调用 todo_write，就说明它重新提交了计划，连续未更新轮数归零。
            if block.name == "todo_write":
                rounds_since_todo = 0

            # 保存工具结果，并用同一个 tool_use_id 与 assistant 请求一一对应。
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        # 将本轮所有 tool_result 作为 user 消息追加到历史，下一次 while 循环会把它们发回模型。
        # 这样模型才能看到工具执行结果，并决定下一步继续调用工具还是给出最终回答。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s05: TodoWrite — plan before execute, nag if you forget")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            default_query = "Refactor s05_todo_write/example/hello.py: add type hints, docstrings, and a main guard"
            query = input(f"\033[36ms05 >> {default_query} \033[0m") or default_query
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
