# 【中文注释】 启用延迟解析类型注解，便于使用较新的类型标注写法并减少运行时依赖。
from __future__ import annotations

# 【中文注释】 标准库依赖：命令行参数、JSON、环境变量、正则、重试计时以及 HTTP 请求。
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
# 【中文注释】 数据结构与路径/类型工具，用于标题对象序列化和静态类型说明。
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# 【中文注释】 匹配 Markdown 1～6 级 ATX 标题，并分别捕获井号和标题正文。
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 【中文注释】 识别独占一行的 Markdown 图片，清理阶段可将这类资源行移除。
IMAGE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
# 【中文注释】 识别独占一行的 HTML 注释。
COMMENT_RE = re.compile(r"^\s*<!--.*?-->\s*$")
# 【中文注释】 宽松识别带章节号、中文/英文序号等编号前缀的文本，用于发现可能漏掉的标题。
ORDER_PREFIX_RE = re.compile(
    r"^(?:"
    r"第\s*[^\s，。！？]{1,16}\s*[章节篇部卷]"
    r"|(?:chapter|part|section|book)\s+[A-Za-z0-9IVXLCDM]+"
    r"|[（(][0-9〇零一二三四五六七八九十百千万甲乙丙丁戊己庚辛壬癸IVXLCDMivxlcdmA-Za-z]{1,16}[）)]"
    r"|[0-9〇零一二三四五六七八九十百千万甲乙丙丁戊己庚辛壬癸IVXLCDMivxlcdmA-Za-z]{1,16}[、.．:：]"
    r")",
    re.IGNORECASE,
)
# 【中文注释】 识别只有序号而没有语义文字的结构标题，供缺失标题的保守补全使用。
ORDER_ONLY_RE = re.compile(
    r"^(?:[（(]?[0-9〇零一二三四五六七八九十百千万甲乙丙丁戊己庚辛壬癸IVXLCDMivxlcdmA-Za-z]{1,16}[）)]?[、.．:：]?)$",
    re.IGNORECASE,
)


# 【中文注释】 使用不可变数据类保存解析结果，避免后续阶段无意修改原始定位信息。
@dataclass(frozen=True)
# 【中文注释】 标准 Markdown 标题记录：保存稳定 ID、原文行号、层级、父节点以及直属文本块规模。
class Heading:
    id: str
    line: int
    end_line: int
    level: int
    title: str
    parent_id: str | None
    block_lines: int
    block_chars: int


# 【中文注释】 使用不可变数据类保存解析结果，避免后续阶段无意修改原始定位信息。
@dataclass(frozen=True)
# 【中文注释】 疑似漏识别标题记录：除文本外保留上下文标题、空行边界及连续原文行，便于安全校验。
class HeadingCandidate:
    id: str
    line: int
    end_line: int
    text: str
    source_lines: tuple[str, ...]
    previous_heading_id: str | None
    next_heading_id: str | None
    previous_heading_title: str | None
    next_heading_title: str | None
    blank_before: bool
    blank_after: bool


# 【中文注释】 读取简单 .env 配置；只补充当前环境中尚未存在的变量，不覆盖已显式设置的环境变量。
def load_dotenv(path: Path) -> None:
    # 【中文注释】 配置文件不存在时直接返回。
    if not path.is_file():
        return
    # 【中文注释】 逐行解析 KEY=VALUE，并兼容 UTF-8 BOM、export 前缀和成对引号。
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


# 【中文注释】 按“主变量 → 项目兼容变量 → 默认值”的优先级读取配置。
def setting(primary: str, project_name: str | None = None, default: str | None = None) -> str | None:
    return os.getenv(primary) or (os.getenv(project_name) if project_name else None) or default


# 【中文注释】 解析单行 Markdown 标题；成功时返回标题层级和去除首尾空白后的标题文字。
def heading_info(line: str) -> tuple[int, str] | None:
    match = HEADING_RE.match(line)
    return (len(match.group(1)), match.group(2).strip()) if match else None


# 【中文注释】 扫描全文建立标题目录，并用层级栈计算父子关系、标题范围和直属内容规模。
def extract_outline(markdown: str) -> list[Heading]:
    # 【中文注释】 先按行切分全文，后续所有定位统一使用一基原文行号。
    lines = markdown.splitlines()
    # 【中文注释】 收集所有已被 Markdown 语法明确标出的标题位置。
    positions = [(index, heading_info(line)) for index, line in enumerate(lines) if heading_info(line)]
    outline: list[Heading] = []
    # 【中文注释】 层级栈始终保留当前标题的有效祖先链。
    stack: list[tuple[int, str]] = []
    for number, (index, info) in enumerate(positions, 1):
        level, title = info  # type: ignore[misc]
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_id = stack[-1][1] if stack else None
        heading_id = f"h{number:04d}"
        end = positions[number][0] if number < len(positions) else len(lines)
        block = lines[index + 1:end]
        outline.append(Heading(heading_id, index + 1, end, level, title, parent_id,
                               len(block), sum(len(line) for line in block)))
        stack.append((level, heading_id))
    return outline


# 【中文注释】 把不可变标题对象转换为可 JSON 序列化的字典，作为模型输入。
def outline_for_llm(outline: list[Heading]) -> list[dict[str, Any]]:
    return [asdict(item) for item in outline]


# 【中文注释】 从非 Markdown 标题行中高召回提取带序号的疑似标题，并保留其原文边界和相邻标题上下文。
def extract_heading_candidates(markdown: str, outline: list[Heading]) -> list[HeadingCandidate]:
    """提取宽松的标题候选；是否为标题完全交给第二阶段语义判断。"""
    # 【中文注释】 先按行切分全文，后续所有定位统一使用一基原文行号。
    lines = markdown.splitlines()
    # 【中文注释】 已有标题行不再进入候选集合，避免重复判断。
    heading_lines = {item.line for item in outline}
    candidates: list[HeadingCandidate] = []
    heading_index = 0
    for line_number, raw in enumerate(lines, 1):
        while heading_index < len(outline) and outline[heading_index].line < line_number:
            heading_index += 1
        text = raw.strip()
        # 【中文注释】 先过滤已有标题、异常长度、图片/注释、列表/引用/表格以及不含中英文字母的文本。
        if (line_number in heading_lines or not 2 <= len(text) <= 80
                or IMAGE_RE.fullmatch(text) or COMMENT_RE.fullmatch(text)
                or text.startswith(("```", ">", "|", "- ", "* ", "+ "))
                or not re.search(r"[\u3400-\u9fffA-Za-z]", text)):
            continue
        # 第二阶段只修复“顺序缺失”，因此候选必须自身携带某种序号结构。
        # 这里描述的是通用序号语法，不绑定具体章节数字或文档词汇。
        # 【中文注释】 第二阶段只处理可由序号结构支持的漏标题，因此无编号结构的文本不进入候选。
        if not ORDER_PREFIX_RE.match(text):
            continue
        blank_before = line_number == 1 or not lines[line_number - 2].strip()
        blank_after = line_number == len(lines) or not lines[line_number].strip()
        # 保持高召回，但排除明显完整的正文句；不绑定任何具体编号体系。
        if not (blank_before or blank_after):
            continue
        if re.search(r"[。！？!?；;，,]$", text):
            continue
        # 【中文注释】 记录候选前后的最近已知标题，为模型判断结构缺口提供局部上下文。
        previous = outline[heading_index - 1].id if heading_index else None
        following = outline[heading_index].id if heading_index < len(outline) else None
        # 【中文注释】 允许将最多三行短文本拼成一个候选，同时保留每一行原文用于执行前一致性检查。
        source_lines = [text]
        cursor = line_number
        while cursor < len(lines) and len(source_lines) < 3:
            continuation = lines[cursor].strip()
            if (not continuation or heading_info(lines[cursor]) or len(continuation) > 60
                    or IMAGE_RE.fullmatch(continuation) or COMMENT_RE.fullmatch(continuation)
                    or re.search(r"[。！？!?；;]$", continuation)
                    or len("".join(source_lines)) + len(continuation) > 80):
                break
            source_lines.append(continuation)
            cursor += 1
        combined_text = "".join(source_lines)
        candidates.append(HeadingCandidate(
            id=f"c{len(candidates) + 1:04d}", line=line_number, end_line=line_number + len(source_lines) - 1,
            text=combined_text, source_lines=tuple(source_lines),
            previous_heading_id=previous, next_heading_id=following,
            previous_heading_title=outline[heading_index - 1].title if heading_index else None,
            next_heading_title=outline[heading_index].title if heading_index < len(outline) else None,
            blank_before=blank_before, blank_after=blank_after,
        ))
    return candidates


# 【中文注释】 通过 OpenAI 兼容的 chat/completions 接口调用模型，包含超时、有限重试和错误信息处理。
def call_llm(system: str, user: str) -> str:
    # 【中文注释】 读取模型端点、API Key、模型名和调用参数，兼容项目旧变量名。
    base_url = setting("LLM_BASE_URL", "POLITICAL_LLM_BASE_URL",
                       "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    api_key = setting("LLM_API_KEY", "POLITICAL_LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("请设置 LLM_API_KEY、POLITICAL_LLM_API_KEY 或 DASHSCOPE_API_KEY。")
    # 【中文注释】 按 OpenAI Chat Completions 兼容格式构造请求体，temperature 固定为 0 以增强可复现性。
    payload = json.dumps({
        "model": setting("LLM_MODEL", "POLITICAL_DEFAULT_MODEL", "qwen-plus"),
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{base_url}/chat/completions", data=payload,
                                     headers={"Authorization": f"Bearer {api_key}",
                                              "Content-Type": "application/json"})
    timeout = int(setting("LLM_TIMEOUT", "POLITICAL_LLM_TIMEOUT", "120"))
    retries = int(setting("LLM_MAX_RETRIES", "POLITICAL_LLM_MAX_RETRIES", "2"))
    last_error: Exception | None = None
    # 【中文注释】 对超时、限流及常见服务端暂态错误执行指数退避重试。
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())["choices"][0]["message"]["content"]
        # 【中文注释】 非暂态 HTTP 错误立即抛出；可重试状态码则保存错误并进入下一轮。
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"LLM 调用失败：HTTP {exc.code}；{detail[:500]}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(min(2 ** attempt, 4))
    raise RuntimeError(f"LLM 调用失败：{last_error}") from last_error


# 【中文注释】 从模型回复或 Markdown JSON 代码围栏中抽取 JSON 对象，并验证顶层必须为字典。
def json_from_response(text: str) -> dict[str, Any]:
    # 【中文注释】 优先提取代码围栏中的 JSON；否则截取回复中最外层花括号范围。
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return value


# 【中文注释】 调用模型获取结构化 JSON；第一次解析失败时仅进行一次 JSON 语法修复。
def call_structured_json(system: str, prompt: str) -> dict[str, Any]:
    """调用模型并解析 JSON；仅在语法错误时追加一次格式修复调用。"""
    # 【中文注释】 先执行正常模型调用并直接尝试解析。
    response = call_llm(system, prompt)
    try:
        return json_from_response(response)
    # 【中文注释】 仅当 JSON 格式/顶层类型不合法时触发格式修复，不改变正常结果。
    except (json.JSONDecodeError, ValueError) as exc:
        repair_system = (
            "你是 JSON 语法修复器。只修复给定内容的 JSON 语法，保持字段、数组元素、字符串语义和 ID 不变。"
            "只返回一个严格合法的 JSON 对象，不要解释，不要使用 Markdown 代码围栏。"
        )
        repair_prompt = f"解析错误：{exc}\n\n待修复内容：\n{response}"
        try:
            return json_from_response(call_llm(repair_system, repair_prompt))
        except (json.JSONDecodeError, ValueError) as repair_exc:
            raise RuntimeError(f"模型 JSON 无法解析，自动修复仍失败：{repair_exc}") from repair_exc


# 【中文注释】 让模型仅依据目录和块长度判断正文边界、非正文区域、误标题以及合理的标题层级。
def analyze_outline(outline: list[Heading]) -> dict[str, Any]:
    # 【中文注释】 提供严格的示例输出结构，降低模型返回字段漂移的概率。
    schema = {
        "body_start_id": "h0001",
        "body_end_before_id": None,
        "exclude_section_ids": ["h0002"],
        "drop_heading_ids": ["h0004"],
        "heading_levels": [{"id": "h0005", "level": 1}],
        "summary": "简短说明",
    }
    # 【中文注释】 系统提示限定模型角色、可执行动作以及禁止改写原文等边界。
    system = (
        "你是 Markdown 书稿结构编辑。输入只有标题目录与块长度，不含正文。"
        "你的任务是识别正文边界、删除目录/出版信息/序言后记/索引/附录等非正文区，"
        "并根据标题语义和层级逻辑关系重建正文标题层级。不要改写标题文字，不要重新排列正文顺序。"
        "所有决定只能引用输入中的 heading id。"
    )
    # 【中文注释】 用户提示同时提供 JSON 模式、细化规则和实际目录/候选数据。
    prompt = (
        "返回严格 JSON，我会程序解析。格式示例：\n" + json.dumps(schema, ensure_ascii=False)
        + "\n规则：\n"
          "1. body_start_id 是第一条正文标题；body_end_before_id 是正文结束后的第一条标题，没有则为 null。\n"
          "   正文开始必须包含第一篇文章之前的专题/部/章父标题，不能直接从其下第一篇文章开始，即使原 Markdown 层级写反。\n"
          "2. exclude_section_ids 删除指定标题及其到下一标题前的直属文本块。每篇文章后的注释、脚注、参考资料等不属于正文，应列入这里。\n"
          "3. drop_heading_ids 仅删除标题行但保留其后内容，只适合无语义编号或被误识别为标题的正文句。\n"
          "4. heading_levels 为所有保留的语义标题给出 1-6 层级；不得全部压成同一级。专题/部/章通常为 1 级，文章题名为 2 级，文章内部小节为 3 级。以标题语义为准，不沿用错误的原始 # 数量。\n"
          "5. 不得删除正文、日期或称呼；注释/脚注/参考资料默认视为非正文区。不要输出正则或原文正文。\n\n"
          "标题目录：\n" + json.dumps(outline_for_llm(outline), ensure_ascii=False)
    )
    return call_structured_json(system, prompt)


# 【中文注释】 验证第一阶段结构计划中的 ID、正文范围、去重约束和 1～6 级标题层级，并返回规范化计划。
def validate_plan(plan: dict[str, Any], outline: list[Heading]) -> dict[str, Any]:
    # 【中文注释】 建立 ID 与位置索引，所有模型返回 ID 都必须回到原始目录中验证。
    ids = [item.id for item in outline]
    positions = {value: index for index, value in enumerate(ids)}
    if plan.get("body_start_id") not in positions:
        raise ValueError("body_start_id 不存在于标题目录")
    end_id = plan.get("body_end_before_id")
    if end_id is not None and end_id not in positions:
        raise ValueError("body_end_before_id 不存在于标题目录")
    if end_id is not None and positions[end_id] <= positions[plan["body_start_id"]]:
        raise ValueError("正文结束 ID 必须位于正文开始 ID 之后")
    body_start_pos = positions[plan["body_start_id"]]
    body_end_pos = positions[end_id] if end_id is not None else len(ids)
    # 【中文注释】 只保留正文区间内部且真实存在的排除项。
    excluded_ids = [value for value in plan.get("exclude_section_ids", [])
                    if value in positions and body_start_pos <= positions[value] < body_end_pos]
    if len(excluded_ids) != len(set(excluded_ids)):
        raise ValueError("exclude_section_ids 不得重复")
    drop_ids = [value for value in plan.get("drop_heading_ids", [])
                if value in positions and body_start_pos <= positions[value] < body_end_pos]
    if len(drop_ids) != len(set(drop_ids)) or any(value not in positions for value in drop_ids):
        raise ValueError("drop_heading_ids 包含重复或未知 ID")
    # 【中文注释】 逐项校验标题层级，拒绝未知 ID、重复 ID 和 1～6 之外的层级。
    levels: dict[str, int] = {}
    for item in plan.get("heading_levels", []):
        heading_id, level = item.get("id"), item.get("level")
        if heading_id not in positions or heading_id in levels or not isinstance(level, int) or not 1 <= level <= 6:
            raise ValueError(f"无效标题层级：{item}")
        levels[heading_id] = level
    normalized = dict(plan)
    normalized["exclude_section_ids"] = excluded_ids
    normalized["drop_heading_ids"] = drop_ids
    normalized["heading_levels"] = [{"id": key, "level": value} for key, value in levels.items()]
    return normalized


# 【中文注释】 在不改原始定位的前提下生成执行第一阶段计划后的目录视图，并重新计算父子关系。
def planned_outline(outline: list[Heading], plan: dict[str, Any]) -> list[Heading]:
    """生成第一阶段目录视图，同时保留原文 ID/行号供第二阶段对齐。"""
    # 【中文注释】 任何实际应用之前再次经过确定性校验，避免直接信任模型计划。
    plan = validate_plan(plan, outline)
    positions = {item.id: index for index, item in enumerate(outline)}
    start = positions[plan["body_start_id"]]
    end = positions[plan["body_end_before_id"]] if plan.get("body_end_before_id") else len(outline)
    removed = set(plan["exclude_section_ids"]) | set(plan["drop_heading_ids"])
    levels = {item["id"]: item["level"] for item in plan["heading_levels"]}
    result: list[Heading] = []
    # 【中文注释】 层级栈始终保留当前标题的有效祖先链。
    stack: list[tuple[int, str]] = []
    for item in outline[start:end]:
        if item.id in removed:
            continue
        level = levels.get(item.id, item.level)
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_id = stack[-1][1] if stack else None
        result.append(Heading(item.id, item.line, item.end_line, level, item.title,
                              parent_id, item.block_lines, item.block_chars))
        stack.append((level, item.id))
    return result


# 【中文注释】 让模型逐父节点检查兄弟标题序列缺口，并限定为候选提升、纯结构插入或 unresolved 三种处理。
def analyze_missing_headings(outline: list[Heading], candidates: list[HeadingCandidate]) -> dict[str, Any]:
    # 【中文注释】 提供严格的示例输出结构，降低模型返回字段漂移的概率。
    schema = {
        "gaps": [
            {"parent_heading_id": None, "level": 1, "previous_heading_id": "h0008",
             "next_heading_id": "h0009", "missing_title": "七",
             "repair": {"mode": "promote_candidate", "candidate_id": "c0001"},
             "confidence": 0.96, "reason": "同一父节点的兄弟序列从六跳到八"},
            {"parent_heading_id": "h0030", "level": 3, "previous_heading_id": None,
             "next_heading_id": "h0031", "missing_title": "一",
             "repair": {"mode": "insert_structural", "anchor_heading_id": "h0030"},
             "confidence": 0.99, "reason": "同一父节点的子序列从二开始且后续连续"},
            {"parent_heading_id": "h0040", "level": 3, "previous_heading_id": "h0041",
             "next_heading_id": "h0042", "missing_title": "三",
             "repair": {"mode": "unresolved"}, "confidence": 0.7,
             "reason": "存在断序但原文没有可靠边界或候选"}
        ],
        "summary": "简短说明",
    }
    # 【中文注释】 系统提示限定模型角色、可执行动作以及禁止改写原文等边界。
    system = (
        "你是 Markdown 目录校勘员。第一阶段已完成正文清理和标题层级整理。"
        "现在检查每一个父标题下的一级、二级、三级及更深层兄弟标题是否存在顺序缺失。"
        "所有层级使用完全相同的审核标准：按 parent_id 分组检查直接子标题的兄弟序列。"
        "不得按一级/二级/三级分别制定规则。不得删除内容，不得改写语义标题。"
    )
    # 【中文注释】 用户提示同时提供 JSON 模式、细化规则和实际目录/候选数据。
    prompt = (
        "返回严格 JSON，我会程序解析。格式示例：\n" + json.dumps(schema, ensure_ascii=False)
        + "\n规则：\n"
          "1. 对每个 parent_id（包括 null 根节点）的直接子标题分组，依据编号体系、命名模板和语义并列关系检查序列缺口；编号不限于中文数字。\n"
          "2. 每个缺口只输出一个 gaps 项。previous/next 是缺口两侧的同父、同级兄弟；缺口在序列开头或结尾时允许一侧为 null。\n"
          "3. repair.mode 只有 promote_candidate、insert_structural、unresolved。promote_candidate 必须引用候选 candidate_id，沿用候选原文。\n"
          "4. 目录和候选的 line 都是原文行号。候选必须处于缺口对应的父节点内容范围内，level 与兄弟一致；"
          "不要用数组下标或重新计算行号。\n"
          "5. insert_structural 只允许在父节点子序列开头补可严格推出的纯序号：previous_heading_id 必须为 null，"
          "anchor_heading_id 必须等于 parent_heading_id，且至少有两个连续同格式兄弟作为证据。禁止生成语义文字。\n"
          "6. 其他没有原文候选的缺口一律 unresolved，不猜测标题或正文边界。\n"
          "7. 逐个父节点递归审核所有 1-6 级标题，不能只检查一级。confidence 为 0 到 1。\n\n"
          "整理后的目录：\n" + json.dumps(outline_for_llm(outline), ensure_ascii=False)
          + "\n\n正文中的疑似标题候选：\n"
          + json.dumps([asdict(item) for item in candidates], ensure_ascii=False)
    )
    return call_structured_json(system, prompt)


# 【中文注释】 对标题找回方案做程序化复核，重新依据原文位置确定父子/兄弟边界，并按证据和置信度分流。
def validate_recovery_plan(plan: dict[str, Any], candidates: list[HeadingCandidate],
                           outline: list[Heading], min_confidence: float = 0.85) -> dict[str, Any]:
    # 【中文注释】 建立候选和目录的 ID 索引，供找回计划逐项核验。
    candidate_by_id = {item.id: item for item in candidates}
    by_id = {item.id: item for item in outline}
    accepted: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    low_confidence: list[dict[str, Any]] = []
    used_candidates: set[str] = set()
    # 【中文注释】 逐个检查模型报告的目录缺口；任何证据不足项都不会直接执行。
    for gap in plan.get("gaps", []):
        level = gap.get("level")
        confidence = gap.get("confidence")
        repair = gap.get("repair", {})
        mode = repair.get("mode")
        normalized = dict(gap)
        validation_error: str | None = None
        if not isinstance(level, int) or not 1 <= level <= 6:
            validation_error = "无效缺口层级"
        elif not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            validation_error = "无效缺口置信度"
        if validation_error:
            unresolved.append({**gap, "validation_error": validation_error})
            continue

        # 【中文注释】 候选提升模式必须引用唯一且真实的候选，并重新基于原文行号推导结构关系。
        if mode == "promote_candidate":
            candidate_id = repair.get("candidate_id")
            if candidate_id not in candidate_by_id or candidate_id in used_candidates:
                unresolved.append({**gap, "validation_error": f"无效或重复的候选 ID：{candidate_id}"})
                continue
            used_candidates.add(candidate_id)
            candidate = candidate_by_id[candidate_id]
            # 父节点和兄弟边界由原文行号确定，不采用模型容易填错的 ID。
            if level == 1:
                parent_id = None
            else:
                parents = [item for item in outline if item.line < candidate.line and item.level < level]
                parent_id = max(parents, key=lambda item: item.line).id if parents else None
            siblings = sorted((item for item in outline
                               if item.parent_id == parent_id and item.level == level), key=lambda item: item.line)
            previous = [item for item in siblings if item.line < candidate.line]
            following = [item for item in siblings if item.line > candidate.line]
            normalized.update({"parent_heading_id": parent_id,
                               "previous_heading_id": previous[-1].id if previous else None,
                               "next_heading_id": following[0].id if following else None})
        # 【中文注释】 纯结构插入要求标题确为纯序号，并检查父节点、相邻兄弟和正文规模证据。
        elif mode == "insert_structural":
            title = gap.get("missing_title", "")
            anchor_id = repair.get("anchor_heading_id")
            anchor = by_id.get(anchor_id)
            parent_id = anchor_id
            siblings = [item for item in outline
                        if item.parent_id == parent_id and item.level == level and ORDER_ONLY_RE.fullmatch(item.title)]
            siblings.sort(key=lambda item: item.line)
            single_sibling_evidence = bool(
                anchor and len(siblings) == 1
                and anchor.block_chars >= 80
                and siblings[0].line == anchor.end_line + 1
            )
            sequence_evidence = len(siblings) >= 2 or single_sibling_evidence
            if (anchor is None or anchor.level >= level or not sequence_evidence
                    or not ORDER_ONLY_RE.fullmatch(title)):
                unresolved.append({**gap, "validation_error": "纯结构标题插入证据不足"})
                continue
            normalized.update({"parent_heading_id": parent_id, "previous_heading_id": None,
                               "next_heading_id": siblings[0].id})
            confidence = min(confidence, 1.0)
            normalized["confidence"] = confidence
        elif mode == "unresolved":
            unresolved.append(normalized)
            continue
        else:
            unresolved.append({**gap, "validation_error": f"未知缺口修复方式：{mode}"})
            continue
        # 【中文注释】 结构性推断采用更严格的置信度阈值；普通候选提升使用调用方阈值。
        if mode == "insert_structural":
            # 多个连续兄弟依靠序列本身；只有一个兄弟时，还必须有父标题后的实质正文和紧邻边界。
            threshold = max(min_confidence, 0.90 if single_sibling_evidence else 0.95)
        else:
            threshold = min_confidence
        (accepted if confidence >= threshold else low_confidence).append(normalized)
    return {"accepted_gaps": accepted, "unresolved_gaps": unresolved,
            "low_confidence_gaps": low_confidence, "summary": plan.get("summary", ""),
            "min_confidence": min_confidence}


# 【中文注释】 把已接受的候选标题提升回 Markdown 标题，同时核对候选原文未发生变化并输出修复报告。
def apply_heading_recovery(markdown: str, candidates: list[HeadingCandidate],
                           plan: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    by_id = {item.id: item for item in candidates}
    # 【中文注释】 仅把已通过验证且修复方式为候选提升的缺口转成行号映射。
    promoted_gaps = [gap for gap in plan["accepted_gaps"] if gap["repair"]["mode"] == "promote_candidate"]
    promotions = {by_id[gap["repair"]["candidate_id"]].line:
                  (by_id[gap["repair"]["candidate_id"]], gap) for gap in promoted_gaps}
    # 【中文注释】 先按行切分全文，后续所有定位统一使用一基原文行号。
    lines = markdown.splitlines()
    removed_continuations: set[int] = set()
    applied: list[dict[str, Any]] = []
    # 【中文注释】 写回前重新比对候选覆盖的每一行，发现源文变化立即安全停止。
    for line_number, (candidate, item) in promotions.items():
        actual = tuple(value.strip() for value in lines[candidate.line - 1:candidate.end_line])
        if actual != candidate.source_lines:
            raise RuntimeError(f"候选行已变化，安全停止：{candidate.id}")
        lines[line_number - 1] = f"{'#' * item['level']} {candidate.text}"
        removed_continuations.update(range(candidate.line + 1, candidate.end_line + 1))
        applied.append({"candidate_id": candidate.id, "line": line_number,
                        "text": candidate.text, "level": item["level"],
                        "confidence": item["confidence"]})
    text = "\n".join(line for number, line in enumerate(lines, 1)
                     if number not in removed_continuations).strip() + "\n"
    return text, {"promoted_count": len(applied), "promoted": applied,
                  "unresolved_gaps": plan["unresolved_gaps"],
                  "low_confidence_gaps": plan["low_confidence_gaps"]}


# 【中文注释】 统一执行正文裁剪、排除区删除、标题降噪/改级、标题找回和纯结构标题插入，并用最大删除比例兜底。
def execute_plan(markdown: str, outline: list[Heading], plan: dict[str, Any],
                 max_delete_ratio: float = 0.60,
                 candidates: list[HeadingCandidate] | None = None,
                 recovery_plan: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    # 【中文注释】 任何实际应用之前再次经过确定性校验，避免直接信任模型计划。
    plan = validate_plan(plan, outline)
    # 【中文注释】 先按行切分全文，后续所有定位统一使用一基原文行号。
    lines = markdown.splitlines()
    by_id = {item.id: item for item in outline}
    # 【中文注释】 把结构计划中的正文起止标题 ID 转换为实际原文行号。
    start_line = by_id[plan["body_start_id"]].line
    end_line = by_id[plan["body_end_before_id"]].line if plan.get("body_end_before_id") else len(lines) + 1
    recovered_by_line: dict[int, tuple[HeadingCandidate, dict[str, Any]]] = {}
    inserted_after_line: dict[int, dict[str, Any]] = {}
    # 【中文注释】 如果存在第二阶段找回计划，预先建立候选提升和结构插入的行号索引。
    if recovery_plan:
        candidate_by_id = {item.id: item for item in (candidates or [])}
        for gap in recovery_plan["accepted_gaps"]:
            repair = gap["repair"]
            if repair["mode"] == "promote_candidate":
                candidate = candidate_by_id[repair["candidate_id"]]
                recovered_by_line[candidate.line] = (candidate, gap)
            elif repair["mode"] == "insert_structural":
                inserted_after_line[by_id[repair["anchor_heading_id"]].line] = gap
    # 【中文注释】 构造要删除的非正文行区间；若区间中找回了漏标题，则在该标题前截断删除范围。
    excluded = []
    for value in plan["exclude_section_ids"]:
        begin, end = by_id[value].line, by_id[value].end_line + 1
        # 漏识别标题可能被上一节的排除区间吞入；确认找回后在该行前结束排除。
        recovered_starts = [line for line in recovered_by_line if begin < line < end]
        excluded.append((begin, min(recovered_starts) if recovered_starts else end))
    dropped = set(plan["drop_heading_ids"])
    levels = {item["id"]: item["level"] for item in plan["heading_levels"]}
    heading_by_line = {item.line: item for item in outline}
    recovered_continuation_lines = {
        line
        for candidate, _ in recovered_by_line.values()
        for line in range(candidate.line + 1, candidate.end_line + 1)
    }
    output: list[str] = []
    counters = {"outside_body_lines": start_line - 1 + max(0, len(lines) - end_line + 1),
                "excluded_lines": 0, "heading_lines_dropped": 0,
                "heading_levels_changed": 0, "asset_lines_removed": 0,
                "headings_recovered": 0, "structural_headings_inserted": 0}
    # 【中文注释】 只遍历正文边界内的原文，并依次应用排除、资源清理、标题调整和找回操作。
    for line_number in range(start_line, end_line):
        if line_number in recovered_continuation_lines:
            continue
        # 【中文注释】 命中非正文排除区间的行直接跳过，并累加审计计数。
        if any(begin <= line_number < end for begin, end in excluded):
            counters["excluded_lines"] += 1
            continue
        line = lines[line_number - 1]
        heading = heading_by_line.get(line_number)
        if heading and heading.id in dropped:
            counters["heading_lines_dropped"] += 1
            continue
        # 【中文注释】 移除独占一行的图片和 HTML 注释资源。
        if IMAGE_RE.fullmatch(line.strip()) or COMMENT_RE.fullmatch(line.strip()):
            counters["asset_lines_removed"] += 1
            continue
        # 【中文注释】 按照已验证计划重写 Markdown 井号数量，但标题文字保持原样。
        if heading and heading.id in levels:
            new_line = f"{'#' * levels[heading.id]} {heading.title}"
            counters["heading_levels_changed"] += int(new_line != line)
            line = new_line
        # 【中文注释】 命中已确认候选时再次校验原始多行文本，再提升为目标层级标题。
        recovered = recovered_by_line.get(line_number)
        if recovered:
            candidate, item = recovered
            actual = tuple(value.strip() for value in lines[candidate.line - 1:candidate.end_line])
            if actual != candidate.source_lines:
                raise RuntimeError(f"候选行已变化，安全停止：{candidate.id}")
            line = f"{'#' * item['level']} {candidate.text}"
            counters["headings_recovered"] += 1
        output.append(line.rstrip())
        # 【中文注释】 对证据充分的序列开头缺口，在锚点标题之后插入纯结构标题。
        inserted = inserted_after_line.get(line_number)
        if inserted:
            output.extend(["", f"{'#' * inserted['level']} {inserted['missing_title']}"])
            counters["structural_headings_inserted"] += 1
    text = "\n".join(output)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    # 【中文注释】 用字符数计算整体删除比例，作为防止模型计划过度删除的最后保险。
    delete_ratio = max(0.0, (len(markdown) - len(text)) / len(markdown)) if markdown else 0.0
    if delete_ratio > max_delete_ratio:
        raise RuntimeError(f"安全停止：删除比例 {delete_ratio:.2%} 超过 {max_delete_ratio:.2%}")
    report = {"input_characters": len(markdown), "output_characters": len(text),
              "delete_ratio": round(delete_ratio, 6), "counters": counters,
              "body_start_line": start_line, "body_end_before_line": end_line}
    return text, report


# 【中文注释】 根据清理后 Markdown 的标题层级生成缩进式正文目录。
def render_toc(cleaned: str) -> str:
    rows = []
    for line in cleaned.splitlines():
        info = heading_info(line)
        if info:
            level, title = info
            rows.append(f"{'  ' * (level - 1)}- {title}")
    return "# 正文目录\n\n" + "\n".join(rows) + "\n"


# 【中文注释】 把结构计划、清理计数和标题找回结果整理为便于人工复核的 Markdown 报告。
def render_review(plan: dict[str, Any], report: dict[str, Any], outline: list[Heading], cleaned: str,
                  recovery_report: dict[str, Any]) -> str:
    cleaned_headings = sum(1 for line in cleaned.splitlines() if heading_info(line))
    return (
        "# 清理报告\n\n"
        f"- 原始标题数：{len(outline)}\n"
        f"- 正文标题数：{cleaned_headings}\n"
        f"- 正文起始行：{report['body_start_line']}\n"
        f"- 删除比例：{report['delete_ratio']:.2%}\n"
        f"- 排除非正文小节数：{len(plan['exclude_section_ids'])}\n"
        f"- 标题层级调整数：{report['counters']['heading_levels_changed']}\n"
        f"- 图片/注释链接删除数：{report['counters']['asset_lines_removed']}\n\n"
        f"- 找回漏识别标题数：{recovery_report['promoted_count']}\n"
        f"- 推断添加纯结构标题数：{recovery_report['inserted_count']}\n"
        f"- 未解决目录断序数：{len(recovery_report['unresolved_gaps'])}\n"
        f"- 低置信度缺口数：{len(recovery_report['low_confidence_gaps'])}\n\n"
        f"计划摘要：{plan.get('summary', '')}\n"
    )


# 【中文注释】 自动串联目录分析、第一阶段清理、漏标题检测/校验和最终执行，仅返回最终 Markdown。
def automatic_clean(markdown: str, max_delete_ratio: float = 0.60,
                    min_recovery_confidence: float = 0.85) -> str:
    """执行完整自动流程，只返回最终 Markdown，不产生中间文件。"""
    # 【中文注释】 自动模式首先建立原始目录，后续阶段均依赖稳定的标题 ID 与行号。
    outline = extract_outline(markdown)
    if not outline:
        raise RuntimeError("文档中没有 Markdown 标题，无法进行语义目录分析")
    # 【中文注释】 第一阶段由模型生成结构计划，再由确定性代码校验后执行。
    plan = validate_plan(analyze_outline(outline), outline)
    stage1, _ = execute_plan(markdown, outline, plan, max_delete_ratio)
    stage1_outline = planned_outline(outline, plan)
    candidates = extract_heading_candidates(markdown, outline)
    by_id = {item.id: item for item in outline}
    body_start_line = by_id[plan["body_start_id"]].line
    body_end_line = (by_id[plan["body_end_before_id"]].line
                     if plan.get("body_end_before_id") else len(markdown.splitlines()) + 1)
    candidates = [item for item in candidates if body_start_line <= item.line < body_end_line]
    # 【中文注释】 第二阶段对清理后的目录与原文候选做断序分析。
    raw_recovery = analyze_missing_headings(stage1_outline, candidates)
    # 【中文注释】 把模型的断序判断按原文证据与置信度再次筛选。
    recovery_plan = validate_recovery_plan(
        raw_recovery, candidates, stage1_outline, min_recovery_confidence)
    cleaned, _ = execute_plan(markdown, outline, plan, max_delete_ratio,
                              candidates, recovery_plan)
    return cleaned


# 【中文注释】 递归批量清理 Markdown 数据集，保持相对目录结构；目标已存在则跳过，单文件失败不阻塞全批次。
def batch_clean_dataset(dataset_dir: Path, clean_dir: Path,
                        cleaner: Any | None = None) -> dict[str, Any]:
    """批量清理 Markdown；保留相对路径和文件名，已存在目标直接跳过。"""
    if not dataset_dir.is_dir():
        raise ValueError(f"批处理输入不是目录：{dataset_dir}")
    # 【中文注释】 固定输入/输出根目录的绝对路径，便于保持相对路径并避免把输出再次当成输入。
    dataset_root = dataset_dir.resolve()
    clean_root = clean_dir.resolve()
    clean_root.mkdir(parents=True, exist_ok=True)
    process = cleaner or automatic_clean
    # 【中文注释】 递归收集 .md/.markdown 文件，并排除清理结果目录中的文件。
    sources = sorted(
        path for path in dataset_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".markdown"}
        and clean_root not in path.resolve().parents
    )
    report: dict[str, Any] = {"found": len(sources), "processed": 0, "skipped": 0,
                              "failed": 0, "failures": []}
    # 【中文注释】 逐文件处理：保留相对路径、跳过已存在结果，并隔离单文件异常。
    for source_path in sources:
        relative = source_path.relative_to(dataset_root)
        target_path = clean_root / relative
        if target_path.exists():
            report["skipped"] += 1
            print(f"[跳过] {relative}")
            continue
        try:
            source = source_path.read_text(encoding="utf-8-sig")
            cleaned = process(source)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(cleaned, encoding="utf-8")
            report["processed"] += 1
            print(f"[完成] {relative}")
        except Exception as exc:  # 单文件失败不能阻塞整批任务
            report["failed"] += 1
            report["failures"].append({"file": str(relative), "error": str(exc)})
            print(f"[失败] {relative}：{exc}")
    return report


# 【中文注释】 命令行入口：解析运行参数，支持批处理以及复用人工审核后的结构/标题找回计划。
def main() -> None:
    # 【中文注释】 定义命令行接口，包括单文件/批处理、输出目录、预审计划和安全阈值。
    parser = argparse.ArgumentParser(description="按语义目录保留 Markdown 正文并重建标题层级")
    parser.add_argument("input", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("result"))
    parser.add_argument("--batch", action="store_true", help="批量处理 input 目录，只输出最终 Markdown")
    parser.add_argument("--clean-dir", type=Path, default=Path("clean"), help="批处理最终结果目录")
    parser.add_argument("--plan", type=Path, help="使用已审核的结构计划，跳过 LLM")
    parser.add_argument("--recovery-plan", type=Path, help="使用已审核的标题找回计划，跳过第二次 LLM")
    parser.add_argument("--max-delete-ratio", type=float, default=0.60)
    parser.add_argument("--min-recovery-confidence", type=float, default=0.85)
    args = parser.parse_args()
    load_dotenv(Path.cwd() / ".env")
    # 【中文注释】 批处理模式不允许混用仅针对单文档的预审计划参数。
    if args.batch:
        if args.plan or args.recovery_plan:
            parser.error("--batch 不能与 --plan 或 --recovery-plan 同时使用")
        report = batch_clean_dataset(
            args.input, args.clean_dir,
            lambda text: automatic_clean(text, args.max_delete_ratio,
                                         args.min_recovery_confidence),
        )
        print(f"批处理完成：发现 {report['found']}，完成 {report['processed']}，"
              f"跳过 {report['skipped']}，失败 {report['failed']}")
        return
    # 【中文注释】 单文件模式读取输入，并依次输出目录、计划、中间结果、最终结果和审计文件。
    source = args.input.read_text(encoding="utf-8-sig")
    outline = extract_outline(source)
    if not outline:
        raise RuntimeError("文档中没有 Markdown 标题，无法进行语义目录分析")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "outline.json").write_text(
        json.dumps(outline_for_llm(outline), ensure_ascii=False, indent=2), encoding="utf-8")
    # 【中文注释】 若提供人工审核计划则直接读取，否则调用模型生成第一阶段计划。
    raw_plan = (json.loads(args.plan.read_text(encoding="utf-8-sig"))
                if args.plan else analyze_outline(outline))
    plan = validate_plan(raw_plan, outline)
    (args.out_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    stage1, stage1_report = execute_plan(source, outline, plan, args.max_delete_ratio)
    stage1_outline = planned_outline(outline, plan)
    # 目录以第一阶段结果为准，但候选必须来自原文，否则可能已被排除区间吞掉。
    candidates = extract_heading_candidates(source, outline)
    body_start_line = next(item.line for item in outline if item.id == plan["body_start_id"])
    body_end_line = (next(item.line for item in outline if item.id == plan["body_end_before_id"])
                     if plan.get("body_end_before_id") else len(source.splitlines()) + 1)
    candidates = [item for item in candidates if body_start_line <= item.line < body_end_line]
    (args.out_dir / "clean-stage1.md").write_text(stage1, encoding="utf-8")
    (args.out_dir / "toc-stage1.md").write_text(render_toc(stage1), encoding="utf-8")
    (args.out_dir / "outline-stage1.json").write_text(
        json.dumps(outline_for_llm(stage1_outline), ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out_dir / "heading-candidates.json").write_text(
        json.dumps([asdict(item) for item in candidates], ensure_ascii=False, indent=2), encoding="utf-8")
    # 【中文注释】 若提供人工审核的标题找回计划则复用，否则执行第二次模型分析。
    raw_recovery = (json.loads(args.recovery_plan.read_text(encoding="utf-8-sig"))
                    if args.recovery_plan else analyze_missing_headings(stage1_outline, candidates))
    (args.out_dir / "recovery-raw.json").write_text(
        json.dumps(raw_recovery, ensure_ascii=False, indent=2), encoding="utf-8")
    recovery_plan = validate_recovery_plan(raw_recovery, candidates, stage1_outline,
                                           args.min_recovery_confidence)
    (args.out_dir / "recovery-plan.json").write_text(
        json.dumps(recovery_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    cleaned, report = execute_plan(source, outline, plan, args.max_delete_ratio,
                                   candidates, recovery_plan)
    # 【中文注释】 汇总最终执行的标题提升/插入数量以及未解决和低置信度缺口。
    recovery_report = {
        "promoted_count": report["counters"]["headings_recovered"],
        "inserted_count": report["counters"]["structural_headings_inserted"],
        "accepted_gaps": recovery_plan["accepted_gaps"],
        "unresolved_gaps": recovery_plan["unresolved_gaps"],
        "low_confidence_gaps": recovery_plan["low_confidence_gaps"],
    }
    (args.out_dir / "clean.md").write_text(cleaned, encoding="utf-8")
    (args.out_dir / "toc.md").write_text(render_toc(cleaned), encoding="utf-8")
    (args.out_dir / "review.md").write_text(
        render_review(plan, report, outline, cleaned, recovery_report), encoding="utf-8")
    report["stage1"] = stage1_report
    report["heading_recovery"] = recovery_report
    (args.out_dir / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")