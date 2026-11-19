"""DeepSeek 调用层：特征抽取 + 翻译 + 摘要 + claim 抽取，一次调用完成。

设计要点：
- LLM 只做「结构化抽取」，不做打分。打分由 scoring.py 按固定权重计算，保证可回放。
- provider=none 时全部退化为规则模式，流水线仍可跑通（零成本验证链路）。
- 每日 token 预算超限后自动降级为规则模式，避免费用失控。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from .config import Config

log = logging.getLogger(__name__)

EVENT_TYPES = [
    "edition",   # 新版本/新战棋规则发布
    "rules",     # 规则更新、平衡补丁、Codex
    "mini",      # 模型/套装新发售
    "novel",     # 小说/出版
    "film",      # 影视
    "game",      # 电子游戏
    "business",  # 财报、股价、公司动态
    "event",     # 赛事、展会、活动
    "rumor",     # 传闻
    "other",
]

CATEGORIES = ["40k", "30k", "aos", "tow", "other"]

SYSTEM_PROMPT = """你是战锤（Warhammer）领域的情报分析助手。你只输出 JSON，不输出任何解释文字。
你的任务是阅读一条原始信息（标题+摘要），抽取结构化字段。严禁编造原文中不存在的事实。
所有 claims 的 evidence 字段必须是原文（title 或 body）中真实出现的连续片段；找不到依据就不要输出该 claim。"""

USER_TEMPLATE = """请分析下面这条战锤领域的信息，输出 JSON。

标题：{title}
来源：{source_name}（可信层级 {tier}）
语言：{lang}
正文摘要：{body}

输出严格的 JSON 对象，字段定义：
{{
  "title_zh": "中文标题，保留专有名词英文原名，不超过 40 字",
  "event_type": "从 {event_types} 中选一个",
  "category": "从 {categories} 中选一个，判断属于哪条产品线",
  "impact_scope": "global 或 cn_community；只有当这条信息的意义仅限中文社区（如中文版引进、国内开店、国内赛事）时才是 cn_community",
  "magnitude": 0-5 的整数，事件量级。按「这条消息对读者决策的价值」打分，而不是按文章体裁：5=改变整个产品线（新版本发布/新游戏发售或立项/影视官宣/财报级公司变动）；4=重要单品或路线图（新 Codex 或 Battletome/主力游戏大版本或路线图/重点模型套装首次公开/官方平衡数据板）；3=常规官方公告（新品预售开启/单款新模型公布/官方活动与赛事）；2=二手报道与分析（媒体转述/评测/攻略/发售日历）；1=社区内容（玩家讨论/涂装分享/战报/同人）；0=与战锤无关或纯广告,
  "actionability": 0-5 的整数，读者看完能立刻做什么：5=马上可预购/下载/报名，3=需观望，0=纯信息,
  "summary": "中文 2-3 句，80-110 字。第一句必须讲清「发生了什么」：谁 + 做了什么 + 至少一个关键数字（日期/价格/数量/版本号/阵营名）；第二句讲对读者的实际影响（要不要买、规则怎么变、什么时候能玩到）。只写原文里有的事实。严禁出现「标题暗示」「正文缺失」「无法判断」「有待确认」这类描述信息缺失的句子，也严禁空泛概括（如「带来新内容」「值得期待」「引发关注」）",
  "why_matters": "中文一句话，不超过 25 字，说明读者为什么要在意。不要与 summary 重复",
  "quote_en": "原文中最关键的一句话（原文语言，不超过 200 字符，必须逐字来自标题或正文）",
  "quote_zh": "上面这句话的中文翻译；若原文就是中文则原样返回；不要解释",
  "entities": ["涉及的阵营/产品/公司/人物名，最多 6 个"],
  "claims": [{{"text": "可验证的原子主张", "type": "price|date|product|event|statement", "evidence": "原文中支撑该主张的连续片段"}}],
  "is_rumor": true/false
}}

若这条信息明显是传闻、泄露或未经证实，is_rumor 设为 true。"""


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip().strip("「」\"'“”")


def evidence_supported(evidence: str, haystack: str) -> bool:
    """claim 的 evidence 必须能在原文里逐字找到（允许空白归一）。

    设计 §6.2 的硬约束只写进了提示词，模型仍会编造引文；这里是程序侧的最后一道闸
    （核查报告 BUG-04）。
    """
    ev = _norm_ws(evidence)
    if not ev:
        return False
    return ev in _norm_ws(haystack)


def _safe_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


class LLMClient:
    def __init__(self, cfg: Config):
        self.provider = cfg.llm_provider
        self.model = cfg.llm_model
        self.base_url = cfg.llm_base_url.rstrip("/")
        self.api_key = cfg.llm_api_key
        self.budget = int(cfg.get("llm.daily_token_budget", 800000))
        self.temperature = float(cfg.get("llm.temperature", 0.2))
        self.max_chars = int(cfg.get("llm.max_input_chars", 6000))
        self.concurrency = int(cfg.get("llm.concurrency", 4))
        self.retries = max(1, int(cfg.get("llm.retries", 3)))
        self.tokens_used = 0
        self.calls = 0
        self.failures = 0
        self._lock = threading.Lock()
        self._client = httpx.Client(
            proxy=cfg.proxy,
            timeout=120.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    @property
    def enabled(self) -> bool:
        if self.provider in ("none", "", "off"):
            return False
        if self.provider == "deepseek" and not self.api_key:
            log.error("未设置 DEEPSEEK_API_KEY，降级为规则模式")
            return False
        return True

    @property
    def budget_left(self) -> bool:
        return self.tokens_used < self.budget

    def _chat(self, messages: list[dict[str, str]], json_mode: bool = True) -> str | None:
        if not self.budget_left:
            return None
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": 1200,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_err = ""
        for attempt in range(1, self.retries + 1):
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=payload)
                if resp.status_code < 400:
                    data = resp.json()
                    usage = data.get("usage") or {}
                    total = int(usage.get("total_tokens") or 0)
                    with self._lock:
                        self.tokens_used += total
                        self.calls += 1
                    return data["choices"][0]["message"]["content"]

                last_err = f"HTTP {resp.status_code}: {resp.text[:160]}"
                # 4xx（除 429）重试也没用：请求本身不合法
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    log.error("LLM %s", last_err)
                    with self._lock:
                        self.failures += 1
                    return None
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
            log.warning("LLM 调用失败（第 %d/%d 次）：%s", attempt, self.retries, last_err[:120])
            if attempt < self.retries:
                time.sleep(min(2.0 * attempt, 8.0))

        log.error("LLM 调用放弃：%s", last_err[:160])
        with self._lock:
            self.failures += 1
        return None

    def analyze(self, doc: dict[str, Any]) -> dict[str, Any] | None:
        body = (doc.get("body") or "")[: self.max_chars]
        user = USER_TEMPLATE.format(
            title=doc.get("title") or "",
            source_name=doc.get("source_name") or doc.get("source_id") or "",
            tier=doc.get("tier") or "L2",
            lang=doc.get("lang") or "en",
            body=body or "（无正文，仅有标题）",
            event_types=", ".join(EVENT_TYPES),
            categories=", ".join(CATEGORIES),
        )
        content = self._chat(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
        )
        if not content:
            return None
        data = _safe_json(content)
        if not data:
            log.warning("LLM 返回非 JSON: %s", content[:150])
            return None
        return self._normalize(data, doc)

    @staticmethod
    def _normalize(data: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
        def clamp(value: Any, default: float = 2.0) -> float:
            try:
                return max(0.0, min(5.0, float(value)))
            except (TypeError, ValueError):
                return default

        event_type = str(data.get("event_type") or "other").lower()
        if event_type not in EVENT_TYPES:
            event_type = "other"
        category = str(data.get("category") or "other").lower()
        if category not in CATEGORIES:
            category = "other"
        scope = str(data.get("impact_scope") or "global").lower()
        if scope not in ("global", "cn_community"):
            scope = "global"

        claims = []
        haystack = f"{doc.get('title') or ''}\n{doc.get('body') or ''}"
        dropped = 0
        for claim in data.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("text") or "").strip()
            evidence = str(claim.get("evidence") or "").strip()
            if not text or not evidence:
                continue
            if not evidence_supported(evidence, haystack):
                # 引文在原文里找不到 → 模型编造，整条丢弃
                dropped += 1
                continue
            claims.append(
                {
                    "text": text,
                    "type": str(claim.get("type") or "statement"),
                    "evidence": evidence,
                }
            )
        if dropped:
            log.warning("丢弃 %d 条无原文依据的 claim（%s）", dropped, (doc.get("title") or "")[:40])

        entities = [str(e) for e in (data.get("entities") or []) if e][:6]

        return {
            "title_zh": str(data.get("title_zh") or doc.get("title") or "")[:120],
            "event_type": event_type,
            "category": category,
            "impact_scope": scope,
            "magnitude": clamp(data.get("magnitude"), 2.0),
            "actionability": clamp(data.get("actionability"), 1.0),
            "summary": str(data.get("summary") or "")[:300],
            "why_matters": str(data.get("why_matters") or "")[:120],
            "quote_en": str(data.get("quote_en") or "")[:300],
            "quote_zh": str(data.get("quote_zh") or "")[:300],
            "entities": entities,
            "claims": claims,
            "is_rumor": bool(data.get("is_rumor")),
        }

    def analyze_batch(
        self, docs: list[dict[str, Any]], progress: bool = True
    ) -> dict[int, dict[str, Any]]:
        """并发分析，返回 {doc_index: 抽取结果}。失败的条目不在结果中。"""
        results: dict[int, dict[str, Any]] = {}
        if not self.enabled or not docs:
            return results

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self.analyze, doc): idx for idx, doc in enumerate(docs)}
            done = 0
            for fut in as_completed(futures):
                idx = futures[fut]
                done += 1
                try:
                    data = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.error("分析失败: %s", exc)
                    data = None
                if data:
                    results[idx] = data
                if progress and done % 10 == 0:
                    log.info("LLM 分析进度 %d/%d（tokens=%d）", done, len(docs), self.tokens_used)
        return results

    def stats(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "calls": self.calls,
            "failures": self.failures,
            "tokens": self.tokens_used,
            "budget": self.budget,
        }

    def close(self) -> None:
        self._client.close()


def fallback_analysis(doc: dict[str, Any]) -> dict[str, Any]:
    """规则模式的抽取结果：走 rules.classify 给出真实的事件类型与量级。

    不翻译、不摘要，但排序依据是真实的——这保证没有 LLM 时日报仍然可用，
    而不是全部打成 other 后一片空白。
    """
    from .rules import classify

    title = doc.get("title") or ""
    judged = classify(title, doc.get("body") or "", doc.get("lang") or "en")
    return {
        "title_zh": "",
        "event_type": judged["event_type"],
        "category": judged["category"],
        "impact_scope": judged["impact_scope"],
        "magnitude": judged["magnitude"],
        "actionability": judged["actionability"],
        "summary": "",
        "why_matters": "",
        "quote_en": title[:200],
        "entities": [],
        "claims": [],
        "is_rumor": judged["is_rumor"],
        "fallback": True,
    }


class MockLLMClient:
    """离线模拟 LLM，用于零成本验证评分与选槽逻辑。

    抽取规则与真实降级模式（rules.classify）一致，只额外伪造翻译与摘要，
    以便在没有任何 key 的情况下检查「抽取→打分→选槽→排版」整条链路。
    """

    def __init__(self) -> None:
        self.provider = "mock"
        self.model = "mock-rules-v1"
        self.tokens_used = 0
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    def analyze(self, doc: dict[str, Any]) -> dict[str, Any]:
        from .rules import classify, describe

        title = doc.get("title") or ""
        judged = classify(title, doc.get("body") or "", doc.get("lang") or "en")
        self.calls += 1
        return {
            "title_zh": f"[模拟]{title[:36]}",
            "event_type": judged["event_type"],
            "category": judged["category"],
            "impact_scope": judged["impact_scope"],
            "magnitude": judged["magnitude"],
            "actionability": judged["actionability"],
            "summary": f"{title[:80]}（模拟摘要）",
            "why_matters": f"模拟判断：{describe(judged['event_type'])}",
            "quote_en": title[:200],
            "entities": [],
            "claims": [{"text": title[:120], "type": "statement", "evidence": title[:120]}] if title else [],
            "is_rumor": judged["is_rumor"],
        }

    def analyze_batch(self, docs: list[dict[str, Any]], progress: bool = True) -> dict[int, dict[str, Any]]:
        return {i: self.analyze(doc) for i, doc in enumerate(docs)}

    def stats(self) -> dict[str, Any]:
        return {
            "provider": "mock", "model": self.model, "calls": self.calls,
            "failures": 0, "tokens": 0, "budget": 0,
        }

    def close(self) -> None:
        pass


def build_llm(cfg: Config) -> Any:
    """按配置构造 LLM 客户端。"""
    if cfg.llm_provider == "mock":
        log.warning("使用 mock LLM（离线模拟，仅用于链路验证）")
        return MockLLMClient()
    return LLMClient(cfg)
