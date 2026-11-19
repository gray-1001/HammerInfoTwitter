"""配置加载与运行环境探测。"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def load_dotenv(path: Path | None = None) -> None:
    """读取项目根目录 .env（若存在）。不覆盖已存在的环境变量。"""
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def detect_system_proxy() -> str | None:
    """按 环境变量 → Windows 注册表 的顺序探测系统代理。

    httpx 只认环境变量，不读 Windows WinINET 设置，而本机代理只写在注册表里，
    所以必须显式探测，否则所有境外源都会握手失败。
    """
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key)
        if value:
            return value if "://" in value else f"http://{value}"

    if sys.platform != "win32":
        return None

    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None

    if not server:
        return None

    # 形如 "http=127.0.0.1:7897;https=127.0.0.1:7897" 或 "127.0.0.1:7897"
    if "=" in server:
        parts = dict(
            item.split("=", 1) for item in server.split(";") if "=" in item
        )
        host = parts.get("https") or parts.get("http")
    else:
        host = server

    if not host:
        return None
    return host if "://" in host else f"http://{host}"


def detect_proxy_bypass() -> list[str]:
    """读 Windows 的 ProxyOverride：这些主机不走代理（localhost / 内网段等）。"""
    if sys.platform != "win32":
        return []
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            override, _ = winreg.QueryValueEx(key, "ProxyOverride")
    except OSError:
        return []
    return [item.strip().lower() for item in str(override).split(";") if item.strip()]


def host_matches_bypass(host: str, patterns: list[str]) -> bool:
    """ProxyOverride 的匹配规则：<local> 与 *.example.com 这类通配。"""
    host = (host or "").lower().split(":")[0]
    if not host:
        return False
    for raw in patterns or []:
        pat = raw.lower()
        if pat in ("<local>", "<-loopback>"):
            if "." not in host or host.startswith("127.") or host == "localhost":
                return True
            continue
        if pat.startswith("*"):
            if host.endswith(pat[1:]):
                return True
        elif host == pat or host.endswith("." + pat):
            return True
    return False


def get_lan_ip() -> str | None:
    """取本机在局域网中的地址（不发包，仅让系统选路由）。"""
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
        return ip if ip and not ip.startswith("127.") else None
    except OSError:
        return None


class Config:
    """配置访问器，支持 config.get("a.b.c", default) 形式的点号路径。

    少数关键项允许用环境变量覆盖（便于测试与部署时不改文件）：
      INFOTWITTER_LLM_PROVIDER -> llm.provider
      INFOTWITTER_LLM_BASE_URL -> llm.base_url
      INFOTWITTER_LLM_MODEL    -> llm.model
      PUSHPLUS_TOKEN           -> push.pushplus_token
      WECOM_WEBHOOK            -> push.wecom_webhook
    """

    ENV_OVERRIDES = {
        "llm.provider": "INFOTWITTER_LLM_PROVIDER",
        "llm.base_url": "INFOTWITTER_LLM_BASE_URL",
        "llm.model": "INFOTWITTER_LLM_MODEL",
        "push.wecom_webhook": "WECOM_WEBHOOK",
        "push.pushplus_token": "PUSHPLUS_TOKEN",
    }

    def __init__(self, data: dict[str, Any], sources: list[dict[str, Any]]):
        self._data = data
        self.sources = sources

        app = data.get("app", {})
        self.db_path = ROOT / app.get("db_path", "data/infotwitter.db")
        self.log_dir = ROOT / app.get("log_dir", "logs")
        self.report_dir = ROOT / app.get("report_dir", "reports")
        self.timezone = app.get("timezone", "Asia/Shanghai")

        net = data.get("network", {})
        proxy_cfg = str(net.get("proxy", "auto")).strip().lower()
        if proxy_cfg in ("auto", ""):
            self.proxy = detect_system_proxy()
        elif proxy_cfg in ("none", "off", "false", "direct"):
            self.proxy = None
        else:
            self.proxy = net["proxy"]
        # 系统代理的例外列表（ProxyOverride）：命中这些主机的请求应直连
        self.proxy_bypass = detect_proxy_bypass() if proxy_cfg in ("auto", "") else []
        self.timeout = float(net.get("timeout", 30))
        self.retries = int(net.get("retries", 3))
        self.user_agent = net.get("user_agent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        # 存储层固定按 UTC+8 处理，配置成别的时区不会生效——与其静默忽略，不如告警
        if str(self.timezone) not in ("Asia/Shanghai", "Asia/Chongqing", "UTC+8", "+08:00"):
            log.warning(
                "app.timezone=%s 不生效：存储与调度固定按 Asia/Shanghai（UTC+8）处理",
                self.timezone,
            )

    def get(self, dotted: str, default: Any = None) -> Any:
        env_name = self.ENV_OVERRIDES.get(dotted)
        if env_name:
            value = os.environ.get(env_name)
            if value:
                return value
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # --- 常用快捷访问 ---
    @property
    def collect_hours(self) -> int:
        return int(self.get("windows.collect_hours", 48))

    @property
    def max_collect_hours(self) -> int:
        """所有启用源里最大的采集窗口。

        中文源更新频率低（实测最新一条可能在一周前），单独放宽到 168h；
        分析窗口和聚类窗口必须跟得上，否则放宽了采集也进不了流水线。
        """
        base = self.collect_hours
        extra = [
            int(s.get("collect_hours") or 0)
            for s in self.sources
            if s.get("active", True)
        ]
        return max([base, *extra] or [base])

    @property
    def cn_slot_days(self) -> int:
        return int(self.get("windows.cn_slot_days", 3))

    @property
    def push_hours(self) -> int:
        return int(self.get("windows.push_hours", 24))

    @property
    def backlog_days(self) -> int:
        return int(self.get("windows.backlog_days", 7))

    @property
    def llm_provider(self) -> str:
        return str(self.get("llm.provider", "none")).lower()

    @property
    def llm_model(self) -> str:
        return str(self.get("llm.model", "deepseek-chat"))

    @property
    def llm_base_url(self) -> str:
        return str(self.get("llm.base_url", "https://api.deepseek.com"))

    @property
    def llm_api_key(self) -> str:
        env_name = str(self.get("llm.api_key_env", "DEEPSEEK_API_KEY"))
        return os.environ.get(env_name, "").strip()

    @property
    def rubric_version(self) -> str:
        return str(self.get("scoring.rubric_version", "importance@v1"))

    def report_base_url(self) -> str:
        """日报页的外部可访问地址。

        默认 auto：用局域网 IP，这样手机连同一 WiFi 就能打开推送里的链接；
        127.0.0.1 在手机上必然是空白页。也可在配置里写死域名（配合内网穿透）。
        """
        configured = str(self.get("app.report_base_url", "auto") or "auto").strip()
        port = int(self.get("app.report_port", 8080))
        if configured and configured.lower() not in ("auto", ""):
            return configured.rstrip("/")
        host = get_lan_ip() or "127.0.0.1"
        return f"http://{host}:{port}"

    def active_sources(self) -> list[dict[str, Any]]:
        out = []
        for src in self.sources:
            if src.get("active", True):
                out.append(src)
        return out

    def as_dict(self) -> dict[str, Any]:
        return self._data


def load_config(config_path: Path | None = None, sources_path: Path | None = None) -> Config:
    load_dotenv()
    cfg_path = config_path or (CONFIG_DIR / "config.yaml")
    src_path = sources_path or (CONFIG_DIR / "sources.yaml")
    data = _read_yaml(cfg_path)
    src_doc = _read_yaml(src_path)
    sources = src_doc.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("sources.yaml 中 sources 必须是列表")
    return Config(data, sources)
