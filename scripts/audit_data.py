"""数据完整性与配置一致性核查。"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "infotwitter"
all_text = "\n".join(f.read_text(encoding="utf-8") for f in SRC.rglob("*.py"))

# ---------- 1) 精确的配置键检查 ----------
cfg_text = (ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
used = set(re.findall(r'(?:cfg|self|config)\.get\(\s*["\']([a-z_][a-z0-9_.]*)["\']', all_text))
# config.py 里 @property 封装常用路径
props = set(re.findall(r"@property\s+def ([a-z_][a-z0-9_]*)", all_text))
print(f"=== 代码引用的配置路径 {len(used)} 条，逐条检查是否存在于 config.yaml ===")
missing = []
for path in sorted(used):
    # 检查顶层键
    top = path.split(".")[0]
    if top in props:
        continue
    if not re.search(rf"^\s{{0,4}}{top}:", cfg_text, re.M):
        missing.append(path)
print(f"缺失 {len(missing)} 条:" if missing else "全部存在")
for m in missing:
    print("  ", m)

# ---------- 2) 数据库完整性 ----------
cfg = load_config()
conn = sqlite3.connect(cfg.db_path)
conn.row_factory = sqlite3.Row

def q(sql, *a):
    return conn.execute(sql, a).fetchall()

print("\n=== 数据完整性 ===")
print("表行数:")
for r in q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    name = r["name"]
    if name.startswith("sqlite_"):
        continue
    n = conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]
    print(f"  {name:<18}{n:>6}")

print("\n重复检查:")
dup_story = q("""SELECT canonical_title, COUNT(*) n FROM stories
                 GROUP BY lower(trim(canonical_title)) HAVING n > 1 ORDER BY n DESC LIMIT 8""")
print(f"  stories 同标题重复: {len(dup_story)} 组")
for r in dup_story:
    print(f"    {r['n']}x  {str(r['canonical_title'])[:52]}")

dup_pp = q("""SELECT product_id, channel, observed_at, COUNT(*) n FROM price_points
              GROUP BY product_id, channel, observed_at HAVING n > 1 LIMIT 5""")
print(f"  price_points 同键重复: {len(dup_pp)} 组")

orphan = q("SELECT COUNT(*) c FROM stories WHERE id NOT IN (SELECT DISTINCT story_id FROM claims WHERE story_id IS NOT NULL)")[0]["c"]
print(f"  stories 无任何 claim 关联: {orphan} 条")

print("\n新鲜度:")
for r in q("""SELECT COUNT(*) c FROM stories WHERE first_seen_at < datetime('now','-7 day')"""):
    print(f"  7 天前的 story: {r['c']} 条")

print("\nclaims 分布:")
for r in q("SELECT COUNT(*) c, COUNT(DISTINCT story_id) s FROM claims"):
    print(f"  {r['c']} 条，覆盖 {r['s']} 个 story")

conn.close()
