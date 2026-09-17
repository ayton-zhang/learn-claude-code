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
#     - L1（snip_compact，剪切）：按规则裁剪中间消息，保留头尾，不调用模型判断内容是否过时。
#     - L2（micro_compact，微缩）：不额外调用模型，将较早工具结果中的长正文原地缩减为占位符。
#     - L3（tool_result_budget，预算）：最新工具结果总量超预算时，将符合大小阈值的结果落盘，留下路径与预览。
#     - L4（compact_history，提炼）：调用模型总结当前历史，再用结构化任务摘要替换历史消息。
#     - Emergency（reactive_compact，熔断自救）：当 API 真正爆出超长错误时的应急修复。
# L1～L4 是策略编号，不是执行顺序；主循环实际先 L3 落盘，再 L1 裁剪、L2 微缩，
# 最后用 estimate_size 判断是否需要 L4。前三步不发起额外模型请求，但仍有计算或磁盘开销。
# 这里压缩的是 messages 中的会话记录，不是模型权重，也不是模型内部的 KV Cache。

# --- 阈值与超参数设定 ---
# CONTEXT_LIMIT: 上下文字符数上限阈值，超过该阈值时自动触发 L4 的 LLM 总结压缩
# 这是本程序主动压缩的触发线，不是服务端模型的真实上下文窗口。
# 主循环在前三层处理后用 > 比较：估算值恰好等于 50000 时不会因此触发 L4。
CONTEXT_LIMIT = 50000
# KEEP_RECENT: L2 微缩时保护的最近工具结果块数量，防止刚拿到的结果被过早擦除
# “最近”按历史中的结果块顺序计算；同一次模型回复请求多个工具时，可能产生多个结果块。
# 这是正整数保留数量，与保留多少条消息、多少轮问答不是同一回事。
KEEP_RECENT = 3
# PERSIST_THRESHOLD: 单个工具输出进入持久化处理的字符数阈值，不是所有输出的强制长度上限
# 该阈值由持久化函数和 L3 预算循环使用；等于阈值时仍保留原文。
# 在当前主循环里，只有最新一条消息的工具结果总量先超预算，才会尝试逐个大块落盘。
PERSIST_THRESHOLD = 30000

# ==========================================
# 大小估算：用消息的字符串表示决定是否升级到语义摘要
# ==========================================
# 参数 msgs 是当前消息列表，返回非负整数；调用方只用它与 CONTEXT_LIMIT 比较。
# 语法：用 len(str(msgs)) 极简估算消息列表体积（以字符数近似替代分词 token 数，避免引入额外 tokenizer 依赖）
# str(msgs) 包括列表、字典的结构符号及其中对象的字符串表示，len 再统计字符数量。
# 因此它不是单纯累加正文长度，也不是实际 API 请求的字节数或精确 token 数。
# 此函数没有统计单独传入 API 的 SYSTEM、TOOLS，也不会修改 msgs；估算不足时由应急路径兜底。
# 语法：冒号后直接写 return 是单行函数体，与换行缩进的写法作用相同。
def estimate_size(msgs):
    return len(str(msgs))


# ==========================================
# 辅助工具：工具调用与消息结构完整性检测
# ==========================================
# 背景知识（关键协议约束）：
#   本例使用 Anthropic 风格的 tool_use/tool_result 内容块表示工具调用与返回。
#   assistant 的工具调用与后续 user 中的结果通过调用标识关联，裁剪时应一起保留。
#   如果切断这种关系，后续 API 请求可能因消息结构不合法被拒绝。
#   以下辅助函数即用于在裁剪时识别并保护这种成对关系。
# 数据流：模型返回 assistant 内容块 → 主循环执行 tool_use 指定的工具 →
# 将带 tool_use_id 的 tool_result 放入 user 消息 → 下次模型请求读取执行结果。
# 此处 user 是工具协议使用的消息角色，不代表这段结果一定由真人输入。
# 这些辅助函数只识别消息类型，供 snip_compact 与 reactive_compact 调整切片边界；
# 它们不执行工具、不核验权限，也不会逐个检查调用 id 与结果 id 是否匹配。

# ==========================================
# 块类型适配：统一读取字典与 SDK 对象的 type
# ==========================================
# 参数 block 是一块消息内容；返回如 text、tool_use 的类型字符串，字段缺失时返回 None。
# 下划线前缀表示“内部辅助函数”的命名惯例，不是 Python 强制的访问限制。
def _block_type(block):
    # 语法：字典用 get 读取键，SDK 对象用 getattr 读取属性，统一得到内容块类型。
    # 三元表达式先判断 isinstance(block, dict)，只执行选中的读取分支。
    # dict.get("type") 缺键时返回 None，不同于 block["type"] 会抛 KeyError；
    # getattr(block, "type", None) 读取对象属性，第三个参数提供属性不存在时的默认值。
    # 这样能同时处理手工构造的字典块，以及 response.content 里的 SDK 内容块。
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


# ==========================================
# 调用消息识别：是否有助手发出的工具执行请求
# ==========================================
# 参数 msg 是单条消息字典；返回布尔值，让裁剪器决定是否连同后续工具结果一起保留。
def _message_has_tool_use(msg):
    """判断一条消息是否为包含 tool_use 的 assistant 消息。"""
    # 先按角色排除不可能的消息：即使用户文字写着 tool_use，也不等于助手发出了调用。
    # get 缺少 role 时返回 None，同样会进入 False 分支，提前结束本函数。
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    # content 可能是普通字符串，也可能是由多个内容块构成的列表。
    # 这里只检查列表结构，避免把普通文本当成块序列；缺少 content 时 None 也会被排除。
    if not isinstance(content, list):
        return False
    # 语法：any(生成器表达式)，只要内容列表中有一个块是 tool_use 即判定为真
    # 生成器逐块调用 _block_type，any 遇到第一个 True 就停止，空列表则返回 False。
    # 同一消息可以同时含 text 与 tool_use；只需发现一个调用块即可，不要求每块都是调用。
    return any(_block_type(block) == "tool_use" for block in content)


# ==========================================
# 结果消息识别：是否有主循环写回的工具执行结果
# ==========================================
# 参数 msg 是单条消息字典；返回布尔值，帮助裁剪器识别“不应脱离前置调用”的尾部结果。
def _is_tool_result_message(msg):
    """判断一条消息是否为包含 tool_result 的 user 消息。"""
    # 工具执行结果在本例中装入 user 消息；其他角色即使带相似字段，也不按结果消息处理。
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    # 只进入块列表，排除普通用户输入的字符串，以及缺少 content 的消息。
    if not isinstance(content, list):
        return False
    # and 短路求值：先确认 block 是字典，才调用 .get，避免对非字典块使用字典方法。
    # 与上面的调用识别不同，这里只接受字典结果块，因为主循环就是用字典构造 tool_result。
    # any 仍是“存在一个即可”：结果可以与其他块共存；这里只看 type，不验证正文或标识。
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)


# ==========================================
# L1 压缩：中间轮次裁剪（snip_compact）
# ==========================================
# 职责：当消息总条数超过 max_messages 时，按位置切掉中间历史，
#       同时保留首部与尾部，并对相邻 tool_use/tool_result 做边界保护。
# L1: snipCompact — trim middle messages
# 参数：messages 是按时间排列的消息列表，每条含 role 与 content；max_messages 默认 50。
# 这里数的是“消息条数”，一次问答或一轮工具调用可能占多条消息，不是 50 轮对话。
# 默认值大于固定保留的头部 3 条；函数没有校验自定义阈值，不能把任意小值当作可靠配置。
# 这层按位置取舍，不判断内容是否真的过时；调用方用返回列表替换当前历史。
def snip_compact(messages, max_messages=50):
    # 消息数量未超标时直接原样返回，不做任何操作
    if len(messages) <= max_messages: return messages

    # 划分保留策略：头部保留 3 条（用户的初始需求与背景），尾部保留 max_messages - 3 条（最近的交互）
    keep_head, keep_tail = 3, max_messages - 3
    # 两次赋值都是元组解包：先确定头尾各留多少条，再确定切片边界。
    # head_end 是头部右侧的“开区间”边界，tail_start 是尾部第一条的下标。
    # 待删除区间为 [head_end, tail_start)，右端本身不会被删除。
    head_end, tail_start = keep_head, len(messages) - keep_tail

    # 边界吸附逻辑 1（保护头部边界）：
    # 如果头部保留的最后一条是 assistant 的 tool_use，那么接下来的 tool_result 绝不能被剪进中间废弃区，
    # 必须把 head_end 往后推，确保成对的 tool_result 也被纳入头部保留区。
    # and 从左到右短路求值：先确认有头部，再访问 head_end - 1。
    # 辅助函数只检测消息中是否含指定块类型，不逐个校验 tool_use_id 是否匹配；
    # 因此这是对原本有效、相邻配对消息的边界保护，不是完整的协议修复器。
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

    # 极端防线：若经过前后边界调整后保留区重叠，直接放弃本次剪切
    # 更准确地说，边界相遇意味着已经没有可安全删除的中间区间，
    # 并不能据此断定所有消息都是工具消息。
    if head_end >= tail_start:
        return messages

    # 语法：切片拼接 `[:head_end] + [提示信息] + [tail_start:]`，中间插入一条提示让模型知道发生过裁剪
    snipped = tail_start - head_end
    # 拼接创建新列表，但保留下来的消息字典仍与原列表共享，未进行深拷贝。
    # snipped 记录实际删除条数；新增提示占 1 条，边界扩展也可能多留消息，
    # 所以 max_messages 是裁剪触发阈值，并非返回列表长度的严格上限。
    return messages[:head_end] + [{"role": "user", "content": f"[snipped {snipped} messages]"}] + messages[tail_start:]


# ==========================================
# L2 压缩：微观就地脱水（micro_compact）
# ==========================================
# 职责：扫描历史中的所有 tool_result，将较早的、冗长的工具执行结果替换为轻量占位文本。
# 设计亮点：
#   保留了所有交互轮次和工具调用的骨架，模型依然知道自己曾经调用过什么工具、在哪个文件，
#   但清除了不再需要的历史输出细节（如几百行代码文件、超长目录清单），仅保留最近 KEEP_RECENT 个结果。
# L2: microCompact — old result placeholders
# ==========================================
# 结果定位：收集可原地修改的工具返回块
# ==========================================
# messages 来自当前历史；返回值按消息顺序、块顺序排列，供 L2 判断哪些结果较早。
def collect_tool_results(messages):
    """辅助函数：遍历消息树，收集所有 tool_result 块的索引与字典引用。"""
    blocks = []
    # 语法：enumerate 遍历外层 messages 列表，mi 为消息索引
    for mi, msg in enumerate(messages):
        # 普通用户文本的 content 可以是字符串，不能当块列表遍历。
        # or 短路组合两个排除条件；continue 跳过本条消息，进入下一条。
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue
        # 语法：enumerate 遍历内层 content 块，bi 为块索引
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # 返回三元组：(消息下标, 块下标, 字典对象引用)
                # block 是原字典的引用，不是副本：后续改它的 content，历史里也立即变更。
                # mi、bi 保留定位信息，当前微缩逻辑只需要第三项 block。
                blocks.append((mi, bi, block))
    return blocks

# ==========================================
# 微缩执行：保留最近结果，清空较早的大段正文
# ==========================================
# 参数 messages 是共享历史；本函数修改嵌套字典，并返回同一个列表对象。
# 最近 KEEP_RECENT 个指“工具结果块”，多个块可能属于同一条 user 消息。
def micro_compact(messages):
    tool_results = collect_tool_results(messages)
    # 如果工具结果总数未达到保留阈值（KEEP_RECENT=3），则无需精简
    if len(tool_results) <= KEEP_RECENT: return messages

    # 语法：`tool_results[:-KEEP_RECENT]` 切片取出除最后 KEEP_RECENT 个之外的所有较早工具结果，
    # `_, _, block` 是解包语法，直接修改 block 字典的 content 字段（内存中就地原地修改，In-place mutation）
    # 两个 _ 都表示“这两个索引不用”；它仍是普通变量，不会参与后续逻辑。
    # 这里依赖 KEEP_RECENT 为正数；Python 的 -0 等于 0，不能用该切片表达“保留零个”。
    for _, _, block in tool_results[:-KEEP_RECENT]:
        # 仅对超过 120 字符的长文本进行脱水，本身就很短的执行结果（如 "OK"、"Edited file"）保留原样
        # 主循环把工具输出转成字符串存入 content，因此这里 len 按字符计算。
        # get 的空串默认值用于缺少 content 键的情况，并不负责转换已有的其他类型。
        if len(block.get("content", "")) > 120:
            # 只改正文，保留 type 和 tool_use_id，让“调用—返回”的关系仍可识别。
            # 占位符只是提示需要时重新执行；这一步本身不会备份原文或自动重跑工具。
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
# 参数：tool_use_id 通常来自工具调用的标识，用于命名文件；output 是完整输出字符串。
# 返回值始终供调用方放入上下文：短输出返回原文，长输出返回“路径 + 预览”。
def persist_large_output(tool_use_id, output):
    """将超出阈值的单个工具输出持久化到磁盘，返回带文件路径和预览的轻量标签。"""
    if len(output) <= PERSIST_THRESHOLD: return output
    # 创建持久化存放目录
    # parents=True 同时创建缺失的父目录；exist_ok=True 允许目录已经存在。
    # 这些是实际磁盘操作；此处不捕获异常，创建或写入失败会向调用方传播。
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # Path 的 / 运算符拼接路径；目录基于程序启动时的工作目录，不是固定脚本目录。
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    # 文件已存在就不覆盖，避免同一标识反复写盘；这里没有检查旧文件是否与新输出一致。
    # 因此调用方需要提供合适且唯一的标识，函数本身没有做文件名清洗。
    if not path.exists(): path.write_text(output)
    # 构造 XML 样式的包装标签，明确告知模型文件绝对路径以及前 2000 字符预览
    # output[:2000] 取前 2000 个字符而非 token；标签是普通文本，模型需另行调用工具读文件。
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"

# ==========================================
# 单轮预算：按体积从大到小外置最新工具输出
# ==========================================
# 参数：messages 是当前历史；max_bytes 默认 200_000（下划线只是数字分隔符）。
# 注意：虽然参数和原有文档称“字节”，实现实际用 len(str(...)) 计算字符数，
# 没有编码为字节，也没有分词；预算仅覆盖最后一条消息里的工具结果正文。
# 返回同一 messages 列表，内部结果块的正文可能已被原地替换。
def tool_result_budget(messages, max_bytes=200_000):
    """对最新一轮的工具返回做总字节预算控制，超标时优先持久化体积最大的块。"""
    # 语法：三元表达式获取最新一条消息；通常工具结果就在最后一条 user 消息中
    last = messages[-1] if messages else None
    # 空历史、末条不是 user、或 content 不是块列表时，均不进入预算计算。
    # 只检查最后一条，避免每次重复扫描全部历史；更早结果交给 L2 精简。
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return messages

    # 列表推导式提取当前轮次中的所有 tool_result 块
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    # blocks 中的 (i, b) 保存块下标和原字典引用；生成器逐个产生正文长度给 sum 求和。
    # str 让这里也能估算非字符串值的文本表示，但不代表按原始结构精确序列化。
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    # 总大小在预算限制以内，无需落盘
    if total <= max_bytes: return messages

    # 算法设计（贪心策略）：按每个结果块的字符长度降序排序（从大到小），优先落盘最占空间的大块
    # 语法：lambda p: len(...) 指定排序依据，reverse=True 为降序
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    # sorted 返回新的排序列表，不改变原消息中各工具结果的顺序。
    # 排序后的 block 仍指向原字典，所以修改可以同步回写 last 和 messages。
    for _, block in ranked:
        if total <= max_bytes: break
        content = str(block.get("content", ""))
        # 总量超标仍只处理单块大于 PERSIST_THRESHOLD 的结果。
        # 若总量来自很多中等大小的块，遍历完仍可能超预算；这里不是硬性限流器。
        if len(content) <= PERSIST_THRESHOLD: continue
        # 缺少标识时使用 unknown；多个缺失标识的结果可能共用同一文件名。
        tid = block.get("tool_use_id", "unknown")
        # 将大文本替换为落盘后的持久化摘要标签
        block["content"] = persist_large_output(tid, content)
        # 重新计算最新的正文字符总量
        # 必须按替换后的正文重算字符总量：预览标签自身也占空间，不能把旧长度直接减光。
        # 下一轮循环检查新总量，达标即 break，避免继续产生不必要的磁盘写入。
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


# ==========================================
# L4 压缩：宏观语义提炼与历史归档（auto_compact）
# ==========================================
# 职责：当经过前三层处理后上下文依然过大时，或者模型认为当前阶段告一段落时，
#       启动"写盘归档先行 + LLM 结构化摘要提炼"，将漫长的对话压缩成单一的上下文状态卡片。
# L4: autoCompact — LLM full summary
# ==========================================
# 历史归档：把当前传入的消息写成逐行记录
# ==========================================
# 参数 messages 是归档时仍留在内存中的历史；已经被 L1 丢弃或 L2 替换的内容无法在此恢复。
# 返回 Path 对象供日志展示；本函数不修改消息，也不会把文件内容自动送回模型。
def write_transcript(messages):
    """安全归档：在丢弃任何上下文之前，必须先将完整消息历史全量落盘（JSONL 格式）。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    # 语法：以当前时间戳命名转储文件，如 transcript_1725716890.jsonl
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    # 文件名精确到秒，同秒多次归档可能同名；w 模式会截断已有文件，并非防覆盖备份。
    # with 管理文件生命周期：正常完成或块内抛异常时，都会关闭已打开的文件。
    with path.open("w") as f:
        # JSONL 每行是一条消息的 JSON；末尾换行用于分隔记录，而非整个列表一次写入。
        # default=str 让 JSON 不认识的对象（例如 SDK 内容块）退化为字符串，
        # 避免这类对象直接导致序列化报错，但不保证能够完整还原其原始结构。
        for msg in messages: f.write(json.dumps(msg, default=str) + "\n")
    return path

# ==========================================
# 语义摘要：用额外一次模型请求生成任务交接卡
# ==========================================
# 参数 messages 可以是完整历史，也可以是应急路径传入的旧历史切片；返回纯文本摘要。
# 该函数只负责总结，不归档、不替换历史，也不执行摘要文本中的任何工具指令。
def summarize_history(messages):
    """调用 LLM 生成结构化会话摘要，提炼当前工程状态。"""
    # 截取前 80000 字符，限制摘要请求中的历史文本体积
    # 这是 JSON 文本的前缀截断，会舍弃超过上限的尾部，甚至切在某条消息中间。
    # 80000 是字符上限而非 token 上限；只能限制输入文本长度，不能保证请求一定被接受。
    conversation = json.dumps(messages, default=str)[:80000]
    # 关键 Prompt 工程：明确规定必须保留的 5 类核心上下文要素（目标、决策、已修改文件、未完成项、约束条件）
    # 圆括号内相邻的字符串字面量会自动拼接，最后用 + 接上待总结的对话文本。
    # 这五项像“交接班清单”，帮助下一次推理接续目标，但模型摘要仍可能遗漏细节。
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    # 单独发起一次 LLM 请求生成总结，不携带工具
    # 使用全局 MODEL 和 client，同步等待响应；max_tokens=2000 限制的是生成长度。
    # 此请求没有传主循环的 SYSTEM 或 TOOLS，历史被包装成一条 user 文本输入。
    # API 异常不会变成空摘要，而是直接向调用方传播；下方兜底只处理返回文本为空。
    response = client.messages.create(model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    # 语法：生成器表达式提取并清洗所有文本块内容，若为空则给出兜底占位文本
    # 先按 type 筛出文本块，再用 getattr 读取 text（属性缺失时取空串）。
    # join 用换行连接多个文本块，strip 去掉整体首尾空白；空字符串为假，
    # 所以末尾的 or 才会改用占位文本，并不是对任意错误进行兜底。
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text").strip() or "(empty summary)"

# ==========================================
# 全量替换：先归档，再总结，最后构造新的历史列表
# ==========================================
# 参数 messages 是当前历史；返回只有一条 user 消息的新列表，由主循环切片赋值写回。
# 函数本身不判断大小阈值，自动压缩分支和模型主动调用 compact 都可进入此流程。
def compact_history(messages):
    """完整的 L4 压缩执行流：先落盘备份 → 再调用模型生成总结 → 用单条卡片替换全部历史。"""
    # 顺序有意安排为“备份成功 → 请求摘要 → 返回新历史”：
    # 写盘失败则不发摘要请求；摘要失败则异常上抛，调用方的历史替换尚未执行。
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    # 重构历史：整个对话被彻底重置为仅包含 1 条经过浓缩提炼的 User 状态卡片
    # 原来的角色交替与工具块不再保留，只把摘要作为后续推理的文字背景。
    # transcript_path 只输出到终端，并未放入返回的状态卡；读盘恢复也不是自动发生的。
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# ==========================================
# 熔断兜底：反应式应急压缩（reactive_compact）
# ==========================================
# 触发时机：当常规估算失效，API 调用抛出 `prompt_too_long` 异常时触发的紧急自愈逻辑。
# 与 auto_compact 的区别：
#   不仅将过去的长历史做总结，还保留最近 5 条正在进行的实时消息（尾部切片），
#   尽量保留当前操作所需的原始信息，供下一次模型请求继续使用。
# Emergency: reactiveCompact — on API error
# 参数 messages 是主请求报超长时的当前历史；返回“旧历史摘要 + 最近消息”的新列表。
# 与全量压缩相比，这里优先保留最近工具结果的原文；重试次数由外层主循环控制。
def reactive_compact(messages):
    # 1. 同样先将当前完整的故障现场写入日志备查
    # transcript 接收归档路径，但本函数后面未使用；文件写入这一副作用仍会发生。
    transcript = write_transcript(messages)
    # 2. 定位保留尾部的起点（默认保留最近 5 条）
    # max 把起点限制为非负数：不足 5 条则从 0 开始保留，不会用负下标误切。
    # 边界修复可能再多保留 1 条，因此 5 条是基准，而非绝对数量。
    tail_start = max(0, len(messages) - 5)

    # 3. 边界完整性修复：检查尾部切口是否切断了 tool_use 与 tool_result 的绑定
    # and 短路保证先检查下标范围，再读取消息；仅在结果块紧邻调用块时向前扩一格。
    # 与 L1 一样，这里按块类型判断，不检查每一个调用标识，也不修复原本已损坏的历史。
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1

    # 4. 仅把 tail_start 之前的陈旧历史送给模型做摘要，最近几条消息原封不动保留
    # [:tail_start] 不含尾部第一条，避免摘要与保留原文覆盖同一段历史。
    # tail_start 为 0 时会对空列表也发一次摘要请求；没有额外的空历史跳过分支。
    # 若摘要请求仍超长或遇到网络异常，本函数没有内部重试，会继续向外抛出。
    summary = summarize_history(messages[:tail_start])

    # 语法：`*messages[tail_start:]` 为列表解包语法，将浓缩后的历史摘要与最近实时的尾部消息重新无缝缝合
    # * 把切片中的每条消息展开为列表元素，不是将整个尾部列表作为一个嵌套元素加入。
    # 新列表的第一项是摘要字典，后续项仍引用原来的尾部消息；旧列表本身未被改写。
    # 保留尾部有助于继续任务，但压缩后能否满足模型长度限制仍取决于实际内容。
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
        try:
            default_query = "​Read every file in s08_context_compact/"
            query = input(f"\033[36ms08 >> {default_query} \033[0m") or default_query
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text": print(block.text)
        print()
