#!/usr/bin/env python3
"""
s08_context_compact.py - Context Compact

Four-layer compaction pipeline inserted before LLM calls:

    L1: snip_compact      — trim middle messages when count > 50
    L2: micro_compact     — replace old tool_results with placeholders
    L3: tool_result_budget — persist large results to disk
    L4: compact_history   — LLM full summary (1 API call)

    Emergency: reactive_compact — when API still returns prompt_too_long

    ┌─────────────────────────────────────────────────────────────┐
    │  messages[]                                                 │
    │    ↓                                                        │
    │  L3 budget ─→ L1 snip ─→ L2 micro ─→ [token > threshold?]  │
    │                                      ├─ No  → LLM          │
    │                                      └─ Yes → L4 summary   │
    │                                              ↓              │
    │                                          LLM call           │
    │                                    [prompt_too_long?]        │
    │                                      └─ Yes → reactive      │
    └─────────────────────────────────────────────────────────────┘

Core principle: cheap first, expensive last.
Execution order matches CC source: budget → snip → micro → auto.

Builds on s07 (skill loading). Usage:

    python s08_context_compact/code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import ast, json, os, subprocess, time
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"): os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
CURRENT_TODOS: list[dict] = []

# s07: Skill catalog scan (inherited from s07)
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()

SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

_scan_skills()

def list_skills() -> str:
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

def load_skill(name: str) -> str:
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]

# s08: SYSTEM includes skill catalog (inherited from s07 build_system)
def build_system() -> str:
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )

SYSTEM = build_system()

# s08: subagent gets its own system prompt — no compact, no skill loading
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s07 (unchanged): Basic Tools
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired: return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines): lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path); file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content); return f"Wrote {len(content)} bytes to {path}"
    except Exception as e: return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text: return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e: return f"Error: {e}"

def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e: return f"Error: {e}"

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

def extract_text(content) -> str:
    if not isinstance(content, list): return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


# ═══════════════════════════════════════════════════════════
#  FROM s06-s07 (unchanged): Subagent
# ═══════════════════════════════════════════════════════════

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
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write,
                "edit_file": run_edit, "glob": run_glob}

def spawn_subagent(description: str) -> str:
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  NEW in s08: Four-Layer Compaction Pipeline
# ═══════════════════════════════════════════════════════════

# ==========================================
# 机制总览：四层梯度上下文压缩流水线（Four-Layer Compaction Pipeline）
# ==========================================
# 痛点与设计决策：
#   AI Coding Agent 在长程交互中会执行几十上百次工具调用，带来巨大的上下文膨胀。
#   如果超出模型上下文限制，会直接导致 API 报错中断；即使未超限，过长的上下文也会让推理速度变慢、费用飙升、甚至"注意力涣散"。
# 解决方案：
#   设计梯度防御机制，按处理代价从低到高递进：
#     - L1（snip_compact，剪切）：零成本规则裁剪，丢弃中间过期对话，保留头部原始意图和尾部最新状态。
#     - L2（micro_compact，微缩）：零成本内容精简，将早期工具的长返回值原地缩减为占位符。
#     - L3（tool_result_budget，预算）：零 LLM 成本，单次巨幅输出（如超长日志）自动持久化到磁盘，Prompt 中仅留摘要。
#     - L4（auto_compact，提炼）：触发阈值时调用一次 LLM，将长历史高度提炼为结构化状态卡片，彻底重置上下文。
#     - Emergency（reactive_compact，熔断自救）：当 API 真正爆出超长错误时的应急修复。

# --- 阈值与超参数设定 ---
# CONTEXT_LIMIT: 上下文字符数上限阈值，超过该阈值时自动触发 L4 的 LLM 总结压缩
CONTEXT_LIMIT = 50000
# KEEP_RECENT: L2 微缩压缩时保护的最近工具调用数量，防止模型刚刚拿到的结果被过早擦除
KEEP_RECENT = 3
# PERSIST_THRESHOLD: 单个工具返回内容的长度上限，超过 30000 字符即认定为巨型输出，触发落盘
PERSIST_THRESHOLD = 30000

# 语法：用 len(str(msgs)) 极简估算消息列表体积（以字符数近似替代分词 token 数，避免引入额外 tokenizer 依赖）
def estimate_size(msgs): return len(str(msgs))


# ==========================================
# 辅助工具：工具调用与消息结构完整性检测
# ==========================================
# 背景知识（关键协议约束）：
#   在主流大模型（如 Claude、OpenAI）的 Function Calling 协议中，
#   Assistant 消息若包含了 tool_use 块，紧随其后的下一条 User 消息必须包含对应的 tool_result 块。
#   如果在中间任意截断导致二者"骨肉分离"，API 会直接返回 400 协议错误。
#   以下辅助函数即用于在裁剪时识别并保护这种成对关系。

def _block_type(block):
    # 语法：三元表达式兼顾两种数据结构——如果是字典取 block["type"]，如果是 SDK 原始对象取 getattr(block, "type")
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _message_has_tool_use(msg):
    """判断一条消息是否为包含 tool_use 的 assistant 消息。"""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    # 语法：any(生成器表达式)，只要内容列表中有一个块是 tool_use 即判定为真
    return any(_block_type(block) == "tool_use" for block in content)


def _is_tool_result_message(msg):
    """判断一条消息是否为包含 tool_result 的 user 消息。"""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)


# ==========================================
# L1 压缩：中间轮次裁剪（snip_compact）
# ==========================================
# 职责：当消息总轮数超过 max_messages 时，切掉中间那些"已经完成且过时的尝试过程"，
#       同时严格保障首部意图、尾部最新动作以及 tool_use/tool_result 的成对完整性。
# L1: snipCompact — trim middle messages
def snip_compact(messages, max_messages=50):
    # 消息数量未超标时直接原样返回，不做任何操作
    if len(messages) <= max_messages: return messages

    # 划分保留策略：头部保留 3 条（用户的初始需求与背景），尾部保留 max_messages - 3 条（最近的交互）
    keep_head, keep_tail = 3, max_messages - 3
    head_end, tail_start = keep_head, len(messages) - keep_tail

    # 边界吸附逻辑 1（保护头部边界）：
    # 如果头部保留的最后一条是 assistant 的 tool_use，那么接下来的 tool_result 绝不能被剪进中间废弃区，
    # 必须把 head_end 往后推，确保成对的 tool_result 也被纳入头部保留区。
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1

    # 边界吸附逻辑 2（保护尾部边界）：
    # 如果尾部保留的第一条是 user 的 tool_result，说明它依赖的前一条 assistant tool_use 差点被切走，
    # 必须把 tail_start 往前拉一位，将前置的 tool_use 一同保留下来。
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1

    # 极端防线：若经过前后吸附后边界交叉，说明上下文几乎全是成对工具，直接放弃强行剪切
    if head_end >= tail_start:
        return messages

    # 语法：切片拼接 `[:head_end] + [提示信息] + [tail_start:]`，中间插入一条提示让模型知道发生过裁剪
    snipped = tail_start - head_end
    return messages[:head_end] + [{"role": "user", "content": f"[snipped {snipped} messages]"}] + messages[tail_start:]


# ==========================================
# L2 压缩：微观就地脱水（micro_compact）
# ==========================================
# 职责：扫描历史中的所有 tool_result，将较早的、冗长的工具执行结果替换为轻量占位文本。
# 设计亮点：
#   保留了所有交互轮次和工具调用的骨架，模型依然知道自己曾经调用过什么工具、在哪个文件，
#   但清除了不再需要的历史输出细节（如几百行代码文件、超长目录清单），仅保留最近 KEEP_RECENT 个结果。
# L2: microCompact — old result placeholders
def collect_tool_results(messages):
    """辅助函数：遍历消息树，收集所有 tool_result 块的索引与字典引用。"""
    blocks = []
    # 语法：enumerate 遍历外层 messages 列表，mi 为消息索引
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue
        # 语法：enumerate 遍历内层 content 块，bi 为块索引
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # 返回三元组：(消息下标, 块下标, 字典对象引用)
                blocks.append((mi, bi, block))
    return blocks

def micro_compact(messages):
    tool_results = collect_tool_results(messages)
    # 如果工具结果总数未达到保留阈值（KEEP_RECENT=3），则无需精简
    if len(tool_results) <= KEEP_RECENT: return messages

    # 语法：`tool_results[:-KEEP_RECENT]` 切片取出除最后 KEEP_RECENT 个之外的所有较早工具结果，
    # `_, _, block` 是解包语法，直接修改 block 字典的 content 字段（内存中就地原地修改，In-place mutation）
    for _, _, block in tool_results[:-KEEP_RECENT]:
        # 仅对超过 120 字符的长文本进行脱水，本身就很短的执行结果（如 "OK"、"Edited file"）保留原样
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# ==========================================
# L3 压缩：工具巨型输出外置落盘（tool_result_budget）
# ==========================================
# 职责：处理单次工具执行产生的爆炸性输出（例如一次性 cat 了 10 万行日志）。
# 处理流程：
#   1. 将完整内容作为文件写入 `.task_outputs/tool-results/<tool_use_id>.txt`；
#   2. 上下文中只保留带有文件路径和前 2000 字符的预览标记 `<persisted-output>`，模型需要全文时再按需读取。
# L3: toolResultBudget — persist large results to disk
def persist_large_output(tool_use_id, output):
    """将超出阈值的单个工具输出持久化到磁盘，返回带文件路径和预览的轻量标签。"""
    if len(output) <= PERSIST_THRESHOLD: return output
    # 创建持久化存放目录
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists(): path.write_text(output)
    # 构造 XML 样式的包装标签，明确告知模型文件绝对路径以及前 2000 字符预览
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"

def tool_result_budget(messages, max_bytes=200_000):
    """对最新一轮的工具返回做总字节预算控制，超标时优先持久化体积最大的块。"""
    # 语法：三元表达式获取最新一条消息；通常工具结果就在最后一条 user 消息中
    last = messages[-1] if messages else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return messages

    # 列表推导式提取当前轮次中的所有 tool_result 块
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    # 总大小在预算限制以内，无需落盘
    if total <= max_bytes: return messages

    # 算法设计（贪心策略）：按每个结果块的字符长度降序排序（从大到小），优先落盘最占空间的大块
    # 语法：lambda p: len(...) 指定排序依据，reverse=True 为降序
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for _, block in ranked:
        if total <= max_bytes: break
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD: continue
        tid = block.get("tool_use_id", "unknown")
        # 将大文本替换为落盘后的持久化摘要标签
        block["content"] = persist_large_output(tid, content)
        # 重新计算最新总字节大小
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


# ==========================================
# L4 压缩：宏观语义提炼与历史归档（auto_compact）
# ==========================================
# 职责：当经过前三层处理后上下文依然过大时，或者模型认为当前阶段告一段落时，
#       启动"写盘归档先行 + LLM 结构化摘要提炼"，将漫长的对话压缩成单一的上下文状态卡片。
# L4: autoCompact — LLM full summary
def write_transcript(messages):
    """安全归档：在丢弃任何上下文之前，必须先将完整消息历史全量落盘（JSONL 格式）。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    # 语法：以当前时间戳命名转储文件，如 transcript_1725716890.jsonl
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages: f.write(json.dumps(msg, default=str) + "\n")
    return path

def summarize_history(messages):
    """调用 LLM 生成结构化会话摘要，提炼当前工程状态。"""
    # 截取前 80000 字符防止摘要本身的输入请求超出模型上下文
    conversation = json.dumps(messages, default=str)[:80000]
    # 关键 Prompt 工程：明确规定必须保留的 5 类核心上下文要素（目标、决策、已修改文件、未完成项、约束条件）
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    # 单独发起一次 LLM 请求生成总结，不携带工具
    response = client.messages.create(model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    # 语法：生成器表达式提取并清洗所有文本块内容，若为空则给出兜底占位文本
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text").strip() or "(empty summary)"

def compact_history(messages):
    """完整的 L4 压缩执行流：先落盘备份 → 再调用模型生成总结 → 用单条卡片替换全部历史。"""
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    # 重构历史：整个对话被彻底重置为仅包含 1 条经过浓缩提炼的 User 状态卡片
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# ==========================================
# 熔断兜底：反应式应急压缩（reactive_compact）
# ==========================================
# 触发时机：当常规估算失效，API 调用抛出 `prompt_too_long` 异常时触发的紧急自愈逻辑。
# 与 auto_compact 的区别：
#   不仅将过去的长历史做总结，还保留最近 5 条正在进行的实时消息（尾部切片），
#   让模型在完成紧缩后能无缝衔接上一瞬间正在执行的代码或工具任务。
# Emergency: reactiveCompact — on API error
def reactive_compact(messages):
    # 1. 同样先将当前完整的故障现场写入日志备查
    transcript = write_transcript(messages)
    # 2. 定位保留尾部的起点（默认保留最近 5 条）
    tail_start = max(0, len(messages) - 5)

    # 3. 边界完整性修复：检查尾部切口是否切断了 tool_use 与 tool_result 的绑定
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1

    # 4. 仅把 tail_start 之前的陈旧历史送给模型做摘要，最近几条消息原封不动保留
    summary = summarize_history(messages[:tail_start])

    # 语法：`*messages[tail_start:]` 为列表解包语法，将浓缩后的历史摘要与最近实时的尾部消息重新无缝缝合
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]


# ═══════════════════════════════════════════════════════════
#  FROM s07: Tool Definitions
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
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "task", "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    {"name": "load_skill", "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    # s08 change: new compact tool — triggers compact_history, not a no-op
    {"name": "compact", "description": "Summarize earlier conversation to free context space.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
}

# FROM s04 (unchanged): Hooks
HOOKS = {"PreToolUse": [], "PostToolUse": []}
def trigger_hooks(event, *args):
    for cb in HOOKS[event]:
        r = cb(*args)
        if r is not None: return r
    return None

DENY_LIST = ["rm -rf /", "sudo", "shutdown"]
def permission_hook(block):
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""): return "Permission denied"
    return None
def log_hook(block):
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

HOOKS["PreToolUse"].append(permission_hook)
HOOKS["PreToolUse"].append(log_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — s08 core: run compaction pipeline before LLM
# ═══════════════════════════════════════════════════════════

# ==========================================
# 控制参数：反应式压缩重试上限
# ==========================================
# 设计考量：限制应急压缩的重试次数为 1 次，防止因持续超长死循环消耗无谓的 API 额度
MAX_REACTIVE_RETRIES = 1  # retry limit for reactive compact


# ==========================================
# 核心中枢：整合四层压缩流水线的 Agent 主循环
# ==========================================
# 机制总览：
#   在每次向 LLM 发送请求前，按"处理成本由低到高（0 API 调用优先，LLM 摘要垫后）"
#   依次执行三级免 API 预处理：L3 巨型落盘 → L1 中间裁剪 → L2 微观脱水。
#   若预处理后体积仍超标，再升级调用 L4 的 LLM 总结。
#   若 API 仍抛出超长异常，启动 reactive 紧急自愈重试。
def agent_loop(messages: list):
    # reactive_retries: 记录当前轮次中触发应急压缩的次数，防止死循环
    reactive_retries = 0
    while True:
        # ─── 阶段一：前置三级免 API 预处理器（Cost = 0，零额外 API 开销）───
        # 设计决策：严格遵循 Claude Code 官方架构的处理顺序：
        #   1. tool_result_budget (L3): 优先将巨型输出落盘，先把最占体积的"巨石"搬走；
        #   2. snip_compact (L1): 当轮数太多时裁剪过期中间轮次，保留首尾关键帧；
        #   3. micro_compact (L2): 将更早的工具长结果就地脱水为占位符。
        # 语法：`messages[:] = ...` 是 Python 的原地切片赋值（In-place Slice Assignment）。
        #       它直接修改原始列表对象内部的数据，而不是让局部变量指向新列表，
        #       这样外部传入的 `history` 变量会同步更新，保证了会话状态一致性。
        # s08 change: three preprocessors (0 API calls, cheap first)
        # Order matches CC source: budget → snip → micro
        messages[:] = tool_result_budget(messages)    # L3: persist large results first
        messages[:] = snip_compact(messages)          # L1: trim middle
        messages[:] = micro_compact(messages)         # L2: old result placeholders

        # ─── 阶段二：主动语义压缩（L4 宏观总结，消耗 1 次 LLM API 调用）───
        # 逻辑：如果经过前三级零成本预处理后，估算字符数依然突破 CONTEXT_LIMIT 警戒线，
        #       说明上下文由大量不可裁剪的高价值内容构成，必须出动大模型做结构化摘要提炼。
        # s08 change: tokens still over threshold → LLM summary (1 API call)
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        # ─── 阶段三：发起模型推理与异常熔断自愈（Reactive Compact）───
        try:
            response = client.messages.create(model=MODEL, system=SYSTEM, messages=messages, tools=TOOLS, max_tokens=8000)
            reactive_retries = 0  # reset on successful API call（API 成功返回后重置应急计数器）
        except Exception as e:
            # 熔断自愈逻辑：若发生字符超限异常（错误信息包含 prompt_too_long 或 too many tokens），
            # 且重试次数未用尽，则触发紧急反应式压缩并立即 continue 重试
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            # 若是网络中断、认证失败或其他未知异常，原样抛出暴露给上层
            raise

        # 记录模型的回复内容到消息历史
        messages.append({"role": "assistant", "content": response.content})
        # 若模型没有调用工具（stop_reason != "tool_use"），说明任务结束或在向用户提问，退出当前循环
        if response.stop_reason != "tool_use": return

        # ─── 阶段四：工具执行与模型自发压缩拦截 ───
        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            print(f"\033[36m> {block.name}\033[0m")

            # 特殊分支：模型自主触发的 `compact` 工具
            # 设计机制：当模型意识到上下文信息过于杂乱时，可主动调用系统预装的 compact 工具自我整理；
            #           此时立即执行全量摘要，并将压缩结果包装为工具返回，直接 break 打断后续工具执行，
            #           带着精简后的新上下文开启下一轮思考。
            # s08: compact tool triggers compact_history, not a no-op string
            if block.name == "compact":
                messages[:] = compact_history(messages)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "[Compacted. Conversation history has been summarized.]"})
                messages.append({"role": "user", "content": results})
                break  # end current turn, start fresh with compacted context

            # 常规工具拦截：执行 PreToolUse 钩子（如权限黑名单拦截）
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)})
                continue

            # 查表分发执行常规工具（bash, read_file, write_file 等）
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # 执行 PostToolUse 钩子（如打印日志）
            trigger_hooks("PostToolUse", block, output)
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        else:
            # 语法：`for...else` 是 Python 的特殊控制流，仅当 for 循环正常结束（未触发 break）时执行。
            # 含义：如果没有调用 compact 工具（未 break），说明是正常的多工具执行流程，
            #       将所有 tool_result 一同打包成单条 user 消息追加到历史中，继续下一轮循环。
            # normal path: no compact was called
            messages.append({"role": "user", "content": results})
            continue
        # compact was called: results already appended above
        # 若触发了 break（即调用了 compact），跳过了 else 分支，此时结果已在上面就地追加，直接进入下一轮循环
        continue


if __name__ == "__main__":
    print("s08: Context Compact — four-layer compaction pipeline")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try: query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt): break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text": print(block.text)
        print()
