-- 战锤信息日报 · 数据库 schema
-- 与 docs/00-需求与设计.md §4 保持一致

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------- 源注册表 ----------
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    domain        TEXT NOT NULL DEFAULT 'warhammer',
    category      TEXT NOT NULL,          -- official|media|community|rumor|marketplace|cn_community
    tier          TEXT NOT NULL,          -- L0|L1|L2|L3
    lang          TEXT NOT NULL,          -- en|zh
    fetch_method  TEXT NOT NULL,          -- rss|googlenews|browser|html|manual|bilibili
    weight        REAL NOT NULL DEFAULT 1.0,
    active        INTEGER NOT NULL DEFAULT 1,
    last_ok_at    TEXT,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    fake_reports  INTEGER NOT NULL DEFAULT 0,  -- 被用户标记「假的」的次数，>=3 时降一级
    note          TEXT
);

-- ---------- 原始抓取 ----------
CREATE TABLE IF NOT EXISTS raw_docs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL REFERENCES sources(id),
    url           TEXT NOT NULL,
    title         TEXT,
    body          TEXT,
    lang          TEXT,
    published_at  TEXT,               -- 原文发布时间（部分源只能取到抓取时间）
    fetched_at    TEXT NOT NULL,      -- 本次抓取时间
    discovered_at TEXT,               -- 首次入库时间：推送窗口与「首次出现」以此为准
    content_hash  TEXT NOT NULL,
    derived_tier  TEXT,               -- 按真实发布域名二次定级（Google News 聚合条目用）
    publisher_domain TEXT,            -- 真实发布域名：判「独立来源」用，不能拿 source_id 顶
    time_source   TEXT DEFAULT 'feed',-- article=列表页给的发布时间；fetch=只能取到抓取时间
    analysis      TEXT,               -- LLM 抽取结果 JSON（一次抽取长期复用，避免重复计费）
    analyzed_at   TEXT,
    UNIQUE(source_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_raw_fetched ON raw_docs(fetched_at);
CREATE INDEX IF NOT EXISTS idx_raw_published ON raw_docs(published_at);

-- ---------- 事件（聚类后） ----------
CREATE TABLE IF NOT EXISTS stories (
    id                 TEXT PRIMARY KEY,
    canonical_title    TEXT NOT NULL,
    title_zh           TEXT,
    event_type         TEXT NOT NULL DEFAULT 'other',
    entities           TEXT,               -- JSON
    category           TEXT NOT NULL DEFAULT 'other',  -- 40k|30k|aos|tow|other
    impact_scope       TEXT NOT NULL DEFAULT 'global',
    first_seen_at      TEXT NOT NULL,      -- 首次入库时间（推送窗口依据）
    published_at       TEXT,               -- 原文发布时间（时效计分依据）
    last_seen_at       TEXT NOT NULL,
    source_ids         TEXT,               -- JSON array
    authenticity       TEXT NOT NULL DEFAULT 'L2',
    authenticity_note  TEXT,
    confidence         TEXT,               -- 徽章文案：官方一手/多源印证/单一来源/未证实
    claim_conflict     INTEGER NOT NULL DEFAULT 0,  -- claim 之间出现矛盾
    importance_total   REAL,
    score_breakdown    TEXT,               -- JSON
    rubric_version     TEXT,
    summary            TEXT,
    why_matters        TEXT,
    quote_en           TEXT,
    quote_zh           TEXT,
    primary_url        TEXT,
    status             TEXT NOT NULL DEFAULT 'new'  -- new|scored|pushed|skipped|expired
);
CREATE INDEX IF NOT EXISTS idx_story_status ON stories(status);
CREATE INDEX IF NOT EXISTS idx_story_first_seen ON stories(first_seen_at);

-- ---------- 事件与原始文档的归属链 ----------
-- story_id 不能再由「簇内最早那篇」算出来：簇成员一变，id 就变，台账去重失效、
-- 旧行变成孤儿。改为记录「某个 raw_doc 属于哪个 story」，下一次聚类时沿这条链复用 id。
CREATE TABLE IF NOT EXISTS story_docs (
    raw_doc_id INTEGER PRIMARY KEY,        -- 一篇文档在一个时刻只归属一个 story
    story_id   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_story_docs_story ON story_docs(story_id);

-- ---------- 原子主张 ----------
CREATE TABLE IF NOT EXISTS claims (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id      TEXT NOT NULL REFERENCES stories(id),
    claim_text    TEXT NOT NULL,
    claim_type    TEXT,
    raw_doc_id    INTEGER REFERENCES raw_docs(id),
    evidence_span TEXT,
    support_count INTEGER NOT NULL DEFAULT 1,
    contradict    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claim_story ON claims(story_id);

-- ---------- 反馈去重（同一 story 同一信号每天只计一次）----------
CREATE TABLE IF NOT EXISTS feedback_dedup (
    story_id   TEXT NOT NULL,
    signal     TEXT NOT NULL,
    day        TEXT NOT NULL,
    PRIMARY KEY (story_id, signal, day)
);

-- ---------- 评分卡版本 ----------
CREATE TABLE IF NOT EXISTS rubrics (
    version    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 注：早期的 skus / price_snapshots 两张表已在 2026-11-19 移除。
-- 它们被 products / price_points 取代（价格模块定稿后一直为空表），
-- 保留只会让人以为还有两套商品数据。

-- ---------- 商品主表（价格模块）----------
CREATE TABLE IF NOT EXISTS products (
    id            TEXT PRIMARY KEY,       -- GW 货号，如 39-13
    name_en       TEXT NOT NULL,
    name_zh       TEXT,
    barcode       TEXT,
    product_codes TEXT,                   -- JSON 数组，同一商品的多个内包装编码
    product_line  TEXT,                   -- 40k|30k|aos|tow|other
    category      TEXT,                   -- boxed|codex|mini|book|paint|other
    status        TEXT DEFAULT 'active',  -- preorder|new|active|oop
    first_seen_at TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);
CREATE INDEX IF NOT EXISTS idx_products_line ON products(product_line);

-- 中文名翻译记录：每次翻译留一条，便于回溯「这条中文名是哪版模型/术语表产的」
CREATE TABLE IF NOT EXISTS name_translations (
    product_id  TEXT NOT NULL,
    name_en     TEXT NOT NULL,
    name_zh     TEXT NOT NULL,
    source      TEXT,                -- 模型标识，如 deepseek/deepseek-chat
    glossary_ver TEXT,               -- 术语表版本标记
    created_at  TEXT NOT NULL,
    PRIMARY KEY (product_id, created_at)
);
CREATE INDEX IF NOT EXISTS idx_nt_product ON name_translations(product_id);

-- 海外零售价目录：整站抓下来的「原始商品 + 价格」，先落库再谈与原商品的匹配。
-- 与 price_points 的区别：这里保留所有抓到的商品（包括对不上 GW 货号的），
-- 因为"没匹配上"本身就是需要观察的数据。
CREATE TABLE IF NOT EXISTS overseas_catalog (
    url          TEXT PRIMARY KEY,   -- 商品页地址
    source       TEXT NOT NULL,      -- element_games / ...
    title        TEXT NOT NULL,      -- 页面上的商品名（英文）
    price        REAL,               -- 页面标价（币种见 currency）
    currency     TEXT DEFAULT 'GBP',
    category_path TEXT,              -- 来源分类路径，如 warhammer-40k/orks
    product_id   TEXT,               -- 匹配到的 GW 货号（后处理填）
    match_score  REAL,               -- 匹配得分，便于复核
    product_code TEXT,                -- 商品页上的官方货号
    first_seen_at TEXT,               -- 首次出现在目录里的时间（每周新品靠它判定）
    crawled_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oc_source ON overseas_catalog(source);
CREATE INDEX IF NOT EXISTS idx_oc_product ON overseas_catalog(product_id);

-- 商品扩展信息：描述、件数、库存、官方货号等，来自零售站商品详情页
CREATE TABLE IF NOT EXISTS product_details (
    product_id   TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    url          TEXT,
    description  TEXT,               -- 清洗后的商品描述
    components   INTEGER,            -- 盒内件数（从 "73 components" 抽）
    stock        TEXT,               -- in_stock / preorder / out_of_stock
    product_code TEXT,               -- 零售站标注的官方货号（可能为空）
    raw_title    TEXT,               -- 页面原始标题
    fetched_at   TEXT NOT NULL
);

-- ---------- 价格观测（按渠道时序）----------
CREATE TABLE IF NOT EXISTS price_points (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  TEXT NOT NULL REFERENCES products(id),
    channel     TEXT NOT NULL,            -- gw_uk|gw_us|cn_official|cn_trade|taobao|xianyu|manual
    platform    TEXT NOT NULL DEFAULT '', -- 具体店铺/站点名
    price       REAL NOT NULL,
    currency    TEXT NOT NULL,
    price_cny   REAL,
    url         TEXT,
    observed_at TEXT NOT NULL,
    confidence  TEXT DEFAULT 'medium',    -- high|medium|low
    note        TEXT,
    UNIQUE(product_id, channel, platform, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_pp_product ON price_points(product_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_pp_channel ON price_points(channel);

-- ---------- 按源产出统计（每天每源一行）----------
CREATE TABLE IF NOT EXISTS source_stats (
    stat_date  TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    docs       INTEGER NOT NULL DEFAULT 0,   -- 本轮该源产出条数
    ok         INTEGER NOT NULL DEFAULT 1,   -- 0 = 采集失败
    avg_body   INTEGER NOT NULL DEFAULT 0,   -- 平均正文字数（判断内容深度）
    updated_at TEXT,
    PRIMARY KEY (stat_date, source_id)
);

-- ---------- 采集台账（按周记录进度）----------
CREATE TABLE IF NOT EXISTS price_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    week        TEXT NOT NULL,            -- 2026-W47
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats       TEXT,
    status      TEXT DEFAULT 'running'
);

-- ---------- 推送台账 ----------
CREATE TABLE IF NOT EXISTS pushes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    push_date   TEXT NOT NULL,        -- YYYY-MM-DD
    slot        INTEGER NOT NULL,
    story_id    TEXT REFERENCES stories(id),
    sku_ids     TEXT,
    title       TEXT,
    importance  REAL,
    channel     TEXT NOT NULL,
    payload     TEXT,
    sent_at     TEXT NOT NULL,
    delivery_ok INTEGER NOT NULL DEFAULT 1,
    UNIQUE(push_date, slot)
);

-- ---------- 反馈 ----------
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    push_id    INTEGER REFERENCES pushes(id),
    story_id   TEXT,
    signal     TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

-- ---------- 兴趣向量 ----------
CREATE TABLE IF NOT EXISTS interests (
    key        TEXT PRIMARY KEY,
    weight     REAL NOT NULL DEFAULT 0,
    updated_at TEXT
);

-- ---------- 正文缓存（同一 URL 不重复抓取）----------
CREATE TABLE IF NOT EXISTS content_cache (
    url_hash    TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    text        TEXT NOT NULL,
    char_count  INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_fetched ON content_cache(fetched_at);

-- ---------- 运行日志 ----------
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    stats       TEXT,
    error       TEXT
);
