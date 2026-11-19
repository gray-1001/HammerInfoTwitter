"""带代理探测、重试、熔断的 HTTP 客户端。

本机代理（Clash 类）存在间歇性 TLS 握手失败（SSL: UNEXPECTED_EOF_WHILE_READING），
实测表现为成段故障后自行恢复。因此：
- 连接超时压短（失败快速返回，而不是干等 30s）
- 对幂等 GET 做多次重试 + 退避抖动
- 同域名**连续请求**失败 N 次后熔断一段时间，冷却后半开放行，避免拖垮整体耗时

熔断计数的单位是「请求」而不是「重试尝试」：一个请求内部重试 3 次就把域名判死，
等于一次抖动带走半个采集面（核查报告 BUG-08）。
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import host_matches_bypass

log = logging.getLogger(__name__)


def url_host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""


def _retry_after_seconds(value: str | None) -> float | None:
    """Retry-After 既可能是秒数，也可能是 HTTP-date（RFC 7231）。

    只按 float 解析会把日期型整条忽略掉（服务端明确要求等 120s，我们却 1.2s 就重试）。
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class Fetcher:
    def __init__(
        self,
        proxy: str | None,
        timeout: float = 30.0,
        connect_timeout: float = 8.0,
        retries: int = 4,
        user_agent: str = "",
        breaker_threshold: int = 3,
        breaker_cooldown: float = 90.0,
        proxy_bypass: list[str] | None = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.breaker_threshold = max(1, breaker_threshold)
        self.breaker_cooldown = max(0.0, breaker_cooldown)
        self.proxy_bypass = proxy_bypass or []
        self._last_hit: dict[str, float] = {}
        self._host_fails: dict[str, int] = {}
        self._dead_until: dict[str, float] = {}
        self.skipped_by_breaker: set[str] = set()
        self._client = self._build_client(proxy, timeout, connect_timeout, user_agent)
        # 命中 ProxyOverride 的主机直连（BUG-49）：httpx 的 client 级 proxy 无法按主机切换，
        # 因此额外准备一个直连 client，按需使用。
        self._direct_client: httpx.Client | None = None

    def _build_client(
        self, proxy: str | None, timeout: float, connect_timeout: float, user_agent: str
    ) -> httpx.Client:
        """建主 client。SOCKS 代理缺 socksio 时降级直连，而不是让整轮采集崩掉。

        `ALL_PROXY=socks5://…` 这种环境变量会让 httpx.Client 在构造阶段抛 ImportError，
        而这个构造发生在 collect() 的 per-source try 之外——一个环境变量就能让所有源不采（C-12）。
        """
        kwargs = {
            "timeout": httpx.Timeout(
                connect=connect_timeout, read=timeout, write=timeout, pool=10.0
            ),
            "follow_redirects": True,
            "headers": {"User-Agent": user_agent,
                        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"},
            "limits": httpx.Limits(max_connections=8, max_keepalive_connections=4),
        }
        if proxy:
            try:
                return httpx.Client(proxy=proxy, **kwargs)
            except ImportError as exc:
                log.error(
                    "代理 %s 需要额外依赖（%s），本次改为直连；"
                    "如需 SOCKS 请 pip install 'httpx[socks]'", proxy, exc,
                )
                self.proxy = None
            except ValueError as exc:
                log.error("代理地址无效（%s），本次改为直连", exc)
                self.proxy = None
        return httpx.Client(**kwargs)

    def _client_for(self, host: str) -> httpx.Client:
        if self.proxy and host_matches_bypass(host, self.proxy_bypass):
            if self._direct_client is None:
                self._direct_client = httpx.Client(
                    timeout=httpx.Timeout(
                        connect=8.0, read=self.timeout, write=self.timeout, pool=10.0
                    ),
                    follow_redirects=True,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                    },
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                )
            return self._direct_client
        return self._client

    # ---------- 熔断（供其它采集路径共用） ----------
    def should_skip_host(self, host: str) -> bool:
        host = (host or "").lower()
        if not host:
            return False
        until = self._dead_until.get(host)
        if until is None:
            return False
        if time.time() >= until:
            # 半开：清掉熔断，放行一次探测；成功即复活，失败会重新熔断
            self._dead_until.pop(host, None)
            self._host_fails[host] = max(0, self.breaker_threshold - 1)
            log.info("域名 %s 冷却结束，放行探测", host)
            return False
        return True

    def mark_host_fail(self, host: str) -> None:
        host = (host or "").lower()
        if not host:
            return
        fails = self._host_fails.get(host, 0) + 1
        self._host_fails[host] = fails
        if fails >= self.breaker_threshold and host not in self._dead_until:
            self._dead_until[host] = time.time() + self.breaker_cooldown
            log.error(
                "域名 %s 连续失败 %d 次，熔断 %.0fs", host, fails, self.breaker_cooldown
            )

    def mark_host_ok(self, host: str) -> None:
        host = (host or "").lower()
        if host:
            self._host_fails[host] = 0
            self._dead_until.pop(host, None)

    def _throttle(self, host: str, min_gap: float) -> None:
        last = self._last_hit.get(host, 0.0)
        wait = min_gap - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.time()

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        min_gap: float = 1.5,
        read_timeout: float | None = None,
    ) -> httpx.Response | None:
        """GET 一个 URL。read_timeout 用于给 RSS/JSON 这类小响应单独压短超时。"""
        host = url_host(url)
        if self.should_skip_host(host):
            self.skipped_by_breaker.add(host)
            log.warning("跳过熔断中的域名 %s（%s）", host, url[:70])
            return None

        self._throttle(host, min_gap)
        delay = 1.2
        last_err: Exception | None = None
        gave_up = False
        req_timeout = (
            httpx.Timeout(connect=min(8.0, read_timeout), read=read_timeout,
                          write=read_timeout, pool=10.0)
            if read_timeout else None
        )

        for attempt in range(1, self.retries + 1):
            try:
                resp = self._client_for(host).get(
                    url, headers=headers or {}, timeout=req_timeout
                )
                if resp.status_code < 400:
                    self.mark_host_ok(host)
                    return resp
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                    hint = f" Retry-After={retry_after:.0f}s" if retry_after else ""
                    log.warning(
                        "HTTP %s on %s（第 %d 次）%s", resp.status_code, url[:80], attempt, hint
                    )
                    if retry_after:
                        delay = max(delay, min(retry_after, 30.0))
                else:
                    log.warning("HTTP %s on %s", resp.status_code, url[:80])
                    return resp  # 4xx 之类重试也没用
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                detail = str(exc) or type(exc).__name__
                log.warning("请求失败 %s（第 %d 次）：%s", url[:70], attempt, detail[:110])

            if attempt < self.retries:
                time.sleep(delay + random.uniform(0, 0.6))
                delay = min(delay * 1.8, 8.0)
            else:
                gave_up = True

        # 一次请求彻底失败，才计一次熔断计数
        self.mark_host_fail(host)
        if gave_up:
            log.error("放弃 %s：%s", url[:70], (str(last_err)[:100] if last_err else "HTTP 错误"))
        return None

    def stats(self) -> dict[str, Any]:
        return {
            "dead_hosts": sorted(self._dead_until),
            "skipped_by_breaker": sorted(self.skipped_by_breaker),
            "proxy": self.proxy,
        }

    def close(self) -> None:
        self._client.close()
        if self._direct_client is not None:
            self._direct_client.close()


def make_fetcher(cfg: Any) -> Fetcher:
    return Fetcher(
        proxy=cfg.proxy,
        timeout=cfg.timeout,
        connect_timeout=float(cfg.get("network.connect_timeout", 8)),
        retries=cfg.retries,
        user_agent=cfg.user_agent,
        breaker_threshold=int(cfg.get("network.breaker_threshold", 3)),
        breaker_cooldown=float(cfg.get("network.breaker_cooldown", 90)),
        proxy_bypass=getattr(cfg, "proxy_bypass", []) or [],
    )
