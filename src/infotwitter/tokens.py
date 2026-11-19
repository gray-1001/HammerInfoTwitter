"""轻量签名工具：给反馈接口签发/校验按天轮换的令牌。

日报页是静态 HTML，服务在局域网内监听，端口对同网段可达。为了不让
「随便一个页面就能 POST /api/feedback 把兴趣权重刷满」，页面里带一个
按天轮换的令牌，服务端校验后才写库。

这不是强鉴权（同网段能读到页面就能拿到令牌），但足以挡住跨站 CSRF
与无意扫描；真正的防刷靠 feedback_dedup 的唯一约束。
"""
from __future__ import annotations

import hashlib
import hmac
import os

_DEFAULT_SALT = "infotwitter-feedback-v1"


def _salt() -> str:
    """每次调用都读环境变量。

    不能在 import 期固化：`load_dotenv()` 发生在 `load_config()` 里，
    而导入链（run.py → cli → runner → render → tokens）比它更早，
    所以在模块顶层读到的永远是进程启动时的空值，`.env` 里配的盐形同虚设（D-06）。
    """
    return os.environ.get("INFOTWITTER_FEEDBACK_SALT") or _DEFAULT_SALT


def feedback_token(day: str) -> str:
    salt = _salt().encode("utf-8")
    return hmac.new(salt, day.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def verify_feedback_token(day: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(feedback_token(day), str(token))
