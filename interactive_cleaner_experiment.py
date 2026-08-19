"""Fact-base Markdown cleaner with action-only LLM decisions and replay plans."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tqdm import tqdm

HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#{1,6})?[ \t]*$")
OCR_HEADING_MARKER_RE = re.compile(r"^[ \t]{0,3}(?:#{1,6}|＃{1,6})[ \t]*\S")
NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第(?:[〇零一二三四五六七八九十百两]+|\d+)(?:编|章|节|部分|篇|讲|专题)"
    r"|[一二三四五六七八九十百]+[、．.]"
    r"|[（(](?:[一二三四五六七八九十百]+|\d+)[）)]"
    r"|\d+[、．.]"
    r")\s*\S"
)
_INTERACTIVE_CALL_NO = 0
_INTERACTIVE_EMPTY_CORE = "<<<INTERACTIVE_EMPTY_CORE>>>"
_INTERACTIVE_EXACT_PROOFS: list[dict[str, Any]] = []
_INTERACTIVE_ACTION_AUDIT: list[dict[str, Any]] = []
_INTERACTIVE_AUTOMATIC_DROPS: list[dict[str, Any]] = []
_PLAN_ACTIONS: list[dict[str, Any]] | None = None
_PLAN_CURSOR = 0
_PLAN_CHECKPOINT_PATH: Path | None = None
_PLAN_CHECKPOINT_SOURCE_SHA256: str | None = None
_PLAN_CHECKPOINT_WINDOW_CHARS: int | None = None
PURE_SOURCE_RE = re.compile(
    r"^\s*<!--\s*(?:(?:source|pdf)\s*pages?|page|来源页)\s*(?::|=)?\s*"
    r"\d+(?:\s*[-–—,，]\s*\d+)*\s*-->\s*$",
    re.I,
)
PURE_IMAGE_RE = re.compile(
    # Hard-delete only an image-only line.  Deliberately reject arbitrary text
    # after the URL: a broad `[^\n]*` can swallow `![](a.jpg) 正文 (注)`.
    r"^\s*(?:"
    r"!\[[^]\n]*\]\(\s*<?[^\s()<>]+>?(?:\s+[\"'][^\"'\n]*[\"'])?\s*\)"
    r"|!\[[^]\n]*\]\[[^]\n]*\]"
    r"|<img\b[^>]*?/?>"
    r")\s*$",
    re.I,
)
OCR_SYSTEM_NOISE_RE = re.compile(
    r"^\s*(?:The Ground Truth image displays|OCR result must ignore|OCR has hallucinated text)"
    r"\b[^\n]{0,1000}\s*$",
    re.I,
)
CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
PURE_COURTESY_RE = re.compile(
    r"^\s*(?:"
    r"(?:尊敬的|敬爱的|亲爱的)[^。！？!?：:]{1,120}[，,!！：:]?|"
    r"各位[^。！？!?：:]{1,120}[，,!！：:]|"
    r"(?:女士们|先生们|同志们|朋友们)(?:[、，,\s]*(?:女士们|先生们|同志们|朋友们))*[！!：:，,。.]?|"
    r"大家好[！!。.]?|谢谢(?:大家)?[！!。.]?"
    r")\s*$"
)
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(?:[^\n]*)$")
CORE_LINE_LABEL_RE = re.compile(r"^C\d{4,}\t")
CORE_LINE_REFERENCE_RE = re.compile(r"^C0*(\d+)$", re.I)
HEADING_REFERENCE_RE = re.compile(r"^H0*(\d+)$", re.I)
PENDING_HEADING_LIMIT = 30


def _fenced_line_mask(lines: list[str]) -> list[bool]:
    """Mark fenced-code lines so document regexes never treat them as prose."""
    mask = [False] * len(lines)
    fence_char: str | None = None
    fence_len = 0
    for index, line in enumerate(lines):
        match = FENCE_RE.match(line)
        if fence_char is None:
            if match:
                token = match.group(1)
                fence_char, fence_len = token[0], len(token)
                mask[index] = True
            continue
        mask[index] = True
        stripped = line.lstrip(" \t")
        if stripped.startswith(fence_char * fence_len):
            tail = stripped[fence_len:]
            if not tail.strip(fence_char + " \t"):
                fence_char, fence_len = None, 0
    return mask


def preprocess_markdown(markdown: str) -> tuple[str, dict[str, int]]:
    """运行在 LLM 前的确定性卫生清洗；不推断标题，也不修改正文文本。"""
    raw_lines = markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    fenced = _fenced_line_mask(raw_lines)
    output: list[str] = []
    report = {
        "control_characters_removed": 0,
        "image_lines_removed": 0,
        "source_marker_lines_removed": 0,
        "ocr_system_noise_lines_removed": 0,
        "extra_blank_lines_removed": 0,
    }
    blank_run = 0
    for index, raw_line in enumerate(raw_lines):
        line, removed = CONTROL_CHARACTER_RE.subn("", raw_line)
        report["control_characters_removed"] += removed
        if not fenced[index]:
            if PURE_IMAGE_RE.fullmatch(line):
                report["image_lines_removed"] += 1
                continue
            if PURE_SOURCE_RE.fullmatch(line):
                report["source_marker_lines_removed"] += 1
                continue
            if OCR_SYSTEM_NOISE_RE.fullmatch(line):
                report["ocr_system_noise_lines_removed"] += 1
                continue
        if not fenced[index] and not line.strip():
            blank_run += 1
            if blank_run > 2:
                report["extra_blank_lines_removed"] += 1
                continue
        else:
            blank_run = 0
        output.append(line)
    # 不用 strip()，避免破坏缩进代码块或 Markdown 的行尾空格语义。
    return ("\n".join(output).rstrip("\n") + "\n" if output else ""), report


def _interactive_core(prompt: str) -> str:
    marker = "【核心区】\n"
    tail = "\n\n【后置只读上下文】"
    if marker not in prompt or tail not in prompt:
        raise RuntimeError("交互模式无法从 prompt 定位核心区")
    core = prompt.split(marker, 1)[1].split(tail, 1)[0]
    # Prompt 中的 C0001 标签只供模型精确引用；执行器必须继续拿到原始 Markdown。
    output: list[str] = []
    for line in core.splitlines():
        value = CORE_LINE_LABEL_RE.sub("", line)
        # <BLANK> 只用于让 LLM 明确看见空行，执行动作前必须还原为空字符串。
        if value.strip() == "<BLANK>":
            value = ""
        output.append(value)
    return "\n".join(output)


def _action_line_number(value: Any) -> int:
    """只接受核心区可见的 C 行号，避免把全文参考位置当成动作目标。"""
    if not isinstance(value, str):
        raise ValueError("行号必须使用核心区 C 标签，例如 C0001；不接受裸数字")
    match = CORE_LINE_REFERENCE_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("行号必须使用核心区 C 标签，例如 C0001")
    return int(match.group(1))


def _drop_range_references(item: Any) -> tuple[Any, Any]:
    """读取删除区间，同时容错常见的对象写法。

    协议首选 ``["C0001", "C0003"]``。部分模型会受其他动作影响，
    输出 ``{"start": "C0001", "end": "C0003"}``；两者语义相同，
    因此在进入后续安全校验前统一为一对行号引用。
    """
    if isinstance(item, list) and len(item) == 2:
        return item[0], item[1]
    if isinstance(item, dict) and {"start", "end"} <= item.keys():
        return item["start"], item["end"]
    raise RuntimeError(
        '非法格式；drop_ranges 必须是 ["C0001", "C0003"] '
        '或 {"start":"C0001","end":"C0003"}'
    )


def _heading_target_has_format_evidence(lines: list[str], line_no: int) -> bool:
    """Validate only an LLM-targeted line; never pre-scan the document for titles."""
    text = lines[line_no - 1]
    stripped = text.strip()
    if HEADING_RE.match(text) or OCR_HEADING_MARKER_RE.match(text):
        return True
    if NUMBERED_HEADING_RE.match(stripped):
        return True
    # Unnumbered OCR-lost headings are allowed only when they are short,
    # paragraph-isolated, and do not look like a normal complete sentence.
    before_blank = line_no == 1 or not lines[line_no - 2].strip()
    after_boundary = (
        line_no == len(lines)
        or not lines[line_no].strip()
        or PURE_IMAGE_RE.fullmatch(lines[line_no]) is not None
    )
    return (
        before_blank
        and after_boundary
        and 1 <= len(stripped) <= 40
        and not re.search(r"[。！？!?；;]$", stripped)
    )


def _heading_content_for_relevel(text: str) -> str:
    """Strip an existing normal/OCR heading marker only after the LLM targets it."""
    match = HEADING_RE.match(text)
    if match:
        return match.group(2).strip()
    # `#标题` and full-width `＃ 标题` are candidates, not globally accepted
    # Markdown headings.  Removing them here prevents output such as `### ＃标题`.
    if OCR_HEADING_MARKER_RE.match(text):
        return re.sub(r"^[ \t]{0,3}(?:#{1,6}|＃{1,6})[ \t]*", "", text, count=1).strip()
    return text.strip()


def _paragraph_spans(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return non-empty Markdown paragraphs as (start, end, exact_text)."""
    spans: list[tuple[int, int, str]] = []
    start: int | None = None
    for index in range(len(lines) + 1):
        is_blank = index == len(lines) or not lines[index].strip()
        if not is_blank and start is None:
            start = index
        elif is_blank and start is not None:
            spans.append((start, index, "\n".join(lines[start:index])))
            start = None
    return spans


def _prove_later_exact_repeat(
    document_lines: list[str], global_start: int, global_end: int
) -> dict[str, Any]:
    """Prove a deletion is a whole-paragraph sequence repeated earlier verbatim."""
    spans = _paragraph_spans(document_lines)
    selected: list[int] = []
    for paragraph_index, (start, end, _text) in enumerate(spans):
        if end <= global_start or start >= global_end:
            continue
        if start < global_start or end > global_end:
            raise RuntimeError(
                f"严格重复模式拒绝删除：原始行 {global_start + 1}-{global_end} "
                "切到了段落内部"
            )
        selected.append(paragraph_index)
    if not selected:
        raise RuntimeError("严格重复模式拒绝删除：删除区间不含完整非空段落")
    if selected != list(range(selected[0], selected[-1] + 1)):
        raise RuntimeError("严格重复模式拒绝删除：段落序列不连续")

    candidate = [spans[index][2] for index in selected]
    first = selected[0]
    match_at: int | None = None
    for earlier in range(0, first - len(candidate) + 1):
        if [spans[index][2] for index in range(earlier, earlier + len(candidate))] == candidate:
            match_at = earlier
            break
    if match_at is None:
        raise RuntimeError(
            f"严格重复模式拒绝删除：原始行 {global_start + 1}-{global_end} 的 "
            f"{len(candidate)} 个完整段落在此前没有逐段完全相同的连续副本"
        )
    earlier_start = spans[match_at][0]
    earlier_end = spans[match_at + len(candidate) - 1][1]
    return {
        "later_lines": [global_start + 1, global_end],
        "earlier_lines": [earlier_start + 1, earlier_end],
        "paragraphs": len(candidate),
    }


def _apply_interactive_actions(
    core: str,
    actions: dict[str, Any],
    *,
    document_lines: list[str] | None = None,
    core_start: int | None = None,
    call_id: int | None = None,
) -> str:
    """只允许删除原行、改变标题层级或拼接被 OCR 拆行的标题；正文字符不可改写。"""
    lines = core.split("\n")
    total = len(lines)
    global_fenced = _fenced_line_mask(document_lines) if document_lines is not None else []

    def in_fence(line_no: int) -> bool:
        if document_lines is None or core_start is None:
            return _fenced_line_mask(lines)[line_no - 1]
        global_index = core_start + line_no - 1
        return 0 <= global_index < len(global_fenced) and global_fenced[global_index]

    drop: set[int] = set()
    exact_only = os.getenv("EXACT_REPEAT_ONLY", "").strip().lower() in {"1", "true", "yes"}
    factbase_mode = os.getenv("FACTBASE_MODE", "").strip().lower() in {"1", "true", "yes"}
    no_action_validation = os.getenv("NO_ACTION_VALIDATION", "").strip().lower() in {"1", "true", "yes"}
    proofs: list[dict[str, Any]] = []
    rejected_actions: list[dict[str, Any]] = []

    def reject(kind: str, item: Any, reason: str) -> None:
        """LLM 动作是候选项；单条不安全时跳过，绝不终止整本文档。"""
        rejected_actions.append({"type": kind, "item": item, "reason": reason})
        tqdm.write(f"[跳过动作] {kind} {item!r}：{reason}")

    for item in actions.get("drop_ranges", []):
        try:
            start_ref, end_ref = _drop_range_references(item)
            start, end = _action_line_number(start_ref), _action_line_number(end_ref)
            if not (1 <= start <= end <= total):
                if not no_action_validation:
                    raise RuntimeError(f"越界，核心区共 {total} 行")
                start, end = max(1, start), min(total, end)
                if start > end:
                    raise RuntimeError(f"越界且与核心区无交集（核心区共 {total} 行）")
            if exact_only and not no_action_validation:
                if document_lines is None or core_start is None:
                    raise RuntimeError("严格重复模式缺少原始文档校验上下文")
                proofs.append(_prove_later_exact_repeat(
                    document_lines, core_start + start - 1, core_start + end
                ))
            elif factbase_mode and not no_action_validation and document_lines is not None and core_start is not None:
                spans = _paragraph_spans(document_lines)
                global_start, global_end = core_start + start - 1, core_start + end
                for paragraph_start, paragraph_end, _text in spans:
                    if paragraph_end <= global_start or paragraph_start >= global_end:
                        continue
                    if paragraph_start < global_start or paragraph_end > global_end:
                        raise RuntimeError(
                            f"事实库模式删除切到段落内部（原始行 {global_start + 1}-{global_end}）"
                        )
            drop.update(range(start, end + 1))
        except (TypeError, ValueError, RuntimeError) as exc:
            reject("drop_ranges", item, str(exc))
    for item in actions.get("drop_lines", []):
        try:
            line_no = _action_line_number(item)
            if not 1 <= line_no <= total:
                raise RuntimeError(f"越界，核心区共 {total} 行")
            if exact_only and not no_action_validation:
                if document_lines is None or core_start is None:
                    raise RuntimeError("严格重复模式缺少原始文档校验上下文")
                proofs.append(_prove_later_exact_repeat(
                    document_lines, core_start + line_no - 1, core_start + line_no
                ))
            elif factbase_mode and not no_action_validation:
                raise RuntimeError("事实库模式请使用 drop_ranges 删除完整段落")
            drop.add(line_no)
        except (TypeError, ValueError, RuntimeError) as exc:
            reject("drop_lines", item, str(exc))

    headings: dict[int, int] = {}
    for item in actions.get("set_headings", []):
        try:
            if exact_only:
                raise RuntimeError("严格重复模式禁止修改标题")
            if not isinstance(item, dict) or "line" not in item or "level" not in item:
                raise RuntimeError("非法格式")
            line_no, level = _action_line_number(item["line"]), int(item["level"])
            if not 1 <= line_no <= total or not 0 <= level <= 6:
                raise RuntimeError("越界")
            if level and not lines[line_no - 1].strip():
                raise RuntimeError("不能把空行变成标题")
            if level and PURE_IMAGE_RE.fullmatch(lines[line_no - 1]):
                raise RuntimeError("不能把图片行变成标题")
            if level and in_fence(line_no):
                raise RuntimeError("不能修改代码围栏内文本")
            if level and not no_action_validation and not _heading_target_has_format_evidence(lines, line_no):
                raise RuntimeError("缺少标题格式证据，拒绝把普通正文升级为标题")
            headings[line_no] = level
        except (TypeError, ValueError, RuntimeError) as exc:
            reject("set_headings", item, str(exc))

    joins: dict[int, tuple[int, int]] = {}
    joined_tail_lines: set[int] = set()
    for item in actions.get("join_heading_ranges", []):
        try:
            if exact_only and not no_action_validation:
                raise RuntimeError("严格重复模式禁止拼接标题")
            if not isinstance(item, dict) or not {"start", "end", "level"} <= item.keys():
                raise RuntimeError("非法格式")
            start, end, level = (
                _action_line_number(item["start"]),
                _action_line_number(item["end"]),
                int(item["level"]),
            )
            if not (1 <= start < end <= total and 1 <= level <= 6):
                raise RuntimeError("越界")
            if end - start + 1 > 4:
                raise RuntimeError("最多拼接 4 个连续原始行")
            if not lines[start - 1].strip() or not lines[end - 1].strip():
                raise RuntimeError("join 的 start/end 必须是非空文本")
            if any(in_fence(line_no) for line_no in range(start, end + 1)):
                raise RuntimeError("不能拼接代码围栏内文本")
            if any(line_no in drop for line_no in range(start, end + 1)):
                raise RuntimeError("与删除区间冲突")
            if any(line_no in joined_tail_lines or line_no in joins for line_no in range(start, end + 1)):
                raise RuntimeError("与其他拼接动作重叠")
            existing_level = headings.get(start)
            if existing_level is not None and existing_level != level:
                raise RuntimeError("与 set_headings 层级冲突")
            joins[start] = (end, level)
            joined_tail_lines.update(range(start + 1, end + 1))
        except (TypeError, ValueError, RuntimeError) as exc:
            reject("join_heading_ranges", item, str(exc))

    if factbase_mode:
        for line_no, original in enumerate(lines, 1):
            if in_fence(line_no):
                continue
            if (PURE_SOURCE_RE.fullmatch(original) or PURE_IMAGE_RE.fullmatch(original)
                    or OCR_SYSTEM_NOISE_RE.fullmatch(original)):
                drop.add(line_no)
                if core_start is not None:
                    if PURE_SOURCE_RE.fullmatch(original):
                        noise_type = "source_marker"
                    elif PURE_IMAGE_RE.fullmatch(original):
                        noise_type = "image_markdown"
                    else:
                        noise_type = "ocr_system_noise"
                    _INTERACTIVE_AUTOMATIC_DROPS.append({
                        "line": core_start + line_no,
                        "type": noise_type,
                    })

    if exact_only:
        for proof in proofs:
            proof["call"] = call_id
            _INTERACTIVE_EXACT_PROOFS.append(proof)
            print(
                "<<<EXACT_REPEAT_PROOF "
                f"call={call_id} later={proof['later_lines']} earlier={proof['earlier_lines']} "
                f"paragraphs={proof['paragraphs']}>>>",
                flush=True,
            )

    _INTERACTIVE_ACTION_AUDIT.append({
        "call": call_id,
        "core_start_line": (core_start + 1) if core_start is not None else None,
        "actions": actions,
        "rejected_actions": rejected_actions,
    })
    if _PLAN_CHECKPOINT_PATH is not None:
        checkpoint = {
            "version": 1,
            "source_sha256": _PLAN_CHECKPOINT_SOURCE_SHA256,
            "window_chars": _PLAN_CHECKPOINT_WINDOW_CHARS,
            "actions": list(_INTERACTIVE_ACTION_AUDIT),
        }
        _PLAN_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = _PLAN_CHECKPOINT_PATH.with_suffix(_PLAN_CHECKPOINT_PATH.suffix + ".tmp")
        temporary.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(_PLAN_CHECKPOINT_PATH)

    output: list[str] = []
    for line_no, original in enumerate(lines, 1):
        if line_no in drop:
            continue
        if line_no in joined_tail_lines:
            continue
        if line_no in joins:
            end, level = joins[line_no]
            fragments: list[str] = []
            for source_line in lines[line_no - 1:end]:
                if source_line.strip():
                    fragments.append(_heading_content_for_relevel(source_line))
            output.append("#" * level + " " + "".join(fragments))
            continue
        level = headings.get(line_no)
        if level is None:
            output.append(original)
            continue
        content = _heading_content_for_relevel(original)
        output.append((("#" * level + " ") if level else "") + content)
    value = "\n".join(output).strip()
    # A whole window can legitimately be pure index/CIP/image noise.  Keep an
    # internal sentinel so normal API mode still treats an accidental empty
    # model response as an error.
    if not value:
        return _INTERACTIVE_EMPTY_CORE
    return value + "\n"


@dataclass(frozen=True)
class Line:
    id: str
    text: str


@dataclass(frozen=True)
class Window:
    number: int
    start: int
    end: int


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        value = raw.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, item = (part.strip() for part in value.split("=", 1))
        if len(item) >= 2 and item[0] == item[-1] and item[0] in {"'", '"'}:
            item = item[1:-1]
        if key and key not in os.environ:
            os.environ[key] = item


def setting(primary: str, fallback: str | None = None, default: str | None = None) -> str | None:
    return os.getenv(primary) or (os.getenv(fallback) if fallback else None) or default


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def _request_heading_actions(catalog: str) -> dict[str, Any]:
    """让模型只复核全文标题层级；不提供正文，也不允许任何其他编辑。"""
    base_url = setting(
        "LLM_BASE_URL", "POLITICAL_LLM_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).rstrip("/")
    api_key = setting("LLM_API_KEY", "POLITICAL_LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("请设置 LLM_API_KEY、POLITICAL_LLM_API_KEY 或 DASHSCOPE_API_KEY。")
    prompt = (
        "你正在进行 Markdown 文档的第二遍全局标题层级复审。\n"
        "下面是按原文顺序抽取的全文 Markdown 标题目录；每行包含 H 标签、当前 # 层级和标题文字。\n"
        "当前 # 数量可能是第一遍清洗产生的错误结果，只能作为参考，不能当作正确答案。\n\n"

        "【唯一任务】\n"
        "只检查和修正现有标题的层级逻辑。唯一允许的动作是 set_levels，即调整标题前 # 的数量。\n"
        "禁止删除标题、添加标题、改写标题文字、拼接标题、移动标题或处理任何正文。\n\n"

        "【重点检查】\n"
        "1. 顶层起点：全文标题体系应存在一级标题 #；不要让文档在没有父级的情况下直接从 ##、### 等层级开始。\n"
        "2. 格式稳定：必须比较前后相邻标题和全文重复模式。同一种编号格式、命名形式、结构角色应尽量保持同一层级。\n"
        "   例如连续的‘第一章/第二章’应同级，连续的‘一、/二、/三、’应同级，连续的‘（一）/（二）’应同级。\n"
        "3. 章与节：章是节的父级，章与其内部的节不能同级；同一本书中的章通常彼此同级，节通常彼此同级。\n"
        "4. 文章与内部小节：文章、报告、文件等完整篇目标题是其内部小节的父级，不能与文章内部的‘一、二、三’"
        "或其他小节标题处于同一层级。\n"
        "5. 父子关系优先：父标题必须高于其子标题；同一结构角色保持同级。判断时优先使用编号体系、标题措辞、"
        "相邻关系和全文反复出现的稳定模式。\n"
        "6. 不要机械套固定层级。常见教材可能是‘章 > 节 > 一、 > （一）’，文章型内容可能是"
        "‘上级章节 > 文章标题 > 一、 > （一）’，但最终以当前文档自身反复出现的结构模式为准。\n"
        "7. 只修复有明确依据的层级错误；已经合理的标题不要重复输出，不确定时保持当前层级。\n\n"

        "【输出协议】\n"
        "只返回一个 JSON object，不要 Markdown、代码围栏、解释或分析。\n"
        "只能返回确实需要修改的标题，H 标签必须从目录中原样复制，禁止裸数字、禁止创造 H 标签。\n"
        "格式：{\"set_levels\":[{\"heading\":\"H0001\",\"level\":1}]}\n"
        "如果全部层级合理，返回：{\"set_levels\":[]}\n\n"

        "【全文标题目录】\n" + catalog
    )
    payload = json.dumps({
        "model": setting("LLM_MODEL", "POLITICAL_DEFAULT_MODEL", "qwen3.7-plus"),
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": "你是 Markdown 全局标题层级复核器。你只能调整现有标题的 # 层级，只返回 set_levels JSON。",
            },
            {"role": "user", "content": prompt},
        ],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    timeout = int(setting("LLM_TIMEOUT", "POLITICAL_LLM_TIMEOUT", "120"))
    retries = int(setting("LLM_MAX_RETRIES", "POLITICAL_LLM_MAX_RETRIES", "2"))
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read())
            content = result["choices"][0]["message"]["content"].strip()
            fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.S | re.I)
            if fenced:
                content = fenced.group(1).strip()
            actions = json.loads(content)
            if not isinstance(actions, dict):
                raise ValueError("标题后处理动作必须是 JSON object")
            return actions
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"标题后处理调用失败：HTTP {exc.code}；{detail[:500]}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(min(2 ** attempt, 4))
    raise RuntimeError(f"标题后处理调用失败：{last_error}") from last_error


def postprocess_headings(markdown: str) -> tuple[str, dict[str, Any]]:
    """第二遍全局复审：只调整现有标题的 # 数量，绝不修改标题文字或正文。"""
    lines = markdown.splitlines()
    entries: list[dict[str, Any]] = []
    for line_index, text in enumerate(lines):
        info = heading_info(text)
        if info:
            level, title = info
            entries.append({
                "id": f"H{len(entries) + 1:04d}",
                "line_index": line_index,
                "level": level,
                "title": title,
            })
    if not entries:
        return markdown, {
            "enabled": True,
            "status": "skipped_no_headings",
            "headings": 0,
            "accepted_actions": [],
            "rejected_actions": [],
        }

    catalog = "\n".join(
        f"{entry['id']}\t{'#' * entry['level']} {entry['title']}" for entry in entries
    )
    tqdm.write(f"标题后处理：正在复核 {len(entries)} 个标题")
    try:
        actions = _request_heading_actions(catalog)
    except RuntimeError as exc:
        tqdm.write(f"[跳过标题后处理] {exc}")
        return markdown, {
            "enabled": True,
            "status": "skipped_error",
            "headings": len(entries),
            "error": str(exc),
            "accepted_actions": [],
            "rejected_actions": [],
        }

    by_id = {entry["id"]: entry for entry in entries}
    new_levels: dict[str, int] = {}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    def reject(kind: str, item: Any, reason: str) -> None:
        rejected.append({"type": kind, "item": item, "reason": reason})
        tqdm.write(f"[跳过标题后处理动作] {kind} {item!r}：{reason}")

    unknown_fields = sorted(set(actions) - {"set_levels"})
    for field in unknown_fields:
        reject("unknown_field", field, "标题复审只允许 set_levels")
    set_level_items = actions.get("set_levels", [])
    if not isinstance(set_level_items, list):
        reject("set_levels", set_level_items, "必须是数组")
        set_level_items = []

    for item in set_level_items:
        try:
            if not isinstance(item, dict) or set(item) != {"heading", "level"}:
                raise ValueError("格式必须只有 heading 和 level")
            heading_id = item["heading"]
            if not isinstance(heading_id, str) or not HEADING_REFERENCE_RE.fullmatch(heading_id):
                raise ValueError("heading 必须使用 H 标签，例如 H0001")
            if heading_id not in by_id:
                raise ValueError("H 标签不存在")
            level = int(item["level"])
            if not 1 <= level <= 6:
                raise ValueError("level 必须为 1 到 6")
            if level == by_id[heading_id]["level"]:
                continue
            new_levels[heading_id] = level
            accepted.append({"type": "set_level", "heading": heading_id, "level": level})
        except (TypeError, ValueError) as exc:
            reject("set_levels", item, str(exc))
    for heading_id, entry in by_id.items():
        line_index = entry["line_index"]
        if heading_id in new_levels:
            lines[line_index] = "#" * new_levels[heading_id] + " " + entry["title"]

    output = "\n".join(lines).rstrip("\n") + "\n"
    return output, {
        "enabled": True,
        "status": "completed",
        "headings": len(entries),
        "accepted_actions": accepted,
        "rejected_actions": rejected,
    }


def call_llm(
    system: str,
    prompt: str,
    *,
    document_lines: list[str] | None = None,
    core_start: int | None = None,
) -> str:
    global _INTERACTIVE_CALL_NO, _PLAN_CURSOR
    if _PLAN_ACTIONS is not None:
        _PLAN_CURSOR += 1
        call_id = _PLAN_CURSOR
        if call_id > len(_PLAN_ACTIONS):
            raise RuntimeError(f"plan.json 动作不足：执行器请求第 {call_id} 个窗口")
        record = _PLAN_ACTIONS[call_id - 1]
        expected_start = record.get("core_start_line")
        actual_start = (core_start + 1) if core_start is not None else None
        if expected_start is not None and int(expected_start) != actual_start:
            raise RuntimeError(
                f"plan.json 窗口错位：第 {call_id} 窗计划起始行 {expected_start}，实际 {actual_start}"
            )
        actions = record.get("actions")
        if not isinstance(actions, dict):
            raise RuntimeError(f"plan.json 第 {call_id} 窗 actions 必须是 JSON object")
        core = _interactive_core(prompt)
        value = _apply_interactive_actions(
            core,
            actions,
            document_lines=document_lines,
            core_start=core_start,
            call_id=call_id,
        )
        print(f"[PLAN] window={call_id} core_start_line={actual_start} accepted", flush=True)
        return value

    if os.getenv("LLM_INTERACTIVE", "").strip().lower() in {"1", "true", "yes"}:
        _INTERACTIVE_CALL_NO += 1
        call_id = _INTERACTIVE_CALL_NO
        core = _interactive_core(prompt)
        core_lines = core.split("\n")
        end_marker = f"<<<LLM_RESPONSE_END:{call_id}>>>"
        compact_debug = os.getenv("LLM_INTERACTIVE_COMPACT", "").strip().lower() in {
            "1", "true", "yes"
        }
        print(f"<<<LLM_PROMPT_BEGIN:{call_id}>>>")
        print(f"[SYSTEM]\n{system}\n[/SYSTEM]")
        if compact_debug:
            print(
                "[USER_META] 事实库 action-only 模拟 API；核心区如下，"
                "前后只读上下文已由调用器内部保留。[/USER_META]"
            )
        else:
            print(f"[USER]\n{prompt}\n[/USER]")
        print(f"<<<LLM_PROMPT_END:{call_id}>>>")
        print(f"<<<INTERACTIVE_CORE_LINES:{call_id}>>>")
        for line_no, text in enumerate(core_lines, 1):
            print(f"C{line_no:04d}\t{text}")
        print(f"<<<INTERACTIVE_CORE_LINES_END:{call_id}>>>")
        print(
            f"<<<LLM_ACTIONS_WAIT:{call_id}>>> 输入 JSON 动作；仅允许 "
            "drop_ranges/drop_lines/set_headings/join_heading_ranges；"
            f"结束时单独输入 {end_marker}",
            flush=True,
        )
        while True:
            response: list[str] = []
            while True:
                raw = sys.stdin.readline()
                if raw == "":
                    raise RuntimeError(f"交互式 LLM 调用 {call_id} 在结束标记前遇到 EOF")
                if raw.rstrip("\r\n") == end_marker:
                    break
                response.append(raw)
            raw_actions = "".join(response).strip()
            try:
                if not raw_actions:
                    raise RuntimeError(f"交互式 LLM 调用 {call_id} 返回空响应")
                actions = json.loads(raw_actions)
                if not isinstance(actions, dict):
                    raise RuntimeError(f"交互式 LLM 调用 {call_id} 动作必须是 JSON object")
                value = _apply_interactive_actions(
                    core,
                    actions,
                    document_lines=document_lines,
                    core_start=core_start,
                    call_id=call_id,
                )
                break
            except (json.JSONDecodeError, RuntimeError) as exc:
                print(
                    f"<<<LLM_ACTIONS_REJECTED:{call_id}>>> {exc}\n"
                    f"<<<LLM_ACTIONS_RETRY:{call_id}>>> 请修正 JSON 动作并再次以 {end_marker} 结束",
                    flush=True,
                )
        print(
            f"<<<LLM_ACTIONS_ACCEPTED:{call_id} core_lines={len(core_lines)} output_chars={len(value)}>>>",
            flush=True,
        )
        return value

    _INTERACTIVE_CALL_NO += 1
    call_id = _INTERACTIVE_CALL_NO
    base_url = setting("LLM_BASE_URL", "POLITICAL_LLM_BASE_URL",
                       "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    api_key = setting("LLM_API_KEY", "POLITICAL_LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("请设置 LLM_API_KEY、POLITICAL_LLM_API_KEY 或 DASHSCOPE_API_KEY。")
    factbase_mode = env_flag("FACTBASE_MODE")
    if factbase_mode:
        empty_schema = (
            '{"drop_ranges":[],'
            '"set_headings":[],'
            '"join_heading_ranges":[]}'
        )
        deletion_rule = (
            "drop_ranges 仅删除明确噪声，并且必须覆盖完整 Markdown 段落；"
            "段落指两个空行边界之间连续的全部非空行。禁止输出 drop_lines。"
        )
    else:
        empty_schema = (
            '{"drop_ranges":[],'
            '"drop_lines":[],'
            '"set_headings":[],'
            '"join_heading_ranges":[]}'
        )
        deletion_rule = "非事实库模式可使用 drop_ranges 或 drop_lines。"

    action_prompt = prompt + (
        "\n\n【动作输出规则】"
        "默认不执行任何动作。绝大多数正常正文应该保持原样；"
        "不要为了使用某个字段而寻找修改。"

        "只能复制【核心区】实际存在的 C 标签；"
        "禁止自行计算、递增或创造 C 标签。"

        + deletion_rule +

        "drop_ranges 的唯一推荐格式为 "
        '{"drop_ranges":[["C0001","C0003"]]}；'
        "每个删除区间必须是含两个 C 标签的数组，单行删除也必须写成 "
        '["C0001","C0001"]。'
        '禁止输出 {"start":"C0001","end":"C0003"} 这种对象形式。'

        "set_headings 格式为 "
        '{"line":"C0001","level":1到6}；'
        "仅用于单独一行已经是完整标题、只需调整标题层级的情况。"
        "空行不得作为 set_headings 目标。"

        "join_heading_ranges 格式为 "
        '{"start":"C0001","end":"C0003","level":1到6}；'
        "仅用于同一个标题被 OCR 拆成多个文本行。"
        "start 和 end 必须非空；中间允许空行；最多覆盖4行。"
        "如果只是调整单行标题层级，必须使用 set_headings。"

        "不确定时不执行动作。"

        "\n\n【输出格式】只返回 JSON object，不要 Markdown 或解释："
        + empty_schema
    )
    payload = json.dumps({
        "model": setting("LLM_MODEL", "POLITICAL_DEFAULT_MODEL", "qwen3.7-plus"),
        "temperature": 0,
        "messages": [{"role": "system", "content": "你是 Markdown 清理和排版器，只返回 JSON 动作。"},
                     {"role": "user", "content": action_prompt}],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    timeout = int(setting("LLM_TIMEOUT", "POLITICAL_LLM_TIMEOUT", "120"))
    retries = int(setting("LLM_MAX_RETRIES", "POLITICAL_LLM_MAX_RETRIES", "2"))
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
                content = payload["choices"][0]["message"]["content"].strip()
                fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.S | re.I)
                if fenced:
                    content = fenced.group(1).strip()
                actions = json.loads(content)
                if not isinstance(actions, dict):
                    raise ValueError("LLM 动作必须是 JSON object")
                core = _interactive_core(prompt)
                return _apply_interactive_actions(
                    core,
                    actions,
                    document_lines=document_lines,
                    core_start=core_start,
                    call_id=call_id,
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"LLM 调用失败：HTTP {exc.code}；{detail[:500]}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(min(2 ** attempt, 4))
    # 格式错误是模型某一窗口的候选动作无效，不是原始 Markdown 的错误。
    # 降级为空动作：保留该窗口正文，同时仍执行安全的确定性噪声删除。
    if isinstance(last_error, (KeyError, ValueError, json.JSONDecodeError)):
        reason = f"模型动作 JSON 无效，按空动作继续：{last_error}"
        tqdm.write(f"[跳过窗口动作] {reason}")
        value = _apply_interactive_actions(
            _interactive_core(prompt),
            {},
            document_lines=document_lines,
            core_start=core_start,
            call_id=call_id,
        )
        _INTERACTIVE_ACTION_AUDIT[-1]["rejected_actions"].append({
            "type": "llm_response",
            "item": None,
            "reason": reason,
        })
        return value
    raise RuntimeError(f"LLM 调用失败：{last_error}") from last_error


def call_straight_llm(system: str, prompt: str) -> str:
    """激进模式：模型直接返回清理后的核心 Markdown，不解析动作。"""
    if env_flag("LLM_INTERACTIVE"):
        raise RuntimeError("--straight 暂不支持 LLM_INTERACTIVE，请使用正常 API 调用")
    base_url = setting(
        "LLM_BASE_URL", "POLITICAL_LLM_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).rstrip("/")
    api_key = setting("LLM_API_KEY", "POLITICAL_LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("请设置 LLM_API_KEY、POLITICAL_LLM_API_KEY 或 DASHSCOPE_API_KEY。")
    payload = json.dumps({
        "model": setting("LLM_MODEL", "POLITICAL_DEFAULT_MODEL", "qwen3.7-plus"),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    timeout = int(setting("LLM_TIMEOUT", "POLITICAL_LLM_TIMEOUT", "120"))
    retries = int(setting("LLM_MAX_RETRIES", "POLITICAL_LLM_MAX_RETRIES", "2"))
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            return payload["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"Straight LLM 调用失败：HTTP {exc.code}；{detail[:500]}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(min(2 ** attempt, 4))
    raise RuntimeError(f"Straight LLM 调用失败：{last_error}") from last_error


def heading_info(text: str) -> tuple[int, str] | None:
    match = HEADING_RE.match(text)
    return (len(match.group(1)), match.group(2).strip()) if match else None


def make_lines(markdown: str) -> list[Line]:
    return [Line(f"L{index:06d}", text) for index, text in enumerate(markdown.splitlines(), 1)]


def _size(lines: list[Line], start: int, end: int) -> int:
    return sum(len(line.text) + 1 for line in lines[start:end])


def _pack(lines: list[Line], ranges: list[tuple[int, int]], target: int) -> list[tuple[int, int]]:
    output: list[tuple[int, int]] = []
    start = end = -1
    for left, right in ranges:
        if start < 0:
            start, end = left, right
        elif _size(lines, start, end) + _size(lines, left, right) <= int(target * 1.2):
            end = right
        else:
            output.append((start, end))
            start, end = left, right
    if start >= 0:
        output.append((start, end))
    return output


def _split_large_block(
    lines: list[Line], start: int, end: int, target: int, fenced: list[bool]
) -> list[tuple[int, int]]:
    """超大 #/## 块优先在 ### 标题前切，否则只在完整段落边界切。"""
    starts = [start] + [index for index in range(start + 1, end)
                        if not fenced[index]
                        and (info := heading_info(lines[index].text)) and info[0] >= 3] + [end]
    if len(starts) > 2:
        return _pack(lines, list(zip(starts, starts[1:])), target)
    result, cursor = [], start
    while cursor < end:
        candidate = cursor + 1
        while candidate < end and _size(lines, cursor, candidate) < target:
            candidate += 1
        if candidate >= end:
            result.append((cursor, end))
            break
        split = candidate
        while split > cursor + 1 and lines[split - 1].text.strip():
            split -= 1
        if split <= cursor + 1:
            split = candidate
            while split < end and lines[split - 1].text.strip():
                split += 1
        result.append((cursor, min(split, end)))
        cursor = min(split, end)
    return result


def make_windows(lines: list[Line], target_chars: int = 10000) -> list[Window]:
    """按 #/## 完整块拼装窗口；# 层级错误不会影响后续模型修复。"""
    if not lines:
        return []
    fenced = _fenced_line_mask([line.text for line in lines])
    starts = [0] + [index for index in range(1, len(lines))
                    if not fenced[index]
                    and (info := heading_info(lines[index].text)) and info[0] <= 2] + [len(lines)]
    blocks: list[tuple[int, int]] = []
    for left, right in zip(starts, starts[1:]):
        if _size(lines, left, right) > int(target_chars * 1.2):
            blocks.extend(_split_large_block(lines, left, right, target_chars, fenced))
        else:
            blocks.append((left, right))
    return [Window(number, left, right) for number, (left, right) in enumerate(_pack(lines, blocks, target_chars), 1)]


def pending_headings(
    lines: list[Line], window: Window, limit: int = PENDING_HEADING_LIMIT
) -> str:
    """提供少量后续标题文本；不暴露全文行号，防止与核心区 C 标签冲突。"""
    headings = [
        line.text
        for line in lines[window.start:]
        if heading_info(line.text)
    ][:limit]
    return "\n".join(headings) or "（没有可参考的待处理标题）"


def confirmed_headings(cleaned_parts: list[str], limit: int = 20) -> str:
    """前序窗口已应用后的标题，仅作为当前窗口的跨窗口结构锚点。"""
    headings = [line for part in cleaned_parts for line in part.splitlines() if heading_info(line)]
    return "\n".join(headings[-limit:]) or "（无：这是首个窗口或前序窗口没有确认标题）"


def _nearby_context(lines: list[Line], start: int, end: int, chars: int) -> tuple[str, str]:
    before: list[str] = []
    used = 0
    for line in reversed(lines[:start]):
        if used + len(line.text) + 1 > chars:
            break
        before.append(line.text)
        used += len(line.text) + 1
    after: list[str] = []
    used = 0
    for line in lines[end:]:
        if used + len(line.text) + 1 > chars:
            break
        after.append(line.text)
        used += len(line.text) + 1
    return "\n".join(reversed(before)), "\n".join(after)


def context_budget(window_chars: int) -> int:
    """前后只读上下文各取窗口长度的 1/10，且不少于 1200 字。"""
    return max(1200, window_chars // 10)


def cleaning_prompt(
    lines: list[Line],
    window: Window,
    cleaned_parts: list[str],
    *,
    context_chars: int | None = None,
) -> str:
    # 独立调用时按实际窗口长度计算；主流程显式传入配置窗口长度的 1/10。
    if context_chars is None:
        context_chars = context_budget(_size(lines, window.start, window.end))
    before, after = _nearby_context(lines, window.start, window.end, chars=context_chars)
    # 给模型稳定、可见的本地行号，避免它因空行或只读上下文产生 off-by-one。
    core = "\n".join(
        f"C{line_no:04d}\t{line.text if line.text.strip() else '<BLANK>'}"
        for line_no, line in enumerate(lines[window.start:window.end], 1)
    )
    if os.getenv("EXACT_REPEAT_ONLY", "").strip().lower() in {"1", "true", "yes"}:
        rules = (
            "严格重复清理模式：\n"
            "1. 只能删除在原始文件更早位置已经逐段、逐字完全相同出现过的后出现连续段落区间。\n"
            "2. 独有段落一律保留；目录、索引、注释、图片、出版信息等即使像噪声，只要不是上述完全重复，也必须保留。\n"
            "3. 不得改写正文，不得添加/调整 Markdown 标题，不得移动内容。\n"
            "4. 不确定时不删除。执行器会对每个删除区间再次做原始文件级硬校验。\n\n"
        )
    elif os.getenv("FACTBASE_MODE", "").strip().lower() in {"1", "true", "yes"}:
        rules = (
            "事实库清理模式：目标是保留可用于抽取事实的主体内容，并理顺标题。\n"
            "1. 正文字符绝对不能改写、纠错、概括或移动。\n"
            "2. KEEP：包含可落到主体/时间/地点/事件/制度/政策措施/数量结果/明确立场的实质内容。\n"
            "   讲话中的明确政策判断、主张和行动要求也有事实价值，应保留。\n"
            "3. DROP：目录/索引/出版信息/OCR碎片/纯导航\n"
            "   空泛过渡而缺乏可抽取事实的内容。宁可保留边界项，不要为了变短而删。\n"
            "4. 删除必须按完整段落区间进行；只能输出 drop_ranges，绝不能输出 drop_lines。"
            "不要从有事实的段落里摘句删除。\n"
            "5. 标题只做结构修复。教材通常 #=章、##=节、###=一/二/三、####=（一）/（二）；"
            "文章/文件按上下文判断。即使原行没有 #，明确编号标题也可 set_headings；日期、署名、称呼不是标题往往不是标题，也要根据实际判断。\n"
            "6. 后出现的大段逐字重复可删除；独有的事实段落必须保留。\n\n"
        )
    else:
        rules = (
            "规则：\n"
            "1. 正文原文不得改写、概括、补写或移动。\n"
            "2. 删除明确的 OCR/出版噪声：目录、页码、图片链接、版权出版信息、注释、索引、附录、明显重复标题。\n"
            "   相邻两条表达同一篇文章/报告的标题时，删除较短或 OCR 残缺的一条，只保留信息更完整的一条。\n"
            "3. 只为明显的章节、文章或编号项添加/调整 #；日期、署名、称呼、出处、导语不是标题。\n"
            "4. 仅当相邻编号提供明确证据时补纯结构标题（一、二、三）；不确定时不修改。\n\n"
        )
    return (
        "清理当前 Markdown 核心区，为后续文本分块准备干净数据。\n"
        + rules +
        "只处理【核心区】。其他内容只用于理解结构，不得对其输出动作。\n\n"
        "【已确认标题】\n" + confirmed_headings(cleaned_parts) + "\n\n"
        f"【待处理标题（最多 {PENDING_HEADING_LIMIT} 条）】\n"
        + pending_headings(lines, window) + "\n\n"
        "待处理标题仅用于理解整篇文档的标题层级。"
        "不要因为标题出现在该列表中就修改它；"
        "只有核心区存在明确结构错误时才输出标题动作。"
        "动作只能引用【核心区】的 C 标签；不确定时保持原样。\n\n"
        "【前置只读上下文】\n" + before + "\n\n"
        "【核心区】\n" + core + "\n\n"
        "【后置只读上下文】\n" + after
    )


def markdown_code_block(content: str) -> str:
    """用不与嵌入 Markdown 冲突的反引号围栏包裹提示词中的原文。"""
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}markdown\n{content}\n{fence}"


def straight_cleaning_prompt(
    lines: list[Line],
    window: Window,
    cleaned_parts: list[str],
    *,
    context_chars: int,
) -> str:
    """不标行号的直接清洗提示词；模型只返回当前核心区的 Markdown。"""
    before, after = _nearby_context(lines, window.start, window.end, chars=context_chars)
    core = "\n".join(line.text for line in lines[window.start:window.end])
    if env_flag("FACTBASE_MODE"):
        scope_rule = "保留可抽取的事实性主体内容；删除明确的目录、索引、出版信息、图片链接、OCR 碎片和纯导航。"
    else:
        scope_rule = "删除明确的 OCR 与出版噪声，修复明显错误的 Markdown 标题结构。"
    return (
        "# Markdown 清理和排版\n\n"
        "请根据标题层级逻辑与正文内容，清理并排版 `核心区`，"
        "直接返回处理后的完整核心区 Markdown。\n\n"
        "## 规则\n\n"
        "- 不得解释、不得输出代码围栏、不得输出前后上下文。\n"
        "- 正文不得改写、概括、补写或移动；不得把正文压缩成目录或提纲。\n"
        "- 排版只包括：根据正文内容与编号关系调整现有标题层级、整理段落边界，"
        "或拼接被 OCR 拆开的同一标题。\n"
        f"- {scope_rule}\n"
        "- 日期、署名、称呼、出处不是标题；不确定时保留原文。\n\n"
        "## 已确认标题\n\n" + markdown_code_block(confirmed_headings(cleaned_parts)) + "\n\n"
        f"## 待处理标题（最多 {PENDING_HEADING_LIMIT} 条）\n\n"
        + markdown_code_block(pending_headings(lines, window)) + "\n\n"
        "## 前置只读上下文\n\n" + markdown_code_block(before) + "\n\n"
        "## 核心区\n\n" + markdown_code_block(core) + "\n\n"
        "## 后置只读上下文\n\n" + markdown_code_block(after)
    )


def normalize_model_markdown(text: str) -> str:
    value = text.strip()
    if value == _INTERACTIVE_EMPTY_CORE:
        return ""
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*(.*?)\s*```", value, re.S | re.I)
    if fenced:
        value = fenced.group(1).strip()
    if not value:
        raise RuntimeError("模型返回空核心区，安全停止")
    return re.sub(r"\n{3,}", "\n\n", value).strip() + "\n"


def clean_markdown(
    markdown: str,
    target_chars: int = 10000,
    *,
    preprocess: bool = True,
    heading_postprocess: bool = False,
    straight: bool = False,
) -> tuple[str, dict[str, Any]]:
    global _INTERACTIVE_EXACT_PROOFS, _INTERACTIVE_ACTION_AUDIT, _INTERACTIVE_AUTOMATIC_DROPS, _PLAN_CURSOR
    _INTERACTIVE_EXACT_PROOFS = []
    _INTERACTIVE_ACTION_AUDIT = []
    _INTERACTIVE_AUTOMATIC_DROPS = []
    _PLAN_CURSOR = 0
    raw_input_characters = len(markdown)
    preprocessing: dict[str, int] = {}
    if preprocess:
        markdown, preprocessing = preprocess_markdown(markdown)
    lines = make_lines(markdown)
    document_lines = [line.text for line in lines]
    windows = make_windows(lines, target_chars)
    cleaned_parts: list[str] = []
    with tqdm(windows, desc="LLM 清洗", unit="窗口", dynamic_ncols=True) as progress:
        for window in progress:
            progress.set_postfix_str(f"原始行 {window.start + 1}-{window.end}")
            if straight:
                response = call_straight_llm(
                    "你是 Markdown 清理和排版器。根据标题层级逻辑与正文内容处理 Markdown；"
                    "只返回处理后的 Markdown。",
                    straight_cleaning_prompt(
                        lines,
                        window,
                        cleaned_parts,
                        context_chars=context_budget(target_chars),
                    ),
                )
            else:
                response = call_llm(
                    "你是 Markdown 清理和排版器，只返回 Markdown。",
                    cleaning_prompt(
                        lines,
                        window,
                        cleaned_parts,
                        context_chars=context_budget(target_chars),
                    ),
                    document_lines=document_lines,
                    core_start=window.start,
                )
            cleaned_parts.append(normalize_model_markdown(response))
    if _PLAN_ACTIONS is not None and _PLAN_CURSOR != len(_PLAN_ACTIONS):
        raise RuntimeError(
            f"plan.json 动作数与窗口数不一致：已使用 {_PLAN_CURSOR}，计划 {len(_PLAN_ACTIONS)}"
        )
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_parts)).strip() + "\n"
    heading_postprocess_report: dict[str, Any] = {"enabled": False, "status": "disabled"}
    if heading_postprocess:
        cleaned, heading_postprocess_report = postprocess_headings(cleaned)
    body_provenance = "SKIPPED_STRAIGHT" if straight else "PASS"
    if not straight:
        # Independent body-character provenance check. Heading markers are ignored;
        # every remaining nonblank line must still come from the raw file in order.
        def body(line: str) -> str:
            match = HEADING_RE.match(line)
            return match.group(2) if match else line
        source_stream = [body(line.text).strip() for line in lines if line.text.strip()]
        output_stream = [body(line) for line in cleaned.splitlines() if line.strip()]
        cursor = 0
        for value in output_stream:
            value = value.strip()
            found_end: int | None = None
            scan = cursor
            while scan < len(source_stream) and found_end is None:
                combined = ""
                for end in range(scan, min(scan + 4, len(source_stream))):
                    combined += source_stream[end]
                    if combined == value:
                        found_end = end + 1
                        break
                    if len(combined) >= len(value):
                        break
                scan += 1
            if found_end is None:
                raise RuntimeError(
                    "正文来源校验失败：输出出现无法按原顺序追溯到原文件的非空行；"
                    f"cursor={cursor} value={value!r}"
                )
            cursor = found_end

    return cleaned, {"windows": len(windows), "input_characters": raw_input_characters,
                     "preprocessed_characters": len(markdown),
                     "output_characters": len(cleaned),
                     "delete_ratio": round(max(0.0, (len(markdown) - len(cleaned)) / len(markdown)), 6),
                     "mode": "straight" if straight else "action_only",
                     "body_provenance": body_provenance,
                     "exact_repeat_proofs": list(_INTERACTIVE_EXACT_PROOFS),
                     "automatic_drops": list(_INTERACTIVE_AUTOMATIC_DROPS),
                     "interactive_actions": list(_INTERACTIVE_ACTION_AUDIT),
                     "heading_postprocess": heading_postprocess_report,
                     "preprocessing": preprocessing}


def batch_clean_dataset(
    dataset_dir: Path,
    clean_dir: Path,
    target_chars: int = 10000,
    *,
    preprocess: bool = True,
    heading_postprocess: bool = False,
    straight: bool = False,
) -> dict[str, Any]:
    if not dataset_dir.is_dir():
        raise ValueError(f"批处理输入不是目录：{dataset_dir}")
    clean_dir.mkdir(parents=True, exist_ok=True)
    sources = sorted(path for path in dataset_dir.rglob("*") if path.is_file()
                     and path.suffix.lower() in {".md", ".markdown"}
                     and clean_dir.resolve() not in path.resolve().parents)
    report: dict[str, Any] = {"found": len(sources), "processed": 0, "skipped": 0, "failed": 0, "failures": []}
    for source in sources:
        target = clean_dir / source.relative_to(dataset_dir)
        if target.exists():
            report["skipped"] += 1
            print(f"[跳过] {source.name}")
            continue
        try:
            cleaned, _ = clean_markdown(
                source.read_text(encoding="utf-8-sig"),
                target_chars,
                preprocess=preprocess,
                heading_postprocess=heading_postprocess,
                straight=straight,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(cleaned, encoding="utf-8")
            report["processed"] += 1
            print(f"[完成] {source.name}")
        except Exception as exc:
            report["failed"] += 1
            report["failures"].append({"file": str(source), "error": str(exc)})
            print(f"[失败] {source.name}：{exc}")
    return report


def main() -> None:
    global _PLAN_ACTIONS, _PLAN_CHECKPOINT_PATH
    global _PLAN_CHECKPOINT_SOURCE_SHA256, _PLAN_CHECKPOINT_WINDOW_CHARS
    parser = argparse.ArgumentParser(description="按完整标题块滑动清理 Markdown")
    parser.add_argument("input", type=Path)
    parser.add_argument("--out-dir", type=Path, help="单文件结果目录，默认 result/<文件名>")
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--clean-dir", type=Path, default=Path("result/clean"))
    parser.add_argument("--window-chars", type=int, default=10000)
    parser.add_argument("--plan", type=Path, help="只执行显式 plan.json，不调用 LLM")
    parser.add_argument("--plan-out", type=Path, help="把本次窗口动作固化为可重放 plan.json")
    parser.add_argument(
        "--factbase-mode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="启用事实库清洗模式；可用 --no-factbase-mode 显式覆盖 .env 配置",
    )
    parser.add_argument(
        "--no-action-validation",
        action="store_true",
        help="跳过 LLM 动作的语义安全校验；仍保留行号边界和 Markdown 基本结构保护",
    )
    parser.add_argument(
        "--heading-postprocess",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="清洗后复核完整标题目录；可用 --no-heading-postprocess 覆盖 .env 配置",
    )
    parser.add_argument(
        "--straight",
        action="store_true",
        help="激进模式：LLM 直接返回清理后的 Markdown，不使用行号动作或本地动作校验",
    )
    parser.add_argument("--no-preprocess", action="store_true", help="关闭 LLM 前的确定性格式预处理")
    args = parser.parse_args()
    load_dotenv(Path.cwd() / ".env")
    if args.factbase_mode is not None:
        os.environ["FACTBASE_MODE"] = "1" if args.factbase_mode else "0"
    if args.no_action_validation:
        os.environ["NO_ACTION_VALIDATION"] = "1"
    heading_postprocess = (
        args.heading_postprocess
        if args.heading_postprocess is not None
        else env_flag("HEADING_POSTPROCESS")
    )
    if args.window_chars < 1000:
        parser.error("--window-chars 不能小于 1000")
    if heading_postprocess and (args.plan or args.plan_out):
        parser.error("--heading-postprocess 暂不与 --plan/--plan-out 组合使用")
    if args.straight and (args.plan or args.plan_out or heading_postprocess):
        parser.error("--straight 暂不与 --plan、--plan-out 或 --heading-postprocess 组合使用")
    if args.batch:
        report = batch_clean_dataset(
            args.input,
            args.clean_dir,
            args.window_chars,
            preprocess=not args.no_preprocess,
            heading_postprocess=heading_postprocess,
            straight=args.straight,
        )
        print(f"批处理完成：发现 {report['found']}，完成 {report['processed']}，跳过 {report['skipped']}，失败 {report['failed']}")
        return
    raw_bytes = args.input.read_bytes()
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if args.plan:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if plan.get("version") != 1 or not isinstance(plan.get("actions"), list):
            raise RuntimeError("plan.json 格式无效：需要 version=1 和 actions 数组")
        if plan.get("source_sha256") != raw_sha256:
            raise RuntimeError("plan.json 与原始文件 SHA-256 不匹配，拒绝生成输出")
        if int(plan.get("window_chars", -1)) != args.window_chars:
            raise RuntimeError("plan.json 的 window_chars 与命令参数不匹配")
        _PLAN_ACTIONS = plan["actions"]
    elif args.plan_out:
        _PLAN_CHECKPOINT_PATH = args.plan_out
        _PLAN_CHECKPOINT_SOURCE_SHA256 = raw_sha256
        _PLAN_CHECKPOINT_WINDOW_CHARS = args.window_chars
    out_dir = args.out_dir or Path("result") / args.input.stem
    cleaned, report = clean_markdown(
        raw_bytes.decode("utf-8-sig"),
        args.window_chars,
        preprocess=not args.no_preprocess,
        heading_postprocess=heading_postprocess,
        straight=args.straight,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "clean.md").write_text(cleaned, encoding="utf-8")
    (out_dir / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.plan_out:
        plan = {
            "version": 1,
            "source_sha256": raw_sha256,
            "window_chars": args.window_chars,
            "actions": report["interactive_actions"],
        }
        args.plan_out.parent.mkdir(parents=True, exist_ok=True)
        args.plan_out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成：{out_dir / 'clean.md'}")


if __name__ == "__main__":
    main()
