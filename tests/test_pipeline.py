import os
import tempfile
import unittest
from pathlib import Path

from markdown_clean.pipeline import (apply_heading_recovery, batch_clean_dataset, execute_plan,
                                     extract_heading_candidates, extract_outline,
                                     load_dotenv, planned_outline, render_toc, setting,
                                     validate_plan, validate_recovery_plan)


class OutlineCleanerTests(unittest.TestCase):
    def setUp(self):
        self.source = (
            "# 书名\n封面信息\n# 目录\n条目\n"
            "## 第一部分\n# 第一篇文章\n日期\n正文一\n"
            "## 注释\n注释内容\n# 第二篇文章\n正文二\n"
            "# 索引\n索引内容\n"
        )
        self.outline = extract_outline(self.source)

    def test_outline_has_stable_ids_and_parent_relationships(self):
        self.assertEqual([item.id for item in self.outline[:3]], ["h0001", "h0002", "h0003"])
        self.assertEqual(self.outline[3].parent_id, None)
        self.assertEqual(self.outline[2].title, "第一部分")

    def test_execute_keeps_body_excludes_notes_and_back_matter(self):
        plan = {
            "body_start_id": "h0003",
            "body_end_before_id": "h0007",
            "exclude_section_ids": ["h0005"],
            "drop_heading_ids": [],
            "heading_levels": [
                {"id": "h0003", "level": 1}, {"id": "h0004", "level": 2},
                {"id": "h0006", "level": 2},
            ],
            "summary": "只保留正文",
        }
        cleaned, audit = execute_plan(self.source, self.outline, plan, max_delete_ratio=0.9)
        self.assertTrue(cleaned.startswith("# 第一部分\n## 第一篇文章"))
        self.assertIn("日期\n正文一", cleaned)
        self.assertIn("## 第二篇文章\n正文二", cleaned)
        self.assertNotIn("目录", cleaned)
        self.assertNotIn("注释内容", cleaned)
        self.assertNotIn("索引", cleaned)
        self.assertEqual(audit["body_start_line"], 5)

    def test_drop_heading_keeps_its_content(self):
        plan = {"body_start_id": "h0003", "body_end_before_id": "h0007",
                "exclude_section_ids": [], "drop_heading_ids": ["h0005"], "heading_levels": []}
        cleaned, _ = execute_plan(self.source, self.outline, plan, 0.9)
        self.assertNotIn("## 注释", cleaned)
        self.assertIn("注释内容", cleaned)

    def test_asset_lines_are_removed_without_touching_caption(self):
        source = "# 正文\n![](images/a.jpg)\n图片说明\n<!-- source -->\n正文\n"
        outline = extract_outline(source)
        plan = {"body_start_id": "h0001", "body_end_before_id": None,
                "exclude_section_ids": [], "drop_heading_ids": [], "heading_levels": []}
        cleaned, audit = execute_plan(source, outline, plan, max_delete_ratio=0.9)
        self.assertNotIn(".jpg", cleaned)
        self.assertNotIn("<!--", cleaned)
        self.assertIn("图片说明", cleaned)
        self.assertEqual(audit["counters"]["asset_lines_removed"], 2)

    def test_unknown_id_is_rejected(self):
        plan = {"body_start_id": "missing", "body_end_before_id": None,
                "exclude_section_ids": [], "drop_heading_ids": [], "heading_levels": []}
        with self.assertRaisesRegex(ValueError, "body_start_id"):
            validate_plan(plan, self.outline)

    def test_duplicate_excluded_sections_are_rejected(self):
        plan = {"body_start_id": "h0003", "body_end_before_id": None,
                "exclude_section_ids": ["h0005", "h0005"],
                "drop_heading_ids": [], "heading_levels": []}
        with self.assertRaisesRegex(ValueError, "不得重复"):
            validate_plan(plan, self.outline)

    def test_ranges_before_body_are_ignored(self):
        plan = {"body_start_id": "h0003", "body_end_before_id": "h0007",
                "exclude_section_ids": ["h0001"],
                "drop_heading_ids": [], "heading_levels": []}
        normalized = validate_plan(plan, self.outline)
        self.assertEqual(normalized["exclude_section_ids"], [])

    def test_delete_ratio_safety(self):
        plan = {"body_start_id": "h0007", "body_end_before_id": None,
                "exclude_section_ids": [], "drop_heading_ids": [], "heading_levels": []}
        with self.assertRaisesRegex(RuntimeError, "删除比例"):
            execute_plan(self.source, self.outline, plan, max_delete_ratio=0.1)

    def test_toc_uses_rebuilt_levels(self):
        toc = render_toc("# 专题\n## 文章\n正文\n")
        self.assertIn("- 专题\n  - 文章", toc)

    def test_second_stage_recovers_only_existing_candidate(self):
        source = "# 六、文化建设\n正文。\n\n七、社会建设\n\n正文。\n# 八、生态建设\n正文。\n"
        outline = extract_outline(source)
        candidates = extract_heading_candidates(source, outline)
        target = next(item for item in candidates if item.text == "七、社会建设")
        plan = validate_recovery_plan({
            "gaps": [{"parent_heading_id": None, "level": 1,
                      "previous_heading_id": "h0001", "next_heading_id": "h0002",
                      "missing_title": "七",
                      "repair": {"mode": "promote_candidate", "candidate_id": target.id},
                      "confidence": 0.96, "reason": "修复六到八的断序"}],
            "summary": "找回七",
        }, candidates, outline)
        cleaned, audit = apply_heading_recovery(source, candidates, plan)
        self.assertIn("# 七、社会建设", cleaned)
        self.assertEqual(audit["promoted_count"], 1)

    def test_low_confidence_candidate_is_not_promoted(self):
        source = "# 六\n\n七、候选\n\n# 八\n"
        outline = extract_outline(source)
        candidates = extract_heading_candidates(source, outline)
        plan = validate_recovery_plan({
            "gaps": [{"parent_heading_id": None, "level": 1,
                      "previous_heading_id": "h0001", "next_heading_id": "h0002",
                      "missing_title": "七",
                      "repair": {"mode": "promote_candidate", "candidate_id": candidates[0].id},
                      "confidence": 0.6, "reason": "证据不足"}],
        }, candidates, outline)
        cleaned, audit = apply_heading_recovery(source, candidates, plan)
        self.assertNotIn("# 七、候选", cleaned)
        self.assertEqual(len(audit["low_confidence_gaps"]), 1)

    def test_recovery_truncates_excluded_section_instead_of_losing_heading(self):
        source = (
            "# 六、文化建设\n正文\n## 注释\n注释一\n\n"
            "七、社会建设\n续行标题\n\n# 八、生态建设\n正文\n"
        )
        outline = extract_outline(source)
        candidates = extract_heading_candidates(source, outline)
        target = next(item for item in candidates if item.text == "七、社会建设续行标题")
        clean_plan = {
            "body_start_id": "h0001", "body_end_before_id": None,
            "exclude_section_ids": ["h0002"], "drop_heading_ids": [],
            "heading_levels": [{"id": "h0001", "level": 1}, {"id": "h0003", "level": 1}],
        }
        recovery = validate_recovery_plan({
            "gaps": [{"parent_heading_id": None, "level": 1,
                      "previous_heading_id": "h0001", "next_heading_id": "h0003",
                      "missing_title": "七",
                      "repair": {"mode": "promote_candidate", "candidate_id": target.id},
                      "confidence": 0.98, "reason": "六到八之间缺七"}],
        }, candidates, planned_outline(outline, clean_plan))
        cleaned, audit = execute_plan(source, outline, clean_plan, 0.9, candidates, recovery)
        self.assertNotIn("注释一", cleaned)
        self.assertIn("# 七、社会建设续行标题", cleaned)
        self.assertNotIn("\n续行标题", cleaned)
        self.assertIn("# 八、生态建设", cleaned)
        self.assertEqual(audit["counters"]["headings_recovered"], 1)

    def test_same_gap_protocol_inserts_missing_first_child(self):
        source = "# 文章\n第一部分正文\n## 二\n第二部分\n## 三\n第三部分\n"
        outline = extract_outline(source)
        clean_plan = {"body_start_id": "h0001", "body_end_before_id": None,
                      "exclude_section_ids": [], "drop_heading_ids": [], "heading_levels": []}
        view = planned_outline(outline, clean_plan)
        recovery = validate_recovery_plan({
            "gaps": [{"parent_heading_id": "h0001", "level": 2,
                      "previous_heading_id": None, "next_heading_id": "h0002",
                      "missing_title": "一",
                      "repair": {"mode": "insert_structural", "anchor_heading_id": "h0001"},
                      "confidence": 0.99, "reason": "同父子序列从二开始"}],
        }, [], view)
        cleaned, audit = execute_plan(source, outline, clean_plan, 0.9, [], recovery)
        self.assertIn("# 文章\n\n## 一\n第一部分正文", cleaned)
        self.assertEqual(audit["counters"]["structural_headings_inserted"], 1)

    def test_single_second_child_is_enough_with_substantial_first_content(self):
        source = "# 文章\n" + ("第一部分正文内容" * 20) + "\n## 二\n第二部分\n"
        outline = extract_outline(source)
        clean_plan = {"body_start_id": "h0001", "body_end_before_id": None,
                      "exclude_section_ids": [], "drop_heading_ids": [], "heading_levels": []}
        view = planned_outline(outline, clean_plan)
        recovery = validate_recovery_plan({
            "gaps": [{"parent_heading_id": "h0001", "level": 2,
                      "previous_heading_id": None, "next_heading_id": "h0002",
                      "missing_title": "一",
                      "repair": {"mode": "insert_structural", "anchor_heading_id": "h0001"},
                      "confidence": 0.93, "reason": "二之前存在完整第一部分"}],
        }, [], view)
        self.assertEqual(len(recovery["accepted_gaps"]), 1)

    def test_planned_outline_keeps_source_coordinates(self):
        plan = {
            "body_start_id": "h0003", "body_end_before_id": "h0007",
            "exclude_section_ids": ["h0005"], "drop_heading_ids": [],
            "heading_levels": [{"id": "h0003", "level": 1}, {"id": "h0004", "level": 2}],
        }
        result = planned_outline(self.outline, plan)
        self.assertEqual([item.id for item in result], ["h0003", "h0004", "h0006"])
        self.assertEqual(result[0].line, self.outline[2].line)
        self.assertEqual(result[1].parent_id, "h0003")

    def test_dotenv_and_project_setting_fallback(self):
        key = "MARKDOWN_CLEAN_TEST_KEY"
        old = os.environ.pop(key, None)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ".env"
                path.write_text(f"{key}=loaded\n", encoding="utf-8")
                load_dotenv(path)
                self.assertEqual(setting("MISSING_PRIMARY", key), "loaded")
        finally:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def test_batch_keeps_names_and_skips_existing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset"
            clean = root / "clean"
            (dataset / "nested").mkdir(parents=True)
            (dataset / "a.md").write_text("# A\n正文\n", encoding="utf-8")
            (dataset / "nested" / "b.markdown").write_text("# B\n正文\n", encoding="utf-8")
            (dataset / "ignored.txt").write_text("ignore", encoding="utf-8")
            clean.mkdir()
            (clean / "a.md").write_text("existing", encoding="utf-8")
            calls: list[str] = []

            def fake_cleaner(text: str) -> str:
                calls.append(text)
                return "CLEAN\n"

            report = batch_clean_dataset(dataset, clean, fake_cleaner)
            self.assertEqual(report["found"], 2)
            self.assertEqual(report["processed"], 1)
            self.assertEqual(report["skipped"], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual((clean / "a.md").read_text(encoding="utf-8"), "existing")
            self.assertEqual((clean / "nested" / "b.markdown").read_text(encoding="utf-8"), "CLEAN\n")
            self.assertFalse((clean / "ignored.txt").exists())


if __name__ == "__main__":
    unittest.main()
