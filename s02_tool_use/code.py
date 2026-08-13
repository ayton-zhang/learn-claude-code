#!/usr/bin/env python3
"""
s02: Tool Use — 在 s01 基础上新增 4 个工具 + 分发映射。

运行: python s02_tool_use/code.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY

本文件 = s01 的全部代码 + 以下新增:
  + run_read / run_write / run_edit / run_glob 四个工具实现
  + TOOL_HANDLERS 分发映射（替代 s01 中硬编码的 run_bash 调用）
  + safe_path 路径安全校验

循环本身（agent_loop）与 s01 完全一致。
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
#  FROM s01 (unchanged)
# ═══════════════════════════════════════════════════════════

# 工具职责：执行模型传入的 shell 命令，并把命令输出转换成字符串交还给模型。
# 这是 s01 保留下来的工具；与下面的专用文件工具相比，它的能力更宽，但副作用风险也更高。
def run_bash(command: str) -> str:
    # `command: str` 表示调用者应传入字符串命令；`-> str` 表示本函数始终把结果包装成字符串。
    # 教学版先用简单的黑名单拦截明显危险操作；它不是完整的 Shell 安全策略，不能替代真正的权限系统。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    # `any(...)` 会逐个检查黑名单片段是否出现在命令中，只要有一个命中就返回 True。
    # 这里使用字符串包含判断，因此例如命令中出现 `sudo` 子串也会被拦截。
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # `subprocess.run` 把 Python 控制权交给操作系统去执行命令，并等待命令结束后再继续。
        # `shell=True` 允许按照当前 Shell 的语法解析管道、重定向等写法；同时也意味着 command 必须被谨慎控制。
        # `cwd=WORKDIR` 让命令始终在 Agent 的工作区执行，而不是随着启动位置意外改变目录。
        # `capture_output=True` 把 stdout/stderr 收回 Python；`text=True` 让它们直接以字符串返回。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        # 把标准输出和错误输出合并，便于模型一次性看到命令的完整反馈。
        # `.strip()` 去掉首尾空白；最多保留 50000 个字符，避免超大的命令结果挤占上下文。
        out = (r.stdout + r.stderr).strip()
        # 三元表达式：有输出就返回截断后的内容；没有输出时返回明确占位文本，
        # 让模型知道命令执行过了，而不是误以为工具没有响应。
        return out[:50000] if out else "(no output)"
    # 命令超过 120 秒时，主动终止等待并把超时作为普通工具结果反馈给模型。
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    # 捕获进程启动失败等操作系统层面的异常，转成字符串后继续 Agent 循环。
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 4 个新工具
# ═══════════════════════════════════════════════════════════

# 工具职责：把模型给出的相对路径解析成工作区内的绝对路径，并阻止路径逃出工作区。
def safe_path(p: str) -> Path:
    # `(WORKDIR / p)` 是 Path 的路径拼接；`.resolve()` 会消除 `.`、`..` 和符号链接，得到规范路径。
    # 例如 WORKDIR 是 `/project`、p 是 `docs/../README.md` 时，结果会规范化为 `/project/README.md`。
    path = (WORKDIR / p).resolve()
    # `is_relative_to(WORKDIR)` 检查 path 是否仍位于工作区目录树中。
    # 如果模型传入 `../secret.txt` 等路径，检查失败就抛出 ValueError，后续文件工具会把它转成错误文本。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# 工具职责：读取工作区内文件，把文件文本作为 tool_result 返回给模型。
def run_read(path: str, limit: int | None = None) -> str:
    try:
        # 先经过 safe_path，再读取文件；这样文件读取和路径安全检查始终绑定在一起。
        # `.splitlines()` 把完整文本拆成字符串列表，并去掉每行末尾的换行符，便于按行截断。
        lines = safe_path(path).read_text().splitlines()
        # `limit` 为 None 或 0 时不截断；只有 limit 是真值且小于总行数时才追加省略提示。
        # `lines[:limit]` 保留前 limit 行，后面的列表元素用一条说明替代，控制返回给模型的上下文大小。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        # `"\n".join(...)` 把行列表重新拼回一个文本字符串，作为工具的输出。
        return "\n".join(lines)
    # 路径越界、文件不存在、权限不足或解码失败都会在这里统一变成可读的错误结果。
    except Exception as e:
        return f"Error: {e}"


# 工具职责：在工作区内创建或覆盖一个文件，并把写入结果报告给模型。
def run_write(path: str, content: str) -> str:
    try:
        # `safe_path` 先限制目标位置；`.parent` 取得父目录，`mkdir(..., parents=True)` 会递归创建缺失目录。
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # `write_text` 会用字符串 content 覆盖写入文件；如果文件已存在，原内容会被替换。
        file_path.write_text(content)
        # `len(content)` 计算的是 Python 字符串的字符数；虽然提示文字写作 bytes，非 ASCII 文本不等于 UTF-8 字节数。
        return f"Wrote {len(content)} bytes to {path}"
    # 把路径和文件系统错误转成工具结果，让模型可以根据错误继续推理或修正路径。
    except Exception as e:
        return f"Error: {e}"


# 工具职责：在工作区内把文件中的一段精确文本替换一次。
def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        # 读取前同样经过 safe_path，保证编辑操作不能访问工作区之外的文件。
        file_path = safe_path(path)
        text = file_path.read_text()
        # 先检查 old_text 是否存在，避免直接 replace 后误报“编辑成功”。
        if old_text not in text:
            return f"Error: text not found in {path}"
        # `replace(old_text, new_text, 1)` 的第三个参数 1 表示最多替换第一次出现的位置，
        # 这样不会把文件中其他相同片段全部一起改掉。
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    # 读取、替换或写回过程中的异常都被转成普通工具错误。
    except Exception as e:
        return f"Error: {e}"


# 工具职责：在工作区内按 glob 通配模式查找文件，并返回相对路径列表。
def run_glob(pattern: str) -> str:
    # 局部导入只在调用 glob 工具时加载标准库模块；`g` 是为了让后面的调用更简短。
    import glob as g
    try:
        results = []
        # `root_dir=WORKDIR` 让 glob 从工作区开始查找，并返回相对于 WORKDIR 的匹配路径。
        for match in g.glob(pattern, root_dir=WORKDIR):
            # 即使 glob 返回了匹配项，也再次规范化并检查路径边界，避免结果指向工作区之外。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        # 有匹配就用换行拼成模型易读的列表；没有匹配则返回明确提示，而不是空字符串。
        return "\n".join(results) if results else "(no matches)"
    # 通配模式非法或文件系统访问失败时，统一返回错误文本。
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具定义（s01 只有一个 bash，现在扩展到 5 个）
# ═══════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具分发映射（s01 是硬编码 run_bash，现在改为查表）
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  agent_loop — 与 s01 结构完全一致，只改了工具执行那部分
#  s01: output = run_bash(block.input["command"])
#  s02: output = TOOL_HANDLERS[block.name](**block.input)
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m> {block.name}\033[0m")
                handler = TOOL_HANDLERS.get(block.name)
                # `handler` 是根据模型返回的 `block.name` 找到的本地 Python 函数；
                # `block.input` 是模型按照该工具的 `input_schema` 生成的参数字典，
                # `**` 会把字典解包成关键字参数，例如：`{"path": "README.md"}`
                # 会变成 `run_read(path="README.md")`，从而真正执行对应工具。
                # 这里的三元表达式还兼顾未知工具：找到处理函数就执行，否则返回错误文本，
                # 避免因为模型返回了未注册的工具名而让整个 Agent 循环直接崩溃。
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s02: Tool Use — 在 s01 基础上加了 4 个工具")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            default_query = "Read both README.md and requirements.txt, then create a summary file"
            query = input(f"\033[36ms02 >> {default_query} \033[0m") or default_query
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
