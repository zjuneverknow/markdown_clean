# Markdown 正文提取器

流程分为两个职责隔离的阶段：

1. **清理与调整**：解析现有 Markdown 标题，Qwen 判断正文边界、排除区间和标题层级，本地脚本生成第一版正文与目录。
2. **检测与添加**：Qwen 对每个父节点的直接子标题执行同一种兄弟序列完整性审核，不区分一级、二级或三级。

每个缺口统一表示为 `gap`。有原文候选时提升候选；没有候选时，只有能够由多个连续兄弟严格推出的纯序号才允许结构化插入。其余缺口进入审核报告，不修改正文。

## 运行

```powershell
& "E:\ai_project\political_compliance_agent\.venv\Scripts\python.exe" -m markdown_clean ".\dataset\input.md" --out-dir result
```

自动读取当前目录 UTF-8 `.env` 中的 `LLM_*` 或 `POLITICAL_LLM_*` 配置，默认模型为 `qwen-plus`。

## 批量处理

```powershell
& "E:\ai_project\political_compliance_agent\.venv\Scripts\python.exe" -m markdown_clean ".\dataset" --batch --clean-dir ".\clean"
```

批处理递归处理 `.md` 和 `.markdown`，只在 `clean` 中写入最终结果，文件名及相对目录与原材料一致。目标文件已经存在时直接跳过，不调用 LLM。单个文件失败会显示错误并继续处理其他文件。

输出：

- `outline.json`：原始标题目录及稳定 ID
- `plan.json`：LLM 的结构计划
- `clean-stage1.md` / `toc-stage1.md`：仅完成清理与层级调整的中间结果
- `outline-stage1.json`：保留原文 ID 和行号的第一阶段目录，用于和候选准确对齐
- `heading-candidates.json`：第一版正文中的疑似漏识别标题
- `recovery-plan.json`：目录断序与标题找回计划
- `recovery-raw.json`：模型返回的原始 gap 审核结果，便于排查被确定性校验拒绝的项目
- `clean.md`：只含正文的 Markdown
- `toc.md`：重建后的正文目录
- `audit.json`：确定性执行统计
- `review.md`：简洁清理报告

可审核 `plan.json` 后离线重跑：

```powershell
& "E:\ai_project\political_compliance_agent\.venv\Scripts\python.exe" -m markdown_clean ".\dataset\input.md" --plan ".\result\plan.json" --out-dir verified
```
