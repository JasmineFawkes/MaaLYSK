from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import logger
from utils.maa_types import is_hit, ocr_text
from utils.params import parse_params


@AgentServer.custom_action("OcrReport")
class OcrReport(CustomAction):
    """Print a recognition node's result to the UI, optionally appending it to a text file.

    Examples:
        `custom_action_param`::

            {
                "recognition": "SomeOcrNode",
                "format": "本次识别到: {result}",
                "export": true,
                "filename": "report"
            }

    Args:
        recognition: 要运行的识别节点名（必填）。
        format: 自定义输出格式，``{result}`` 会被替换为识别到的最佳文本；
            省略时直接打印识别文本。
        export: 是否追加写入文本文件，默认 ``false``。
        filename: 导出文件名，自动补 ``.txt`` 后缀；省略时使用默认名。
    """

    DEFAULT_FILENAME = "ocr_report.txt"

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param, "recognition")
        except ValueError as error:
            logger.error(f"【OcrReport】参数无效：{error}")
            return CustomAction.RunResult(success=False)

        node = params["recognition"]
        if not isinstance(node, str) or not node.strip():
            logger.error("【OcrReport】缺少识别节点名（recognition）")
            return CustomAction.RunResult(success=False)
        node = node.strip()

        image = context.tasker.controller.post_screencap().wait().get()
        if image is None:
            logger.error("【OcrReport】截图失败，无法识别")
            return CustomAction.RunResult(success=False)

        detail = context.run_recognition(node, image)
        if not is_hit(detail):
            logger.info(f"【OcrReport】节点 {node} 未识别到目标")
            return CustomAction.RunResult(success=True)

        text = ocr_text(detail)
        fmt = params.get("format")
        if isinstance(fmt, str) and fmt:
            message = fmt.replace("{result}", text)
        else:
            message = text

        # 打印到 UI 是必做的，写 txt 是可选（export）
        logger.info(message)

        if params.get("export"):
            self._append_to_file(message, params)

        return CustomAction.RunResult(success=True)

    def _append_to_file(self, message: str, params: dict[str, Any]) -> None:
        filename = params.get("filename")
        if isinstance(filename, str) and filename.strip():
            filename = filename.strip()
            if not filename.lower().endswith(".txt"):
                filename += ".txt"
            path = Path(filename)
        else:
            path = Path(self.DEFAULT_FILENAME)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as file:
                file.write(f"{timestamp} {message}\n")
        except Exception:
            logger.exception(f"【OcrReport】写入导出文件失败 {path}")
