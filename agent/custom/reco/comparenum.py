from __future__ import annotations

import re
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_recognition import CustomRecognition

from utils import logger
from utils.maa_types import is_hit, ocr_text
from utils.params import parse_params

_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")

_COMPARATORS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


@AgentServer.custom_recognition("CompareNum")
class CompareNum(CustomRecognition):
    """Compare an OCR-recognized number against an expected value, hit only when the condition holds.

    Runs the given OCR recognition node on the current image (optionally narrowed
    to ``roi``), extracts the first number from the recognized text, then hits
    only when the condition holds — so the pipeline can branch on a numeric condition.

    支持两种模式：单值比较（``expected`` + ``operator``）或区间判断（``min``/``max``，每侧可用
    ``min_op``/``max_op`` 指定开闭）。

    Examples:
        `custom_recognition_param`::
        单值模式 ::

            {
                "recognition": "CompareNodeName",
                "roi": [605, 63, 38, 28],
                "expected": 550,
                "operator": ">="
            }

        区间模式（每侧开闭可配）::

            {
                "recognition": "CompareNodeName",
                "roi": [605, 63, 38, 28],
                "min": 30,
                "min_op": ">",     // 30 < 体力
                "max": 50,
                "max_op": "<="     // 体力 <= 50，即 30 < 体力 <= 50
            }

    Args:
        recognition: 要运行的 OCR 识别节点名（必填）。
        roi: 可选 ``[x, y, w, h]``，限定 OCR 区域。

        单值模式（需 expected + operator）:
            expected: 期望值，数字。
            operator: 比较符，``">"`` ``">="`` ``"<"`` ``"<="``。

        区间模式（给 min 和/或 max）:
            min / min_op: 下界及其比较符，min_op 支持 ``">="`` ``">"``，默认 ``">="``。
            max / max_op: 上界及其比较符，max_op 支持 ``"<="`` ``"<"``，默认 ``"<="``。
            只给一侧做单向判断，两侧都省略则无区间约束。

    Returns:
        命中时返回 ROI（或识别框）与比较结果 detail；否则返回 None。
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult | None:
        try:
            params = parse_params(argv.custom_recognition_param, "recognition")
        except ValueError as error:
            logger.debug("CompareNum: %s", error)
            return None

        node = params["recognition"]
        if not isinstance(node, str) or not node.strip():
            logger.debug("CompareNum: 缺少识别节点名（recognition）")
            return None
        node = node.strip()

        roi = params.get("roi")
        if roi is not None and (not isinstance(roi, (list, tuple)) or len(roi) != 4):
            logger.debug(f"CompareNum: roi 必须为 [x, y, w, h]，当前：{roi!r}")
            return None

        # 区间模式：给了 min/max 任一即进入；否则走单值比较模式
        min_value = params.get("min")
        max_value = params.get("max")
        range_mode = min_value is not None or max_value is not None

        if range_mode:
            # 每侧可单独指定开闭；min_op 只允许 >= 或 >，max_op 只允许 <= 或 <
            min_op = params.get("min_op", ">=")
            max_op = params.get("max_op", "<=")
            lower_comparator = _COMPARATORS.get(min_op)
            upper_comparator = _COMPARATORS.get(max_op)
            if min_op not in (">=", ">"):
                logger.debug(f"CompareNum: min_op 仅支持 >= 或 >，当前：{min_op!r}")
                return None
            if max_op not in ("<=", "<"):
                logger.debug(f"CompareNum: max_op 仅支持 <= 或 <，当前：{max_op!r}")
                return None
            try:
                lower = float(min_value) if min_value is not None else None
                upper = float(max_value) if max_value is not None else None
            except (TypeError, ValueError):
                logger.debug(
                    f"CompareNum: min/max 必须是数字，当前 min={min_value!r}，max={max_value!r}"
                )
                return None
            if lower is not None and upper is not None and lower > upper:
                logger.debug(f"CompareNum: min({lower:g}) 不能大于 max({upper:g})")
                return None
        else:
            expected = params.get("expected")
            operator = params.get("operator")
            if expected is None or operator is None:
                logger.debug("CompareNum: 配置不完整：区间模式需 min/max，单值模式需 expected+operator")
                return None
            comparator = _COMPARATORS.get(operator)
            if comparator is None:
                logger.debug(f"CompareNum: 不支持的比较符 {operator!r}（支持 >, >=, <, <=）")
                return None
            try:
                expected_number = float(expected)
            except (TypeError, ValueError):
                logger.debug(f"CompareNum: expected 必须是数字，当前：{expected!r}")
                return None

        # 通过 pipeline override 把 roi 交给识别节点，而不是手动裁剪小图
        if roi is not None:
            override = {node: {"recognition": {"param": {"roi": list(roi)}}}}
            detail = context.run_recognition(node, argv.image, override)
        else:
            detail = context.run_recognition(node, argv.image)

        if not is_hit(detail):
            return None

        text = ocr_text(detail)
        match = _NUMBER_PATTERN.search(text or "")
        if match is None:
            return None

        recognized_number = float(match.group())

        if range_mode:
            if lower is not None and not lower_comparator(recognized_number, lower):
                return None
            if upper is not None and not upper_comparator(recognized_number, upper):
                return None
            result_detail: dict[str, Any] = {
                "recognized": recognized_number,
                "min": lower,
                "min_op": min_op,
                "max": upper,
                "max_op": max_op,
                "in_range": True,
            }
        else:
            if not comparator(recognized_number, expected_number):
                return None
            result_detail = {
                "recognized": recognized_number,
                "expected": expected_number,
                "operator": operator,
            }

        box = list(roi) if roi is not None else [int(v) for v in detail.box]
        return CustomRecognition.AnalyzeResult(
            box=box,
            detail=result_detail,
        )
