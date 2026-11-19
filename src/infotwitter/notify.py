"""推送通道。P0 实现企业微信群机器人；预留 Server酱 / Bark / 飞书 适配位。"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class Notifier:
    name = "base"
    max_bytes = 2048
    content_type = "text"   # text | html

    def send(self, title: str, text: str) -> tuple[bool, str]:
        """title 由排版层显式给出（HTML 正文没有换行，不能从首行推断）。"""
        raise NotImplementedError


class PreviewNotifier(Notifier):
    """预览：保持真实通道的排版格式与长度上限，但只打印不发送。"""

    preview = True

    def __init__(self, inner: Notifier):
        self.name = inner.name
        self.content_type = inner.content_type
        self.max_bytes = inner.max_bytes

    def send(self, title: str, text: str) -> tuple[bool, str]:
        print("\n" + "=" * 64)
        print(f"[标题] {title}")
        print("-" * 64)
        print(text[:1500])
        if len(text) > 1500:
            print(f"…（共 {len(text)} 字符）")
        print("=" * 64 + f"\n[{len(text.encode('utf-8'))} bytes · {self.content_type}]")
        return True, "preview only"


class ConsoleNotifier(Notifier):
    name = "console"
    max_bytes = 2048

    def send(self, title: str, text: str) -> tuple[bool, str]:
        print("\n" + "=" * 64)
        print(f"[标题] {title}")
        print("-" * 64)
        print(text)
        print("=" * 64 + f"\n[{len(text.encode('utf-8'))} bytes]")
        return True, "printed to console"


class PushPlusNotifier(Notifier):
    """PushPlus（推送加）：把消息经微信公众号推到个人微信。

    接口：POST https://www.pushplus.plus/send
      {token, title, content, template}
    返回：{"code": 200, "msg": "请求成功"}
    限制（免费/实名用户）：每日 200 次、每分钟 5 次、相同内容每小时 3 条、
    标题 ≤100 字、内容 ≤2 万字。
    """

    name = "pushplus"
    # 内容上限 2 万字；按字节保守取 20000，中文约 6.7 千字，远低于限制
    max_bytes = 20000
    API = "https://www.pushplus.plus/send"

    CODE_HINT = {
        200: "成功",
        302: "未登录",
        401: "请求未授权",
        403: "请求 IP 未授权",
        500: "系统异常",
        600: "数据异常",
        805: "无权查看",
        888: "积分不足",
        900: "账号请求次数受限",
        903: "无效的用户令牌",
        905: "账号未实名认证",
        999: "服务端验证错误",
    }

    def __init__(
        self,
        token: str,
        template: str = "txt",
        proxy: str | None = None,
        timeout: float = 20.0,
    ):
        self.token = token.strip()
        self.template = template
        # content_type 必须跟随 template，否则 txt 模板会收到一整篇 HTML（见核查报告附加修复）
        self.content_type = "text" if str(template).lower() == "txt" else "html"
        self._client = httpx.Client(proxy=proxy, timeout=timeout)

    def send(self, title: str, text: str) -> tuple[bool, str]:
        if not self.token:
            return False, "未配置 PushPlus token"

        title = (title or "战锤日报").strip()[:100] or "战锤日报"
        payload = {
            "token": self.token,
            "title": title,
            "content": text,
            "template": self.template,
        }
        try:
            resp = self._client.post(self.API, json=payload)
            data = resp.json() if resp.content else {}
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

        code = data.get("code")
        if code == 200:
            return True, "ok"
        hint = self.CODE_HINT.get(code, "")
        return False, f"code={code}（{hint}）msg={data.get('msg')}"

    def close(self) -> None:
        self._client.close()


class WeComBotNotifier(Notifier):
    """企业微信群机器人。

    限制：单条 text 内容上限 2048 字节，每个机器人 20 条/分钟。
    微信插件开启后消息会同步到个人微信。
    """

    name = "wecom_bot"
    MAX_BYTES = 2048
    max_bytes = 2048

    def __init__(self, webhook: str, proxy: str | None = None, timeout: float = 20.0):
        self.webhook = webhook.strip()
        self._client = httpx.Client(proxy=proxy, timeout=timeout)

    def _post(self, payload: dict[str, Any]) -> tuple[bool, str]:
        try:
            resp = self._client.post(self.webhook, json=payload)
            data = resp.json() if resp.content else {}
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if data.get("errcode") == 0:
            return True, "ok"
        return False, f"errcode={data.get('errcode')} errmsg={data.get('errmsg')}"

    def send(self, title: str, text: str) -> tuple[bool, str]:
        if not self.webhook:
            return False, "未配置企业微信 webhook"
        if title and not text.startswith("【"):
            text = f"{title}\n{text}"
        data = text.encode("utf-8")
        if len(data) <= self.MAX_BYTES:
            return self._post({"msgtype": "text", "text": {"content": text}})

        # 超长拆分为多条，按行切分避免截断句子
        chunks: list[str] = []
        buf: list[str] = []
        size = 0
        for line in text.split("\n"):
            line_bytes = len(line.encode("utf-8")) + 1
            if size + line_bytes > self.MAX_BYTES - 10 and buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            buf.append(line)
            size += line_bytes
        if buf:
            chunks.append("\n".join(buf))

        results = []
        for idx, chunk in enumerate(chunks, 1):
            prefix = f"({idx}/{len(chunks)})\n" if len(chunks) > 1 else ""
            ok, msg = self._post({"msgtype": "text", "text": {"content": prefix + chunk}})
            results.append((ok, msg))
            if not ok:
                return False, f"第 {idx} 段失败: {msg}"
        return True, f"分 {len(chunks)} 段发送成功"

    def close(self) -> None:
        self._client.close()


def build_notifier(cfg: Any, force_preview: bool = False) -> Notifier:
    from .config import Config

    assert isinstance(cfg, Config)
    notifier = _build_real(cfg)
    if force_preview or cfg.get("push.dry_run"):
        return PreviewNotifier(notifier)
    return notifier


def _build_real(cfg: Any) -> Notifier:
    from .config import Config

    assert isinstance(cfg, Config)
    channel = str(cfg.get("push.channel", "pushplus")).lower()

    if channel == "pushplus":
        token = str(cfg.get("push.pushplus_token", "") or "").strip()
        if not token:
            log.warning("未配置 PushPlus token（config 或 .env），本次仅打印到控制台")
            return ConsoleNotifier()
        return PushPlusNotifier(
            token,
            template=str(cfg.get("push.pushplus_template", "txt")),
            proxy=cfg.proxy,
        )

    if channel == "wecom_bot":
        import os

        webhook = str(cfg.get("push.wecom_webhook", "") or "").strip() or os.environ.get(
            "WECOM_WEBHOOK", ""
        ).strip()
        if not webhook:
            log.warning("未配置企业微信 webhook（config 或 .env），本次仅打印到控制台")
            return ConsoleNotifier()
        return WeComBotNotifier(webhook, proxy=cfg.proxy)

    log.warning("未知推送渠道 %s，退回控制台", channel)
    return ConsoleNotifier()
