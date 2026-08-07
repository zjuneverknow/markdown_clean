# Markdown Cleaner

用于把 PDF、Word 或 OCR 转出的 Markdown 整理成适合后续切块和检索的正文数据：删除确定的出版/OCR 噪声，隔离附录、注释、图注，并谨慎修正明显错误的标题层级。它不会让模型重写正文。

## 启动前准备

- Windows PowerShell
- Python 3.10+
- 可选：一个兼容 OpenAI `Chat Completions` 的模型服务。阿里云百炼的 `qwen-plus` 可直接使用。

项目没有第三方 Python 依赖，下面的命令直接使用已有虚拟环境即可。程序启动时会自动从项目根目录加载 UTF-8 编码的 `.env`，且不会覆盖已在 PowerShell 中设置的同名环境变量。

## 配置百炼 API

在项目根目录创建 `.env`，将 Key 替换为自己的百炼 API Key：

```powershell
$env:LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:LLM_API_KEY = "你的百炼_API_Key"
$env:LLM_MODEL = "qwen-plus"
$env:LLM_TIMEOUT = "120"
$env:LLM_MAX_RETRIES = "2"
```

如果使用北京地域的专属兼容地址，请替换 `LLM_BASE_URL`，并确保 Key 与该地域匹配。

可直接复制 [.env.example](E:/ai_project/markdown_clean/.env.example) 后填写 `.env`。不要提交包含真实 API Key 的 `.env`。

## 单文件处理

先进入项目目录：

```powershell
Set-Location "E:\ai_project\markdown_clean"
```

使用 LLM 清洗：

```powershell
& "E:\ai_project\political_compliance_agent\.venv\Scripts\python.exe" `
  -m cleaner `
  ".\dataset\《习近平谈治国理政》第一卷.md" `
  --out-dir ".\result\第一卷"
```

PowerShell 中，路径前必须带 `&`。否则会出现 `意外的标记 '-m'`。

不调用模型、仅执行确定性规则（目录、索引、图片路径、附录、注释等）时：

```powershell
& "E:\ai_project\political_compliance_agent\.venv\Scripts\python.exe" `
  -m cleaner `
  ".\dataset\《习近平谈治国理政》第一卷.md" `
  --out-dir ".\result\第一卷-rules" `
  --rules-only
```

## 批量处理

递归扫描 `dataset` 下的 `.md` 和 `.markdown` 文件，并在 `result` 下保持原有目录结构：

```powershell
& "E:\ai_project\political_compliance_agent\.venv\Scripts\python.exe" `
  -m cleaner ".\dataset" `
  --batch `
  --out-dir ".\result"
```

仅规则批处理：

```powershell
& "E:\ai_project\political_compliance_agent\.venv\Scripts\python.exe" `
  -m cleaner ".\dataset" `
  --batch `
  --out-dir ".\result-rules" `
  --rules-only
```

## 输出结果

单文件输出目录包含：

```text
result/第一卷/
├── clean.md       # 用于后续 chunk / 检索的核心正文
├── auxiliary.md   # 附录、注释、图注等可能有事实价值的材料
└── audit.json     # 删除/隔离区域、LLM 操作和校验结果
```

批处理根目录还会生成 `batch_report.json`，其中记录成功和失败的文件。

## 常用参数

```powershell
--window-chars 9000        # 每次送给模型的 CORE 文本目标大小，范围 2000~30000
--context-lines 16         # 每个窗口前后只读上下文行数
--max-delete-ratio 0.60    # DROP 区域占原始行数的告警阈值
--rules-only               # 禁用 LLM，仅运行确定性清洗
```

## 当前清洗边界

- 确定性处理：目录、封面/出版前置内容、索引/CIP 尾页、Markdown 图片路径、附录、注释、图注。
- LLM 仅能对正文行执行三类操作：调整已有标题的 `#` 数量、移除误加的 `#`、删除明确的重复标题或 OCR 噪声。
- LLM 必须给出原行逐字匹配的 `expected_text`；不匹配即不执行。因此正文不会被模型重写。
