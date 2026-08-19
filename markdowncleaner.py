"""可直接调用的 Markdown OCR 清理器。

示例：

    from markdowncleaner import MarkdownCleaner, CleanerConfig

    cleaner = MarkdownCleaner(CleanerConfig(window_chars=10_000))
    output_path = cleaner.clean("dataset/示例.md", "clean/示例.md")

    # 或使用默认输出位置：<输入文件所在目录>/clean/<原文件名>
    output_path = cleaner.clean("dataset/示例.md")

本模块复用 ``interactive_cleaner_experiment.py`` 中已经验证的规则引擎、
提示词和 LLM 调用实现。这里不复制规则，避免命令行版与 Python API 逐渐
产生两套不一致的清洗逻辑。
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import interactive_cleaner_experiment as engine


@dataclass(slots=True)
class CleanerConfig:
    """MarkdownCleaner 的运行配置。

    LLM 参数为空时，沿用环境变量或 ``.env`` 中的 ``LLM_*`` 配置。
    ``plan_path`` 用于重放已有 plan；``plan_out_path`` 用于在本次清洗中
    记录可重放的 plan。二者不能同时使用。
    """

    window_chars: int = 10_000
    preprocess: bool = True
    factbase_mode: bool | None = None
    no_action_validation: bool = False
    heading_postprocess: bool = False
    straight: bool = False
    exact_repeat_only: bool = False
    dotenv_path: str | Path | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout: int | None = None
    llm_max_retries: int | None = None
    plan_path: str | Path | None = None
    plan_out_path: str | Path | None = None
    write_audit: bool = True

    def __post_init__(self) -> None:
        if self.window_chars < 1000:
            raise ValueError("window_chars 不能小于 1000")
        if self.plan_path and self.plan_out_path:
            raise ValueError("plan_path 与 plan_out_path 不能同时使用")
        if self.straight and (self.plan_path or self.plan_out_path or self.heading_postprocess):
            raise ValueError("straight 不能与 plan 或 heading_postprocess 组合使用")
        if self.heading_postprocess and (self.plan_path or self.plan_out_path):
            raise ValueError("heading_postprocess 不能与 plan 组合使用")


class MarkdownCleaner:
    """通过文件路径调用的 Markdown OCR 清理器。

    ``clean()`` 的返回值永远是已写入的 clean Markdown 绝对路径；最近一次
    的审计结果可从 ``last_report`` 获取。
    """

    def __init__(self, config: CleanerConfig | Mapping[str, Any] | None = None) -> None:
        if config is None:
            self.config = CleanerConfig()
        elif isinstance(config, CleanerConfig):
            self.config = config
        elif isinstance(config, Mapping):
            self.config = CleanerConfig(**dict(config))
        else:
            raise TypeError("config 必须是 CleanerConfig、字典或 None")
        self.last_report: dict[str, Any] | None = None

    @staticmethod
    def _default_output_path(source: Path) -> Path:
        return source.parent / "clean" / source.name

    @staticmethod
    def _audit_path(output_path: Path) -> Path:
        return output_path.with_name(f"{output_path.stem}.audit.json")

    @contextmanager
    def _runtime_settings(self, source: Path) -> Iterator[None]:
        """加载 .env 并暂时应用本次配置，不污染调用方的进程环境。"""
        original_environment = dict(os.environ)
        dotenv_path = (
            Path(self.config.dotenv_path)
            if self.config.dotenv_path is not None
            else Path.cwd() / ".env"
        )
        engine.load_dotenv(dotenv_path)

        overrides: dict[str, str | None] = {
            "FACTBASE_MODE": (
                None if self.config.factbase_mode is None
                else ("1" if self.config.factbase_mode else "0")
            ),
            # 明确写入 0，避免上一次调用或外部环境遗留的开关影响本次任务。
            "NO_ACTION_VALIDATION": "1" if self.config.no_action_validation else "0",
            "EXACT_REPEAT_ONLY": "1" if self.config.exact_repeat_only else "0",
            "LLM_BASE_URL": self.config.llm_base_url,
            "LLM_API_KEY": self.config.llm_api_key,
            "LLM_MODEL": self.config.llm_model,
            "LLM_TIMEOUT": (
                str(self.config.llm_timeout) if self.config.llm_timeout is not None else None
            ),
            "LLM_MAX_RETRIES": (
                str(self.config.llm_max_retries)
                if self.config.llm_max_retries is not None else None
            ),
        }
        try:
            for key, value in overrides.items():
                if value is not None:
                    os.environ[key] = value
            yield
        finally:
            os.environ.clear()
            os.environ.update(original_environment)

    def _configure_plan(self, raw_bytes: bytes) -> None:
        """把 plan 配置接入既有引擎的重放/检查点能力。"""
        engine._PLAN_ACTIONS = None
        engine._PLAN_CURSOR = 0
        engine._PLAN_CHECKPOINT_PATH = None
        engine._PLAN_CHECKPOINT_SOURCE_SHA256 = None
        engine._PLAN_CHECKPOINT_WINDOW_CHARS = None

        if self.config.plan_path:
            plan_path = Path(self.config.plan_path)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if plan.get("version") != 1 or not isinstance(plan.get("actions"), list):
                raise RuntimeError("plan.json 格式无效：需要 version=1 和 actions 数组")
            if plan.get("source_sha256") != hashlib.sha256(raw_bytes).hexdigest():
                raise RuntimeError("plan.json 与原始文件 SHA-256 不匹配，拒绝生成输出")
            if int(plan.get("window_chars", -1)) != self.config.window_chars:
                raise RuntimeError("plan.json 的 window_chars 与配置不匹配")
            engine._PLAN_ACTIONS = plan["actions"]
        elif self.config.plan_out_path:
            engine._PLAN_CHECKPOINT_PATH = Path(self.config.plan_out_path)
            engine._PLAN_CHECKPOINT_SOURCE_SHA256 = hashlib.sha256(raw_bytes).hexdigest()
            engine._PLAN_CHECKPOINT_WINDOW_CHARS = self.config.window_chars

    @staticmethod
    def _reset_plan_state() -> None:
        engine._PLAN_ACTIONS = None
        engine._PLAN_CURSOR = 0
        engine._PLAN_CHECKPOINT_PATH = None
        engine._PLAN_CHECKPOINT_SOURCE_SHA256 = None
        engine._PLAN_CHECKPOINT_WINDOW_CHARS = None

    def clean(self, input_path: str | Path, output_path: str | Path | None = None) -> Path:
        """清洗一个 UTF-8 Markdown 文件并返回结果文件的绝对路径。"""
        source = Path(input_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"输入 Markdown 文件不存在：{source}")
        if source.suffix.lower() not in {".md", ".markdown"}:
            raise ValueError(f"输入文件不是 Markdown：{source}")
        target = (
            Path(output_path).expanduser().resolve()
            if output_path is not None
            else self._default_output_path(source).resolve()
        )
        if target == source:
            raise ValueError("output_path 不能覆盖输入文件")

        raw_bytes = source.read_bytes()
        try:
            with self._runtime_settings(source):
                self._configure_plan(raw_bytes)
                cleaned, report = engine.clean_markdown(
                    raw_bytes.decode("utf-8-sig"),
                    self.config.window_chars,
                    preprocess=self.config.preprocess,
                    heading_postprocess=self.config.heading_postprocess,
                    straight=self.config.straight,
                )
                if self.config.plan_out_path:
                    plan_path = Path(self.config.plan_out_path)
                    plan_path.parent.mkdir(parents=True, exist_ok=True)
                    plan_path.write_text(
                        json.dumps(
                            {
                                "version": 1,
                                "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                                "window_chars": self.config.window_chars,
                                "actions": report["interactive_actions"],
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
        finally:
            self._reset_plan_state()

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(cleaned, encoding="utf-8")
        self.last_report = report
        if self.config.write_audit:
            self._audit_path(target).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return target

    def clean_batch(self, input_dir: str | Path, output_dir: str | Path) -> dict[str, Any]:
        """递归清洗目录中的 Markdown；同名结果已存在时跳过。"""
        source_root = Path(input_dir).expanduser().resolve()
        target_root = Path(output_dir).expanduser().resolve()
        if not source_root.is_dir():
            raise NotADirectoryError(f"输入目录不存在：{source_root}")
        if source_root == target_root:
            raise ValueError("output_dir 不能与 input_dir 相同")
        target_root.mkdir(parents=True, exist_ok=True)
        sources = sorted(
            path for path in source_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".md", ".markdown"}
            and target_root not in path.resolve().parents
        )
        report: dict[str, Any] = {
            "found": len(sources), "processed": 0, "skipped": 0,
            "failed": 0, "outputs": [], "failures": [],
        }
        for source in sources:
            target = target_root / source.relative_to(source_root)
            if target.exists():
                report["skipped"] += 1
                continue
            try:
                result = self.clean(source, target)
                report["processed"] += 1
                report["outputs"].append(str(result))
            except Exception as exc:
                report["failed"] += 1
                report["failures"].append({"file": str(source), "error": str(exc)})
        return report


__all__ = ["CleanerConfig", "MarkdownCleaner"]
