"""维护脚本：重置台账 / 事件 / 抽取结果。

注意：默认**保留** LLM 抽取结果，因为重新分析要花钱。
只有显式加 --analyses 才会清空。

用法：
    python scripts/admin_reset.py              # 只清推送台账
    python scripts/admin_reset.py --all        # 清台账 + 事件（保留抽取结果，零成本）
    python scripts/admin_reset.py --analyses   # 额外清空抽取结果（会触发重新调用 LLM）
    python scripts/admin_reset.py --purge      # 连原始文档一起清空
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB = Path(__file__).resolve().parents[1] / "data" / "infotwitter.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="清空 stories 与 claims（保留抽取结果）")
    parser.add_argument("--analyses", action="store_true", help="清空 LLM 抽取结果（会重新计费）")
    parser.add_argument("--purge", action="store_true", help="清空 raw_docs（完整重跑）")
    parser.add_argument(
        "--keep-pushes", action="store_true",
        help="保留推送台账（默认会清空）。去重靠它，清掉之后当天的内容会被再推一遍",
    )
    args = parser.parse_args()

    if not DB.exists():
        print(f"数据库不存在：{DB}")
        return 1

    con = sqlite3.connect(DB)

    if args.keep_pushes:
        print("保留推送台账    （--keep-pushes）")
    else:
        n = con.execute("DELETE FROM pushes").rowcount
        print(f"已清空 pushes        {n} 条  ← 去重依据，清掉后当天内容会被再推一次")

    if args.all or args.analyses or args.purge:
        print(f"已清空 stories       {con.execute('DELETE FROM stories').rowcount} 条")
        print(f"已清空 claims        {con.execute('DELETE FROM claims').rowcount} 条")
        # story_docs 是事件身份链（A-06）：stories 没了它必须一起清，否则留下悬挂归属
        try:
            print(f"已清空 story_docs    {con.execute('DELETE FROM story_docs').rowcount} 条")
        except sqlite3.OperationalError:
            print("story_docs 表不存在，跳过（旧库）")

    if args.analyses or args.purge:
        n = con.execute("UPDATE raw_docs SET analysis=NULL, analyzed_at=NULL").rowcount
        print(f"已重置分析           {n} 条  ← 下次运行会重新调用 LLM（产生费用）")

    if args.purge:
        print(f"已清空 raw_docs      {con.execute('DELETE FROM raw_docs').rowcount} 条")
        try:
            print(f"已清空 story_docs    {con.execute('DELETE FROM story_docs').rowcount} 条")
        except sqlite3.OperationalError:
            pass

    con.commit()
    con.close()
    print("\n抽取结果已保留，下次运行不会重复计费。" if not (args.analyses or args.purge) else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
