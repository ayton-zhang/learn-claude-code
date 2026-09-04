#!/usr/bin/env python3
"""
s06: Subagent — spawn sub-agents with fresh messages[] for context isolation.

  Parent Agent                           Subagent
  +------------------+                  +------------------+
  | messages=[...]   |                  | messages=[task]  | <-- fresh
  |                  |   dispatch       |                  |
  | tool: task       | ---------------> | own while loop   |
  |   prompt="..."   |                  |   bash/read/...  |
  |                  |   summary only   |   (max 30 turns) |
  | result = "..."   | <--------------- | return last text |
  +------------------+                  +------------------+
        ^                                      |
        |       intermediate results DISCARDED  |
        +--------------------------------------+

  Subagent tools: bash, read, write, edit, glob (NO task — no recursion)

Changes from s05:
  + task tool + spawn_subagent() with fresh messages[]
  + Safety limit: max 30 turns per subagent
  + extract_text() helper
  Subagent cannot spawn sub-subagents (no task tool in sub_tools).
  Main loop unchanged: task auto-dispatches via TOOL_HANDLERS.

Run: python s06_subagent/code.py
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

SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "For complex sub-problems, use the task tool to spawn a subagent."
)

# s06: subagent gets its own system prompt — no task, no recursion
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s05 (unchanged): Tool Implementations
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

def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
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
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s06: Subagent — fresh messages[], summary only
# ═══════════════════════════════════════════════════════════

# ==========================================
# 组件：子代理工具定义与执行路由
# 设计决策：工具白名单隔离，防止子代理递归派生
# ==========================================

# 子代理可用工具列表（SUB_TOOLS）：
# 与主代理（Parent Agent）相比，这里做了一处至关重要的设计剪裁：
# 1. 保留：bash、read_file、write_file、edit_file、glob（赋予充分的代码读写与排查能力）
# 2. 剔除：task（严禁子代理派生"孙代理"，避免递归爆炸与死循环消耗无限 token）
# 3. 剔除：todo_write（任务状态由父代理全局把控，子代理只专注完成当前特定子目标）
SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]
# NO "task" tool — prevent recursive spawning

# 子代理工具执行函数的分发字典（Tool Dispatch Map）：
# 当模型返回工具调用请求时，通过 block.name 快速索引到对应的本地 Python 实现函数
SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}

# ==========================================
# 辅助函数：文本块提取
# 功能：从模型多模态/复杂的消息内容块（Content Blocks）中过滤并拼接纯文本
# ==========================================
def extract_text(content) -> str:
    """Extract text from message content blocks."""
    # 防御性判断：若 content 不是列表（例如直接是普通字符串），则统一强转为 str 返回
    if not isinstance(content, list):
        return str(content)
    # 语法注释：生成器推导式 + getattr 反射安全获取
    # - getattr(b, "type", None) == "text"：过滤出类型为文本的块，忽略 tool_use 等非文本块
    # - getattr(b, "text", "")：安全读取 text 属性，如果不存在则退化为 ""，避免抛出 AttributeError
    # - "\n".join(...)：将多个文本段落按换行符连接成一个完整的回答字符串
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")

# ==========================================
# 核心组件：子代理派生与执行器（spawn_subagent）
# 核心思想：上下文隔离（Context Isolation）+ 过程丢弃（Summary Only）
# ==========================================
def spawn_subagent(description: str) -> str:
    """Spawn a subagent with fresh messages[], return summary only."""
    # 参数说明：
    #   description: 父代理下发给子代理的具体任务描述字符串（Prompt）
    # 返回值：
    #   str: 子代理在独立环境中完成任务后返回的最终文字总结（中间过程全部丢弃）
    print(f"\n\033[35m[Subagent spawned]\033[0m")

    # ─── 关键机制 1：空白独立上下文（Fresh Context）───
    # 比喻：主代理就像项目经理，子代理像派去攻坚的外包工程师。
    # 子代理开启全新的聊天群（messages 只有一条初始任务），完全不继承主代理冗长的历史对话。
    # 优势：防止长对话导致的注意力稀释（Lost in the Middle），且大幅降低 API 费用。
    messages = [{"role": "user", "content": description}]  # fresh context

    # ─── 关键机制 2：安全步数上限（Safety Limit）───
    # 限制子代理单次最多迭代 30 轮，防止工具调用产生死循环或无休止报错重试
    for _ in range(30):  # safety limit
        # 调用 Claude API：传入专用的系统提示词 SUB_SYSTEM（要求只给总结）和受限工具集 SUB_TOOLS
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000,
        )
        # 将模型本轮生成的助理消息（可能包含文本或工具调用指令）追加到上下文中
        messages.append({"role": "assistant", "content": response.content})

        # 终止条件：若模型的 stop_reason 不是 "tool_use"（如给出 end_turn 总结回答），说明任务已收敛，跳出循环
        if response.stop_reason != "tool_use":
            break

        # ─── 关键机制 3：子代理的工具调用与钩子拦截 ───
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # Issue 1: subagent also runs hooks (permissions apply)
                # 前置钩子校验：子代理同样受系统全局权限控制（例如文件写保护、危险命令拦截）
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    # 如果操作被 Hook 拒绝，将拒绝原因作为工具结果伪装返回给模型，让其知晓并调整策略
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue

                # 查找对应的本地工具执行函数；若未在 SUB_HANDLERS 中注册则返回未知错误
                handler = SUB_HANDLERS.get(block.name)
                # 语法注释：**block.input 是字典解包，将 API 传过来的参数作为关键字实参直接传给处理函数
                output = handler(**block.input) if handler else f"Unknown: {block.name}"

                # 后置钩子：执行后触发监听（如日志审计、统计）
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")

                # 打包执行结果：必须携带 tool_use_id，以便 API 准确对应指令与执行回包
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})

        # 将所有的工具执行回执整体作为一条 user 角色的消息追加到子代理上下文中，供下一轮继续推理
        messages.append({"role": "user", "content": results})

    # ─── 关键机制 4：兜底提取结论（Issue 5 fallback）───
    # 正常情况下，退出循环是因为模型输出了最终文本，此时 messages[-1] 就是包含最终答案的 assistant 消息
    result = extract_text(messages[-1]["content"])
    if not result:
        # 特殊异常兜底：若在调用工具后刚好耗尽 30 步上限强制跳出，最后一条消息是 user 发送的 tool_result。
        # 此时逆向遍历历史消息（reversed(messages)），寻找离当前最近的 assistant 文本作为降级结论
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        # 若整场对话中助理从未产生任何文本内容，返回明确的超时失败提示
        if not result:
            result = "Subagent stopped after 30 turns without final answer."

    print(f"\033[35m[Subagent done]\033[0m")
    # ─── 关键机制 5：信息提炼与过程丢弃（Summary Only）───
    # 仅将纯文本摘要字符串返回给父代理；中间这 30 轮繁杂的执行步骤与报错细节在函数结束后被垃圾回收，
    # 彻底保护了父代理的上下文整洁度，避免了上下文污染
    return result  # only summary, entire message history discarded

# ==========================================
# 挂载：将 task 工具动态注册到父代理（Parent Agent）
# ==========================================
# 向父代理的工具声明清单（TOOLS）追加 task 规范，让父模型知道何时以及如何调用子代理
# Add task tool to parent's tools
TOOLS.append({
    "name": "task",
    "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
    "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
})
# 绑定执行分发：当父模型发出调用 "task" 的意图时，自动路由执行 spawn_subagent 函数
TOOL_HANDLERS["task"] = spawn_subagent


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
#  agent_loop — same as s05 + nag reminder, task auto-dispatches
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    rounds_since_todo = 0
    while True:
        # s05: nag reminder
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

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

        rounds_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            trigger_hooks("PostToolUse", block, output)

            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s06: Subagent — spawn sub-agents with fresh context, summary only")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            default_query = "Use a subtask to find what testing framework this project uses"
            query = input(f"\033[36ms06 >> {default_query} \033[0m") or default_query
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
