#!/usr/bin/env python3
"""Code-only QA for a fact-base Markdown output."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#{1,6})?[ \t]*$")
SOURCE_RE = re.compile(
    r"^\s*<!--\s*(?:(?:source|pdf)\s*pages?|page|来源页)\s*(?::|=)?\s*"
    r"\d+(?:\s*[-–—,，]\s*\d+)*\s*-->\s*$",
    re.I,
)
IMAGE_RE = re.compile(
    r"^\s*(?:"
    r"!\[[^]\n]*\]\(\s*<?[^\s()<>]+>?(?:\s+[\"'][^\"'\n]*[\"'])?\s*\)"
    r"|!\[[^]\n]*\]\[[^]\n]*\]"
    r"|<img\b[^>]*?/?>"
    r")\s*$",
    re.I,
)
OCR_RE = re.compile(
    r"^\s*(?:The Ground Truth image displays|OCR result must ignore|OCR has hallucinated text)"
    r"\b[^\n]{0,1000}\s*$",
    re.I,
)
COURTESY_RE = re.compile(
    r"^\s*(?:"
    r"(?:尊敬的|敬爱的|亲爱的)[^。！？!?：:]{1,120}[，,!！：:]?|"
    r"各位[^。！？!?：:]{1,120}[，,!！：:]|"
    r"(?:女士们|先生们|同志们|朋友们)(?:[、，,\s]*(?:女士们|先生们|同志们|朋友们))*[！!：:，,。.]?|"
    r"大家好[！!。.]?|谢谢(?:大家)?[！!。.]?"
    r")\s*$"
)
NOISE_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:目录|内容提要|版权页|出版说明|编者说明|前言|序言|代序|后记|编后记|"
    r"译后记|再版说明|索引|人物索引|名词索引|参考文献|注释|附录|学习要点|课后思考)\s*$"
)
TOC_DOTS_RE = re.compile(
    r"(?:…{2,}|\.{4,}|·{4,}|_{4,}|-{4,}|—{2,})\s*"
    r"(?:\d+|[IVXLCDM]+|[〇零一二三四五六七八九十百]+)\s*$",
    re.I,
)
CHAPTER_RE = re.compile(r"^第(?:[〇零一二三四五六七八九十百两]+|\d+)章(?:\s|$)")
SECTION_RE = re.compile(r"^第(?:[〇零一二三四五六七八九十百两]+|\d+)节(?:\s|$)")
LEVEL3_RE = re.compile(r"^[一二三四五六七八九十百]+[、．.]\s*\S")
LEVEL4_RE = re.compile(r"^[（(](?:[一二三四五六七八九十百]+|\d+)[）)]\s*\S")
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(?:[^\n]*)$")


def fenced_line_mask(lines: list[str]) -> list[bool]:
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


def body(line: str) -> str:
    match = HEADING_RE.match(line)
    return (match.group(2) if match else line).strip()


def prove_body_provenance(source_lines: list[str], output_lines: list[str]) -> bool:
    source = [body(line) for line in source_lines if line.strip()]
    output = [body(line) for line in output_lines if line.strip()]
    cursor = 0
    for value in output:
        found_end: int | None = None
        scan = cursor
        while scan < len(source) and found_end is None:
            combined = ""
            for end in range(scan, min(scan + 4, len(source))):
                combined += source[end]
                if combined == value:
                    found_end = end + 1
                    break
                if len(combined) >= len(value):
                    break
            scan += 1
        if found_end is None:
            return False
        cursor = found_end
    return True


def paragraph_duplicates(lines: list[str], min_chars: int) -> list[dict[str, int]]:
    seen: dict[str, int] = {}
    duplicates: list[dict[str, int]] = []
    current: list[str] = []
    start = 0
    for line_no, line in enumerate(lines + [""], 1):
        if line.strip():
            if not current:
                start = line_no
            current.append(line.strip())
            continue
        if not current:
            continue
        text = "\n".join(current)
        if len(text) >= min_chars and text in seen:
            duplicates.append({"earlier": seen[text], "later": start, "chars": len(text)})
        else:
            seen.setdefault(text, start)
        current = []
    return duplicates


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate fact-base Markdown without editing it")
    parser.add_argument("source", type=Path)
    parser.add_argument("clean", type=Path)
    parser.add_argument("--textbook", action="store_true")
    parser.add_argument("--min-duplicate-chars", type=int, default=80)
    args = parser.parse_args()

    source_text = args.source.read_text(encoding="utf-8-sig")
    clean_text = args.clean.read_text(encoding="utf-8")
    source_lines = source_text.splitlines()
    clean_lines = clean_text.splitlines()
    clean_fenced = fenced_line_mask(clean_lines)

    checks = Counter()
    for index, line in enumerate(clean_lines):
        if clean_fenced[index]:
            continue
        checks["source_markers"] += bool(SOURCE_RE.fullmatch(line))
        checks["images"] += bool(IMAGE_RE.fullmatch(line))
        checks["ocr_system_noise"] += bool(OCR_RE.fullmatch(line))
        checks["courtesy_candidates"] += bool(COURTESY_RE.fullmatch(body(line)))
        checks["noise_heading_candidates"] += bool(NOISE_HEADING_RE.fullmatch(line))
        checks["toc_dot_lines"] += bool(TOC_DOTS_RE.search(line))

    textbook_hierarchy_errors = 0
    hierarchy_details: list[dict[str, object]] = []
    if args.textbook:
        # Numbering depth is contextual.  In a normal chapter with `第X节`,
        # `一、`/`（一）` map to H3/H4; in an unsectioned 导论/绪论 they commonly
        # map to H2/H3.  A global numbering->level regex creates false alarms.
        chapter_has_section = False
        for line_no, line in enumerate(clean_lines, 1):
            if clean_fenced[line_no - 1]:
                continue
            info = HEADING_RE.match(line)
            if not info:
                continue
            level, title = len(info.group(1)), info.group(2).strip()
            if level == 1:
                chapter_has_section = False
            expected: int | None = None
            if CHAPTER_RE.match(title):
                expected = 1
            elif SECTION_RE.match(title):
                expected = 2
                chapter_has_section = True
            elif LEVEL3_RE.match(title):
                expected = 3 if chapter_has_section else 2
            elif LEVEL4_RE.match(title):
                expected = 4 if chapter_has_section else 3
            if expected is not None and level != expected:
                textbook_hierarchy_errors += 1
                hierarchy_details.append({
                    "line": line_no,
                    "actual": level,
                    "expected": expected,
                    "title": title[:160],
                })
        checks["textbook_hierarchy_errors"] = textbook_hierarchy_errors

    duplicates = paragraph_duplicates(clean_lines, args.min_duplicate_chars)
    provenance = prove_body_provenance(source_lines, clean_lines)
    # These are contextual QA candidates, not proof of bad output.  In
    # particular, a single dotted line can be prose rather than a TOC row.
    warning_keys = {"courtesy_candidates", "noise_heading_candidates", "toc_dot_lines"}
    warnings = {key: value for key, value in checks.items() if key in warning_keys and value}
    failures = {key: value for key, value in checks.items() if key not in warning_keys and value}
    if duplicates:
        failures["exact_duplicate_paragraphs"] = len(duplicates)
    if not provenance:
        failures["body_provenance"] = 1

    report = {
        "source": str(args.source),
        "clean": str(args.clean),
        "status": "PASS" if not failures else "FAIL",
        "body_provenance": "PASS" if provenance else "FAIL",
        "source_characters": len(source_text),
        "clean_characters": len(clean_text),
        "character_reduction_ratio": round((len(source_text) - len(clean_text)) / len(source_text), 6),
        "clean_nonblank_lines": sum(bool(line.strip()) for line in clean_lines),
        "checks": dict(checks),
        "textbook_hierarchy_details": hierarchy_details[:50],
        "warnings": warnings,
        "exact_duplicate_paragraphs": duplicates[:20],
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not failures else 1)


if __name__ == "__main__":
    main()
