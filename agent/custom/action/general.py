from __future__ import annotations

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.params import parse_params


@AgentServer.custom_action("DisableNode")
class DisableNode(CustomAction):
    """Disable one pipeline node at runtime.

    运行时把 ``{节点名: {"enabled": false}}`` 通过 ``override_pipeline`` 合并进当前
    pipeline，之后 MaaFW 不再执行该节点（跳过识别与动作）。注意只影响后续执行，
    已在队列中/本次已命中的流程不受影响。

    Examples:
        `custom_action_param`::

            {
                "node_name": "NodeName"
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            node_name = parse_params(argv.custom_action_param, "node_name")["node_name"]
        except ValueError as error:
            logger.error("DisableNode: %s", error)
            return CustomAction.RunResult(success=False)

        context.override_pipeline({node_name: {"enabled": False}})
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("NodeOverride")
class NodeOverride(CustomAction):
    """Pass a pipeline override object directly to context.override_pipeline().

    DisableNode 的通用形态：把整份覆盖配置原样交给 MaaFW，运行时任意改动节点的
    recognition / action / next / timeout 等字段。覆盖字典的 key 就是 pipeline
    节点名，值是该节点要改的字段。

    Examples:
        `custom_action_param`::

            {
                "NodeName": {
                    "enabled": false,
                    "timeout": 1000
                }
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            pipeline_override = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("NodeOverride: %s", error)
            return CustomAction.RunResult(success=False)

        if pipeline_override:
            context.override_pipeline(pipeline_override)
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("ResetCount")
class ResetCount(CustomAction):
    """Clear hit counters for one or more nodes.

    清空节点的命中计数 hit_count。MaaFW 节点可配 ``max_hit_count`` 限制命中次数
    （常用于"循环 N 次后跳出"）。当流程用 JumpBack 反复回到同一节点时，计数会
    一直累积，需要按场景主动清零才能重新计。

    ``strict=True`` 时任一节点清零失败，整个 action 判定为失败；
    ``strict=False``（默认）只标记失败节点，不影响 action 成功。

    Examples:
        `custom_action_param`::

            {
                "nodes": ["NodeA", "NodeB"],
                "strict": false
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("ResetCount: %s", error)
            return CustomAction.RunResult(success=False)

        nodes = params.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            logger.error("ResetCount requires non-empty custom_action_param.nodes.")
            return CustomAction.RunResult(success=False)

        strict = params.get("strict", False)
        if not isinstance(strict, bool):
            logger.error("ResetCount requires boolean custom_action_param.strict.")
            return CustomAction.RunResult(success=False)

        has_failure = False
        for node_name in nodes:
            if not isinstance(node_name, str) or not node_name:
                has_failure = True
                continue
            if not context.clear_hit_count(node_name):
                has_failure = True

        return CustomAction.RunResult(success=not (strict and has_failure))


@AgentServer.custom_action("SubTask")
class SubTask(CustomAction):
    """Run sub tasks in order from a custom action node.

    ``run_task`` 以子任务名为入口跑一段独立 pipeline，``task_detail.status.failed``
    表示该段任务是否执行失败。

    ``continue=true``：某个子任务失败后仍继续跑后续子任务；
    ``strict=true``（默认）：任一子任务失败即让本 action 判定失败。

    Examples:
        `custom_action_param`::

            {
                "sub": ["TaskA", "TaskB"],
                "continue": false,
                "strict": true
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("SubTask: %s", error)
            return CustomAction.RunResult(success=False)

        tasks = params.get("sub")
        if not isinstance(tasks, list) or not tasks:
            logger.error("SubTask requires non-empty custom_action_param.sub.")
            return CustomAction.RunResult(success=False)

        continue_on_failure = bool(params.get("continue", False))
        strict = bool(params.get("strict", True))
        has_failure = False

        for task_name in tasks:
            if not isinstance(task_name, str) or not task_name:
                has_failure = True
                if not continue_on_failure:
                    break
                continue

            task_detail = context.run_task(task_name)
            if task_detail and task_detail.status.failed:
                has_failure = True
                if not continue_on_failure:
                    break

        return CustomAction.RunResult(success=not (has_failure and strict))
