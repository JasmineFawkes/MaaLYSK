"""外部通知 / External notification.

对标 MAA ``MaaWpfGui/Services/ExternalNotification`` 的移植实现。

映射关系::

    MAA (C#/WPF)                          MaaLYSK (MaaFW + Python Agent)
    -----------------------------------   ----------------------------------------
    IExternalNotificationProvider     ->  NotificationProvider (Protocol)
    ExternalNotificationService       ->  NotificationService
    Gotify/ServerChan/Telegram/...    ->  *_Provider 类（同名的 10 种渠道）
    DummyNotificationProvider         ->  DummyProvider（占位，永远成功）
    Event.AllTaskComplete             ->  NotificationSink(TaskerEventSink)
    （无对应物）                      ->  FocusLogSink(ContextEventSink) 收集 focus
    SettingsViewModel 持久化配置       ->  config/notification.json

差异说明：

- MAA 的配置项由 WPF 配置系统托管；本项目 GUI 是 MFAAvalonia，
  项目侧没有自己的设置页，配置落在 ``config/notification.json``。
- MAA 在 ``TaskQueueViewModel`` 里显式调用通知；本项目通过 MaaFW 的
  **事件监听器（sink）** 在任务开始/结束的回调里自动触发，无需改 pipeline。
- **通知正文不同**：MAA 只发一句「任务已完成」；本项目按需求发送
  **任务开始时间** + **任务过程中 GUI 日志窗口展示的内容**（节点 focus 消息，
  可选叠加 Agent 自身日志）。
- 全部依赖 Python 标准库（``urllib`` / ``smtplib``），不新增第三方包，
  因此不需要改动 ``requirements.txt``。

Config example (``config/notification.json``)::

    {
      "enabled": true,
      "title_prefix": "[MaaLYSK]",
      "send_on": ["task_started", "all_task_complete", "task_failed"],
      "include_focus": true,
      "include_agent_log": true,
      "max_log_lines": 100,
      "entry_filter": [],
      "providers": [
        {"type": "dingtalk", "enabled": true, "token": "...", "secret": ""},
        {"type": "telegram", "enabled": true, "bot_token": "...", "chat_id": "..."}
      ]
    }
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import smtplib
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any, Callable, Protocol

from maa.agent.agent_server import AgentServer
from maa.context import Context, ContextEventSink
from maa.custom_action import CustomAction
from maa.event_sink import NotificationType
from maa.tasker import Tasker, TaskerEventSink

from utils import logger
from utils.params import parse_params
from utils.runtime_paths import get_runtime_paths

__all__ = [
    "NotificationProvider",
    "NotificationSettings",
    "NotificationService",
    "NotificationSink",
    "FocusLogSink",
    "ExternalNotify",
    "extract_focus",
    "service",
]

DEFAULT_TIMEOUT = 10
CONFIG_FILENAME = "notification.json"
EXAMPLE_FILENAME = "notification.example.json"

# 本模块日志的统一前缀；收集 Agent 日志时要排除自身，避免自激
OWN_LOG_MARK = "【外部通知】"

# 触发时机 / send_on 可选值
EVENT_ALL_TASK_COMPLETE = "all_task_complete"
EVENT_TASK_FAILED = "task_failed"
EVENT_TASK_STARTED = "task_started"
EVENT_MANUAL = "manual"


# --------------------------------------------------------------------------
# HTTP 工具
# --------------------------------------------------------------------------
def _post_json(url: str, payload: Any, headers: dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """POST JSON，返回 (是否成功, 响应文本)。不抛异常。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _post_raw(url, body, "application/json; charset=utf-8", headers, timeout)


def _post_form(url: str, form: dict[str, str], headers: dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """POST application/x-www-form-urlencoded。"""
    body = urllib.parse.urlencode(form).encode("utf-8")
    return _post_raw(url, body, "application/x-www-form-urlencoded; charset=utf-8", headers, timeout)


def _post_raw(
    url: str,
    body: bytes,
    content_type: str,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("User-Agent", "MaaLYSK-ExternalNotification")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:  # 有响应体，尽量读出来便于排查
        text = ""
        try:
            text = error.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读取失败时忽略，保留空串
            pass
        return False, f"HTTP {error.code}: {text}"
    except Exception as error:  # noqa: BLE001 - 通知失败绝不能影响任务流程
        return False, str(error)


def _get(url: str, params: dict[str, str], timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    query = urllib.parse.urlencode(params)
    full = f"{url}?{query}" if query else url
    request = urllib.request.Request(full, method="GET")
    request.add_header("User-Agent", "MaaLYSK-ExternalNotification")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, response.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        return False, str(error)


def _json_field(text: str, key: str) -> Any:
    try:
        return json.loads(text).get(key)
    except Exception:  # noqa: BLE001
        return None


def _escape_json_string(value: str) -> str:
    """把内容安全地塞进 JSON 字符串字面量（对齐 MAA 的 EscapeJsonString）。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n")


# --------------------------------------------------------------------------
# focus / 日志文本处理
#
# MaaFW 回调里的 focus 是「任意类型」（见 2.3 回调协议），实际形态有三种：
#   1. 字符串简写            -> 等价于 display: ["log"]
#   2. 单条消息对象          -> {"content": "...", "display": [...], "trace": bool}
#   3. 整个 focus 字典       -> {"Node.Action.Succeeded": "...", ...}（最常见）
# 占位符 {name}/{task_id} 等由 Client 替换，Agent 收到的是**未替换**的模板，
# 因此这里需要自己渲染一遍（对已渲染的文本再跑一次也是幂等的）。
# --------------------------------------------------------------------------
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_DISPLAY_LOG = "log"

# 参与占位符渲染的字段（只取标量，避免把 list/dict 塞进文本）
_SCALAR_TYPES = (str, int, float, bool)


def strip_html(text: str) -> str:
    """去掉 HTML 标签，纯文本渠道（钉钉/Telegram 等）不该收到 ``<span>`` 这类噪音。"""
    if "<" not in text:
        return text
    return _HTML_TAG_RE.sub("", text)


def render_focus_template(template: str, details: dict[str, Any]) -> str:
    """用回调 details 里的标量字段替换 ``{name}`` ``{task_id}`` 等占位符。"""
    if "{" not in template:
        return template
    for key, value in details.items():
        if isinstance(value, _SCALAR_TYPES):
            template = template.replace(f"{{{key}}}", str(value))
    return template


def _format_duration(seconds: float) -> str:
    """把秒数格式化成人能读的耗时。"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    if minutes < 60:
        return f"{minutes} 分 {rest:.0f} 秒"
    hours = minutes // 60
    return f"{hours} 小时 {minutes % 60} 分 {rest:.0f} 秒"


def _as_display_list(value: Any) -> list[str]:
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [_DISPLAY_LOG]


def extract_focus(focus: Any, message: str, details: dict[str, Any]) -> str | None:
    """从回调的 focus 里取出「会显示到日志窗口」的那条文本。

    取不到、或 display 不含 ``log``（即不进日志窗口）时返回 None。
    """
    if focus is None:
        return None

    # 形态 1：字符串简写
    if isinstance(focus, str):
        text = focus.strip()
        return render_focus_template(text, details) if text else None

    if not isinstance(focus, dict):
        return None

    # 形态 2：单条消息对象 {content, display, trace}
    if "content" in focus:
        displays = _as_display_list(focus.get("display"))
        if _DISPLAY_LOG not in displays:
            return None
        text = str(focus.get("content") or "").strip()
        return render_focus_template(text, details) if text else None

    # 形态 3：整个 focus 字典，按当前消息类型取值后递归解析
    entry = focus.get(message)
    if entry is not None:
        return extract_focus(entry, message, details)

    return None


# --------------------------------------------------------------------------
# Provider 接口（对应 MAA 的 IExternalNotificationProvider）
# --------------------------------------------------------------------------
class NotificationProvider(Protocol):
    """通知渠道接口。对应 MAA 的 ``IExternalNotificationProvider``。"""

    @property
    def name(self) -> str:
        """渠道显示名，用于日志。"""

    def send(self, title: str, content: str) -> bool:
        """发送通知，成功返回 True。实现内部必须吞掉所有异常。"""


class DummyProvider:
    """空实现，对应 MAA 的 ``DummyNotificationProvider``。"""

    @property
    def name(self) -> str:
        return "Dummy"

    def send(self, title: str, content: str) -> bool:
        logger.debug(f"【外部通知】未启用任何渠道，跳过（title={title}）")
        return True


class DingTalkProvider:
    """钉钉机器人。token 支持直接粘贴完整 webhook，自动提取 access_token。"""

    ENDPOINT = "https://oapi.dingtalk.com/robot/send"

    def __init__(self, token: str = "", secret: str = "", **_: Any) -> None:
        self._access_token = self._extract_token(token)
        self._secret = secret or ""

    @property
    def name(self) -> str:
        return "钉钉"

    @staticmethod
    def _extract_token(token: str) -> str:
        raw = (token or "").strip()
        if not raw:
            return ""
        if "access_token=" in raw:
            return urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("access_token", [""])[0]
        return raw

    def _build_url(self) -> str:
        url = f"{self.ENDPOINT}?access_token={self._access_token}"
        if not self._secret:
            return url
        timestamp = str(int(datetime.now().timestamp() * 1000))
        sign_base = f"{timestamp}\n{self._secret}".encode("utf-8")
        digest = hmac.new(self._secret.encode("utf-8"), sign_base, hashlib.sha256).digest()
        sign = urllib.parse.quote(base64.b64encode(digest).decode())
        return f"{url}&timestamp={timestamp}&sign={sign}"

    def send(self, title: str, content: str) -> bool:
        if not self._access_token:
            logger.warning("【外部通知】钉钉 token 为空，跳过")
            return False
        ok, text = _post_json(
            self._build_url(),
            {"msgtype": "text", "text": {"content": f"{title}\n{content}"}},
        )
        if ok and _json_field(text, "errcode") == 0:
            return True
        logger.warning(f"【外部通知】钉钉发送失败：{text}")
        return False


class TelegramProvider:
    """Telegram Bot。"""

    API_BASE = "https://api.telegram.org"

    def __init__(self, bot_token: str = "", chat_id: str = "", topic_id: str = "", **_: Any) -> None:
        self._bot_token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._topic_id = (topic_id or "").strip()

    @property
    def name(self) -> str:
        return "Telegram"

    def send(self, title: str, content: str) -> bool:
        if not self._bot_token or not self._chat_id:
            logger.warning("【外部通知】Telegram 的 bot_token / chat_id 不完整，跳过")
            return False
        payload: dict[str, Any] = {"chat_id": self._chat_id, "text": f"{title}\n{content}"}
        if self._topic_id:
            payload["message_thread_id"] = self._topic_id
        ok, text = _post_json(f"{self.API_BASE}/bot{self._bot_token}/sendMessage", payload)
        if ok and '"ok":false' not in text:
            return True
        logger.warning(f"【外部通知】Telegram 发送失败：{text}")
        return False


class DiscordProvider:
    """Discord Webhook（比 Bot DM 更简单，无需 Bot Token）。"""

    def __init__(self, webhook_url: str = "", username: str = "", **_: Any) -> None:
        self._url = (webhook_url or "").strip()
        self._username = (username or "").strip()

    @property
    def name(self) -> str:
        return "Discord"

    def send(self, title: str, content: str) -> bool:
        if not self._url:
            logger.warning("【外部通知】Discord webhook 地址为空，跳过")
            return False
        payload: dict[str, Any] = {"content": f"**{title}**\n{content}"}
        if self._username:
            payload["username"] = self._username
        ok, text = _post_json(self._url, payload)
        if ok:
            return True
        logger.warning(f"【外部通知】Discord 发送失败：{text}")
        return False


class ServerChanProvider:
    """Server酱 / Server酱³。send_key 支持 ``sctp`` 形式。"""

    API_BASE = "https://sctapi.ftqq.com"
    TURBO_TEMPLATE = "https://{tuid}.push.ft07.com"

    def __init__(self, send_key: str = "", **_: Any) -> None:
        self._send_key = (send_key or "").strip()

    @property
    def name(self) -> str:
        return "Server酱"

    def _build_url(self) -> str:
        key = self._send_key
        if key.startswith("sctp"):
            # sctp{tuid}t{key} -> https://{tuid}.push.ft07.com/send/{key}.send
            parts = key.split("t", 2)
            if len(parts) == 3:
                return f"{self.TURBO_TEMPLATE.format(tuid=parts[1])}/send/{parts[2]}.send"
        return f"{self.API_BASE}/{key}.send"

    def send(self, title: str, content: str) -> bool:
        if not self._send_key:
            logger.warning("【外部通知】Server酱 send_key 为空，跳过")
            return False
        ok, text = _post_form(self._build_url(), {"title": title, "desp": content})
        if ok and '"code":0' in text.replace(" ", ""):
            return True
        logger.warning(f"【外部通知】Server酱发送失败：{text}")
        return False


class QmsgProvider:
    """Qmsg 酱。"""

    def __init__(self, server: str = "https://qmsg.zendee.cn", key: str = "", user: str = "", bot: str = "", **_: Any) -> None:
        self._server = (server or "https://qmsg.zendee.cn").strip().rstrip("/")
        self._key = (key or "").strip()
        self._user = (user or "").strip()
        self._bot = (bot or "").strip()

    @property
    def name(self) -> str:
        return "Qmsg"

    def send(self, title: str, content: str) -> bool:
        if not self._key or not self._user:
            logger.warning("【外部通知】Qmsg 的 key / user 不完整，跳过")
            return False
        params = {"msg": f"{title}\n{content}", "qq": self._user}
        if self._bot:
            params["bot"] = self._bot
        ok, text = _post_form(f"{self._server}/send/{self._key}", params)
        if ok and _json_field(text, "success") is True:
            return True
        logger.warning(f"【外部通知】Qmsg 发送失败：{text}")
        return False


class WxPusherProvider:
    """WxPusher 微信推送。``app_token`` 留空时 ``uid`` 走极简推送(SPT)。"""

    API_BASE = "https://wxpusher.zjiecode.com"

    def __init__(self, app_token: str = "", uid: str = "", **_: Any) -> None:
        self._app_token = (app_token or "").strip()
        self._uid = (uid or "").strip()

    @property
    def name(self) -> str:
        return "WxPusher"

    def send(self, title: str, content: str) -> bool:
        if not self._uid:
            logger.warning("【外部通知】WxPusher 的 uid/SPT 为空，跳过")
            return False
        if self._app_token:
            payload = {
                "appToken": self._app_token,
                "content": f"{title}\n{content}",
                "summary": title,
                "contentType": 1,
                "uids": [self._uid],
            }
            endpoint = f"{self.API_BASE}/api/send/message"
        else:
            payload = {"spt": self._uid, "content": f"{title}\n{content}", "contentType": 1}
            endpoint = f"{self.API_BASE}/api/send/simple_message"
        ok, text = _post_json(endpoint, payload)
        if ok and _json_field(text, "success") is True:
            return True
        logger.warning(f"【外部通知】WxPusher 发送失败：{text}")
        return False


class BarkProvider:
    """Bark（iOS）。"""

    def __init__(self, server: str = "https://api.day.app", send_key: str = "", **_: Any) -> None:
        self._server = (server or "https://api.day.app").strip().rstrip("/")
        self._send_key = (send_key or "").strip()

    @property
    def name(self) -> str:
        return "Bark"

    def send(self, title: str, content: str) -> bool:
        if not self._server or not self._send_key:
            logger.warning("【外部通知】Bark 的 server / send_key 不完整，跳过")
            return False
        ok, text = _post_json(
            f"{self._server}/push",
            {"device_key": self._send_key, "title": title, "body": content, "group": "MaaLYSK"},
        )
        if ok and _json_field(text, "code") == 200:
            return True
        logger.warning(f"【外部通知】Bark 发送失败：{text}")
        return False


class GotifyProvider:
    """Gotify 自建推送。"""

    def __init__(self, server: str = "", token: str = "", **_: Any) -> None:
        self._server = (server or "").strip().rstrip("/")
        self._token = (token or "").strip()

    @property
    def name(self) -> str:
        return "Gotify"

    def send(self, title: str, content: str) -> bool:
        if not self._server or not self._token:
            logger.warning("【外部通知】Gotify 的 server / token 不完整，跳过")
            return False
        ok, text = _post_json(
            f"{self._server}/message",
            {"title": title, "message": content},
            headers={"X-Gotify-Key": self._token},
        )
        if ok and _json_field(text, "id") is not None:
            return True
        logger.warning(f"【外部通知】Gotify 发送失败：{text}")
        return False


class SmtpProvider:
    """SMTP 邮件。使用标准库 smtplib，不依赖 MailKit。"""

    def __init__(
        self,
        server: str = "",
        port: int | str = 465,
        use_ssl: bool = True,
        requires_auth: bool = True,
        username: str = "",
        password: str = "",
        sender: str = "",
        to: str = "",
        **_: Any,
    ) -> None:
        self._server = (server or "").strip()
        self._port = int(port) if str(port).strip().isdigit() else 465
        self._use_ssl = bool(use_ssl)
        self._requires_auth = bool(requires_auth)
        self._username = (username or "").strip()
        self._password = password or ""
        self._sender = (sender or "").strip()
        self._to = (to or "").strip()

    @property
    def name(self) -> str:
        return "邮件(SMTP)"

    def send(self, title: str, content: str) -> bool:
        if not self._server or not self._sender or not self._to:
            logger.warning("【外部通知】SMTP 的 server / from / to 不完整，跳过")
            return False
        message = MIMEText(content.replace("\n", "<br/>"), "html", "utf-8")
        message["Subject"] = Header(title.replace("\r", "").replace("\n", ""), "utf-8")
        message["From"] = formataddr(parseaddr(self._sender))
        message["To"] = formataddr(parseaddr(self._to))
        try:
            if self._use_ssl:
                client = smtplib.SMTP_SSL(self._server, self._port, timeout=DEFAULT_TIMEOUT, context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(self._server, self._port, timeout=DEFAULT_TIMEOUT)
            try:
                if not self._use_ssl:
                    client.starttls(context=ssl.create_default_context())
                if self._requires_auth:
                    client.login(self._username, self._password)
                client.sendmail(self._sender, self._to.split(","), message.as_string())
            finally:
                try:
                    client.quit()
                except Exception:  # noqa: BLE001 - quit 失败不影响结果
                    pass
            return True
        except Exception as error:  # noqa: BLE001
            logger.warning(f"【外部通知】SMTP 发送失败：{error}")
            return False


class WebhookProvider:
    """通用 Webhook。body 模板支持 ``{title}`` ``{content}`` ``{time}`` 占位符。"""

    def __init__(
        self,
        url: str = "",
        method: str = "POST",
        headers: str = "",
        body: str = '{"text": "{title}\n{content}"}',
        content_type: str = "application/json",
        **_: Any,
    ) -> None:
        self._url = (url or "").strip()
        self._method = (method or "POST").strip().upper()
        self._header_text = headers or ""
        self._body_template = body or '{"text": "{title}\n{content}"}'
        self._content_type = (content_type or "application/json").strip()

    @property
    def name(self) -> str:
        return "Webhook"

    def _parse_headers(self) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for line in self._header_text.replace("\r", "").split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if key:
                parsed[key] = value
        return parsed

    def send(self, title: str, content: str) -> bool:
        if not self._url:
            logger.warning("【外部通知】Webhook 地址为空，跳过")
            return False
        body = (
            self._body_template.replace("{title}", _escape_json_string(title))
            .replace("{content}", _escape_json_string(content))
            .replace("{time}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        if self._method == "GET":
            ok, text = _get(self._url + ("?" + body if body else ""), {})
        else:
            ok, text = _post_raw(self._url, body.encode("utf-8"), self._content_type, self._parse_headers())
        if ok:
            return True
        logger.warning(f"【外部通知】Webhook 发送失败：{text}")
        return False


PROVIDER_FACTORIES: dict[str, Callable[..., Any]] = {
    "dingtalk": DingTalkProvider,
    "telegram": TelegramProvider,
    "discord": DiscordProvider,
    "serverchan": ServerChanProvider,
    "qmsg": QmsgProvider,
    "wxpusher": WxPusherProvider,
    "bark": BarkProvider,
    "gotify": GotifyProvider,
    "smtp": SmtpProvider,
    "webhook": WebhookProvider,
    "custom_webhook": WebhookProvider,
}


def build_provider(config: dict[str, Any]) -> Any:
    """按 ``type`` 字段构造 Provider；未知或缺失时返回 DummyProvider。"""
    if not isinstance(config, dict):
        return DummyProvider()
    provider_type = str(config.get("type", "")).strip().lower()
    factory = PROVIDER_FACTORIES.get(provider_type)
    if factory is None:
        logger.warning(f"【外部通知】未知的通知渠道类型：{provider_type}")
        return DummyProvider()
    try:
        return factory(**config)
    except Exception as error:  # noqa: BLE001
        logger.warning(f"【外部通知】构造渠道 {provider_type} 失败：{error}")
        return DummyProvider()


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
EXAMPLE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "title_prefix": "[MaaLYSK]",
    "send_on": [EVENT_ALL_TASK_COMPLETE],
    "include_focus": True,
    "include_agent_log": True,
    "max_log_lines": 100,
    "entry_filter": [],
    "providers": [
        {
            "type": "dingtalk",
            "enabled": False,
            "token": "https://oapi.dingtalk.com/robot/send?access_token=你的token",
            "secret": "",
        },
        {"type": "telegram", "enabled": False, "bot_token": "", "chat_id": "", "topic_id": ""},
        {"type": "discord", "enabled": False, "webhook_url": "", "username": "MaaLYSK"},
        {"type": "serverchan", "enabled": False, "send_key": ""},
        {"type": "wxpusher", "enabled": False, "app_token": "", "uid": ""},
        {"type": "qmsg", "enabled": False, "server": "https://qmsg.zendee.cn", "key": "", "user": "", "bot": ""},
        {"type": "bark", "enabled": False, "server": "https://api.day.app", "send_key": ""},
        {"type": "gotify", "enabled": False, "server": "", "token": ""},
        {
            "type": "smtp",
            "enabled": False,
            "server": "smtp.qq.com",
            "port": 465,
            "use_ssl": True,
            "requires_auth": True,
            "username": "",
            "password": "",
            "sender": "",
            "to": "",
        },
        {
            "type": "webhook",
            "enabled": False,
            "url": "",
            "method": "POST",
            "content_type": "application/json",
            "headers": "",
            "body": '{"text": "{title}\n{content}"}',
        },
    ],
}


@dataclass
class NotificationSettings:
    """外部通知配置。

    通知正文 = 任务开始时间 + 任务过程中日志窗口展示的内容（focus 消息，
    可选叠加 Agent 自身日志）。
    """

    enabled: bool = False
    title_prefix: str = "[MaaLYSK]"
    send_on: list[str] = field(default_factory=lambda: [EVENT_ALL_TASK_COMPLETE])
    include_focus: bool = True
    include_agent_log: bool = True
    max_log_lines: int = 100
    entry_filter: list[str] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NotificationSettings":
        send_on = data.get("send_on")
        if not isinstance(send_on, list) or not send_on:
            send_on = [EVENT_ALL_TASK_COMPLETE]
        providers = data.get("providers")
        if not isinstance(providers, list):
            providers = []
        entry_filter = data.get("entry_filter")
        if not isinstance(entry_filter, list):
            entry_filter = []
        max_lines = data.get("max_log_lines", 100)
        if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines <= 0:
            max_lines = 100
        # include_details 是早期字段名，等价于 include_focus，保留兼容
        include_focus = data.get("include_focus")
        if include_focus is None:
            include_focus = data.get("include_details", True)
        return cls(
            enabled=bool(data.get("enabled", False)),
            title_prefix=str(data.get("title_prefix", "[MaaLYSK]")),
            send_on=[str(item) for item in send_on],
            include_focus=bool(include_focus),
            include_agent_log=bool(data.get("include_agent_log", True)),
            max_log_lines=max_lines,
            entry_filter=[str(item) for item in entry_filter],
            providers=[item for item in providers if isinstance(item, dict)],
        )

    def accepts(self, event: str) -> bool:
        return event in self.send_on

    def accepts_entry(self, entry: str) -> bool:
        """``entry_filter`` 为空表示不过滤；否则只通知列出的任务。"""
        if not self.entry_filter:
            return True
        return entry in self.entry_filter

    def active_providers(self) -> list[dict[str, Any]]:
        return [item for item in self.providers if item.get("enabled") is True]


def _config_path() -> Path:
    return get_runtime_paths().config_dir / CONFIG_FILENAME


def _ensure_example(path: Path) -> None:
    """配置文件不存在时，生成一份带注释性质的模板，避免用户从零猜字段。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps(EXAMPLE_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"【外部通知】已生成配置文件模板：{path}")
    except Exception as error:  # noqa: BLE001
        logger.warning(f"【外部通知】生成配置文件失败：{error}")


def load_settings() -> NotificationSettings:
    path = _config_path()
    _ensure_example(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是对象")
        return NotificationSettings.from_dict(data)
    except FileNotFoundError:
        return NotificationSettings()
    except Exception as error:  # noqa: BLE001
        logger.warning(f"【外部通知】读取 {path} 失败（{error}），通知功能按未启用处理")
        return NotificationSettings()


# --------------------------------------------------------------------------
# 服务（对应 MAA 的 ExternalNotificationService）
# --------------------------------------------------------------------------
class _AgentLogHandler(logging.Handler):
    """标准库 logging 的收集器（项目无 loguru 时走这条路径）。"""

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__(level=logging.INFO)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink(record.getMessage())
        except Exception:  # noqa: BLE001 - 绝不能因为收集日志而抛出
            pass


class NotificationService:
    """通知编排：读配置 -> 构造 Provider -> 并发发送。

    与 MAA 一致的点：
    * 单个渠道抛异常不影响其它渠道；
    * 发送是 fire-and-forget，不阻塞任务流水线。
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="maalysk-notify")
        self._lock = threading.Lock()
        self._settings: NotificationSettings | None = None
        self._settings_mtime: float = -1.0
        # task_id -> 日志行（带时间戳），内容即 GUI 日志窗口展示的东西
        self._logs: dict[int, list[str]] = {}
        self._current_task_id: int | None = None
        self._log_capture_attached = False
        self._loguru_sink_id: int | None = None

    # ---------------- 配置 ----------------
    def settings(self) -> NotificationSettings:
        """按文件 mtime 缓存，用户改完配置无需重启 Agent。"""
        try:
            mtime = _config_path().stat().st_mtime
        except OSError:
            mtime = -1.0
        with self._lock:
            if self._settings is None or mtime != self._settings_mtime:
                self._settings = load_settings()
                self._settings_mtime = mtime
            return self._settings

    def reload(self) -> NotificationSettings:
        with self._lock:
            self._settings = None
        return self.settings()

    # ---------------- 日志收集（对应 GUI 日志窗口的内容） ----------------
    def begin_log(self, task_id: int) -> None:
        """标记当前活跃任务，之后的日志都归到它名下。"""
        with self._lock:
            self._current_task_id = task_id
            self._logs.setdefault(task_id, [])

    def record_log(self, text: str, task_id: int | None = None) -> None:
        """追加一行日志。连续重复的行会折叠（rate_limit 循环会刷同一句话）。"""
        text = strip_html(text).strip()
        if not text or OWN_LOG_MARK in text:
            return  # 不把通知模块自己的日志收进通知里
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            target = task_id if task_id is not None else self._current_task_id
            if target is None:
                return
            lines = self._logs.setdefault(target, [])
            if lines and lines[-1].endswith(f"] {text}"):
                return
            lines.append(f"[{stamp}] {text}")

    def take_log(self, task_id: int) -> str:
        """取出并清空该任务的日志，超出 ``max_log_lines`` 时保留最近的若干行。"""
        with self._lock:
            lines = self._logs.pop(task_id, [])
            if self._current_task_id == task_id:
                self._current_task_id = None
        if not lines:
            return ""
        limit = self.settings().max_log_lines
        if len(lines) > limit:
            omitted = len(lines) - limit
            lines = lines[-limit:]
            lines.insert(0, f"（已省略前面 {omitted} 条）")
        return "\n".join(lines)

    def _log_capture_alive(self) -> bool:
        """检查收集器是否还挂着。

        ``utils.logger.change_console_level()`` 会调 ``logger.remove()`` / 清空
        handlers，把我们的 sink 一起干掉；这里检测到就重新挂载。
        判断失败时一律返回 True，宁可漏收也不重复挂载（重复会导致日志翻倍）。
        """
        try:
            if hasattr(logger, "_core"):  # loguru
                handlers = getattr(logger._core, "handlers", None)
                if isinstance(handlers, dict):
                    return self._loguru_sink_id in handlers
            elif hasattr(logger, "handlers"):  # 标准 logging
                return any(isinstance(handler, _AgentLogHandler) for handler in logger.handlers)
        except Exception:  # noqa: BLE001
            return True
        return True

    def attach_log_capture(self) -> None:
        """把 Agent 自身 logger 的输出也收进通知（可选，由 include_agent_log 控制）。

        兼容 loguru 与标准 logging：loguru 的 logger 有 ``add``，标准库没有。
        """
        if self._log_capture_attached and self._log_capture_alive():
            return
        try:
            if hasattr(logger, "add"):
                self._loguru_sink_id = logger.add(self.record_log, format="{message}", level="INFO")
            elif hasattr(logger, "addHandler"):
                logger.addHandler(_AgentLogHandler(self.record_log))
            self._log_capture_attached = True
        except Exception as error:  # noqa: BLE001 - 收集日志失败不影响主流程
            logger.debug(f"【外部通知】挂载日志收集失败：{error}")

    # ---------------- 发送 ----------------
    def send_sync(self, title: str, content: str, provider_configs: list[dict[str, Any]] | None = None, force: bool = False) -> dict[str, bool]:
        """同步发送，返回 {渠道名: 是否成功}。"""
        settings = self.settings()
        configs = provider_configs if provider_configs is not None else settings.active_providers()
        if not force and not settings.enabled and provider_configs is None:
            logger.debug(f"【外部通知】未启用，跳过发送（title={title}）")
            return {}
        if not configs:
            logger.info("【外部通知】没有已启用的通知渠道，跳过发送")
            return {}

        full_title = f"{settings.title_prefix} {title}".strip()
        results: dict[str, bool] = {}
        for config in configs:
            provider = build_provider(config)
            try:
                ok = bool(provider.send(full_title, content))
            except Exception as error:  # noqa: BLE001 - 渠道自身未捕获的异常
                logger.warning(f"【外部通知】渠道 {getattr(provider, 'name', '?')} 抛出异常：{error}")
                ok = False
            results[getattr(provider, "name", str(config.get("type", "?")))] = ok
            logger.info(f"【外部通知】{getattr(provider, 'name', '?')} {"发送成功" if ok else "发送失败"}")
        return results

    def send(self, title: str, content: str, provider_configs: list[dict[str, Any]] | None = None, force: bool = False) -> None:
        """异步发送，不阻塞流水线。"""
        self._executor.submit(self.send_sync, title, content, provider_configs, force)

    def is_event_enabled(self, event: str) -> bool:
        settings = self.settings()
        return settings.enabled and settings.accepts(event) and bool(settings.active_providers())


service = NotificationService()


# --------------------------------------------------------------------------
# 事件监听器：任务开始 / 结束自动通知
# --------------------------------------------------------------------------
@AgentServer.tasker_sink()
class NotificationSink(TaskerEventSink):
    """监听任务生命周期，在「任务开始 / 全部完成 / 失败」时发通知。

    对应 MAA 的 ``ExternalNotificationService.Event.AllTaskComplete``，
    并额外支持任务开始时刻的通知（MAA 没有这个时机）。

    MaaFW 的 ``Tasker.Task.*`` 对 ``context.run_task()`` 启动的子任务也会触发，
    这里用嵌套深度计数把子任务过滤掉，只在外层任务（depth 归零）时通知。
    """

    def __init__(self) -> None:
        self._depth = 0
        self._lock = threading.Lock()
        self._start_at: dict[int, datetime] = {}

    def _emit(self, event: str, detail: TaskerEventSink.TaskerTaskDetail, title: str, body: str) -> None:
        if not service.is_event_enabled(event):
            return
        if not service.settings().accepts_entry(detail.entry):
            return
        service.send(title, body)
        logger.info(f"【外部通知】已触发「{title}」通知（entry={detail.entry}）")

    def on_tasker_task(self, tasker: Tasker, noti_type: NotificationType, detail: TaskerEventSink.TaskerTaskDetail) -> None:
        try:
            if noti_type == NotificationType.Starting:
                started = datetime.now()
                with self._lock:
                    self._depth += 1
                    self._start_at[detail.task_id] = started
                # 只有最外层任务才开始一轮日志收集，子任务的内容会一并归入
                if self._depth == 1:
                    service.begin_log(detail.task_id)
                    if service.settings().include_agent_log:
                        service.attach_log_capture()
                    self._emit(
                        EVENT_TASK_STARTED,
                        detail,
                        "任务开始执行",
                        f"任务：{detail.entry}\n开始时间：{started.strftime('%Y-%m-%d %H:%M:%S')}",
                    )
                return

            with self._lock:
                self._depth = max(0, self._depth - 1)
                is_outer = self._depth == 0
                started = self._start_at.pop(detail.task_id, None)
            if not is_outer:
                return

            succeeded = noti_type == NotificationType.Succeeded
            event = EVENT_ALL_TASK_COMPLETE if succeeded else EVENT_TASK_FAILED

            finished = datetime.now()
            lines = [f"任务：{detail.entry}"]
            if started is not None:
                lines.append(f"开始时间：{started.strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"结束时间：{finished.strftime('%Y-%m-%d %H:%M:%S')}")
            if started is not None:
                lines.append(f"耗时：{_format_duration((finished - started).total_seconds())}")

            body = "\n".join(lines)
            if service.settings().include_focus:
                log = service.take_log(detail.task_id)
                if log:
                    body += f"\n\n---- 运行日志 ----\n{log}"
                    # 便于排查：真机上能直接看出 focus 到底收没收到
                    logger.info(f"【外部通知】本次收集到 {log.count(chr(10)) + 1} 行运行日志")
                else:
                    logger.info("【外部通知】本次未收集到 focus 日志（相关节点可能没配 focus）")
            else:
                # 关闭时也要清空缓冲，避免跨任务累积
                service.take_log(detail.task_id)

            title = "任务已全部完成" if succeeded else "任务执行失败"
            self._emit(event, detail, title, body)
        except Exception as error:  # noqa: BLE001 - sink 抛异常会打断 MaaFW 回调链
            logger.warning(f"【外部通知】任务事件处理异常：{error}")


@AgentServer.context_sink()
class FocusLogSink(ContextEventSink):
    """收集节点 ``focus`` 消息——即 GUI 日志窗口里展示的那部分内容。

    走 ``on_raw_notification`` 统一处理所有 ``Node.*`` 消息：只有在这里才能拿到
    完整的 ``details``（含 focus 与用于渲染占位符的字段）。

    一个节点通常只配置它关心的消息类型（比如只配 ``Node.PipelineNode.Failed``），
    按消息名精确取值天然不会重复；``display`` 不含 ``log`` 的会被跳过，
    因为它们本来也不会出现在日志窗口里。
    """

    def on_raw_notification(self, context: Context, msg: str, details: dict[str, Any]) -> None:
        try:
            if not msg.startswith("Node."):
                return
            settings = service.settings()
            if not settings.include_focus:
                return
            text = extract_focus(details.get("focus"), msg, details)
            if not text:
                return
            service.record_log(text, details.get("task_id"))
        except Exception as error:  # noqa: BLE001
            logger.debug(f"【外部通知】收集 focus 失败：{error}")


# --------------------------------------------------------------------------
# 自定义动作：在 pipeline 里显式发通知
# --------------------------------------------------------------------------
@AgentServer.custom_action("ExternalNotify")
class ExternalNotify(CustomAction):
    """在 Pipeline 节点中主动发送外部通知。

    业务侧想在特定时机（例如「体力已清空」「抽卡结果」）推送时，
    把本动作挂到对应节点即可，配置仍从 ``config/notification.json`` 读取。

    Examples:
        `custom_action_param`::

            {
                "title": "体力已清空",
                "content": "本次共消耗 240 体力",
                "details": true,
                "block": false
            }

    Args:
        title: 通知标题，默认 ``MaaLYSK 通知``。
        content: 通知正文。
        log: 是否附带本次任务到目前为止的运行日志（focus 消息），默认 ``false``。
        block: 是否同步等待发送完成（用于排查问题），默认 ``false`` 异步发送。
        providers: 可选，内联覆盖渠道配置（不走配置文件），一般用于测试。
        test: 测试模式，忽略配置文件里的 ``enabled`` 开关。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error(f"【外部通知】参数无效：{error}")
            return CustomAction.RunResult(success=False)

        title = str(params.get("title") or "MaaLYSK 通知")
        content = str(params.get("content") or "")

        task_id = argv.task_detail.task_id if argv.task_detail else None
        if params.get("log") and task_id is not None:
            log = service.take_log(task_id)
            if log:
                content = f"{content}\n\n---- 运行日志 ----\n{log}" if content else log

        provider_configs = params.get("providers")
        if not isinstance(provider_configs, list):
            provider_configs = None

        force = bool(params.get("test")) or provider_configs is not None

        if bool(params.get("block")):
            results = service.send_sync(title, content, provider_configs, force=force)
            if provider_configs is not None and not any(results.values()):
                return CustomAction.RunResult(success=False)
        else:
            service.send(title, content, provider_configs, force=force)

        return CustomAction.RunResult(success=True)
