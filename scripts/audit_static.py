"""静态审查：死代码、配置键一致性、异常处理、分层依赖。"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "infotwitter"
FILES = sorted(SRC.rglob("*.py"))


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------- 1) 定义的函数 vs 引用次数 ----------------
all_text = "\n".join(read(f) for f in FILES)
tests_text = "\n".join(read(f) for f in (ROOT / "tests").rglob("*.py"))

defs: list[tuple[str, str, int]] = []   # (module, name, lineno)
for f in FILES:
    tree = ast.parse(read(f))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append((f.relative_to(SRC).as_posix(), node.name, node.lineno))

unused_src = []
unused_everywhere = []
for mod, name, ln in defs:
    if name.startswith("__"):
        continue
    # 源码里除定义行之外的引用
    uses_src = len(re.findall(rf"\b{re.escape(name)}\b", all_text)) - 1
    uses_test = len(re.findall(rf"\b{re.escape(name)}\b", tests_text))
    if uses_src <= 0:
        if uses_test == 0:
            unused_everywhere.append((mod, name, ln))
        else:
            unused_src.append((mod, name, ln))

print(f"=== 未被引用的函数（源码内无调用、测试里也没有）{len(unused_everywhere)} 个 ===")
for mod, name, ln in unused_everywhere:
    print(f"  {mod}:{ln}  {name}")
print()
print(f"=== 仅测试在用、源码无调用 {len(unused_src)} 个 ===")
for mod, name, ln in unused_src:
    print(f"  {mod}:{ln}  {name}")

# ---------------- 2) 配置键一致性 ----------------
cfg_text = read(ROOT / "config" / "config.yaml")
cfg_keys = set(re.findall(r"^\s{0,4}([a-z_][a-z0-9_]*):", cfg_text, re.M))
# 代码里 cfg.get("a.b") 的点路径首段
used_paths = set(re.findall(r'\.get\(\s*"([a-z_][a-z0-9_.]*)"', all_text))
used_paths |= set(re.findall(r"\.get\(\s*'([a-z_][a-z0-9_.]*)'", all_text))
props = set(re.findall(r"@property\s+def ([a-z_][a-z0-9_]*)", all_text))

missing = sorted({p.split(".")[0] for p in used_paths} - cfg_keys - props - {"llm", "slots", "windows", "thresholds", "pricing", "push", "network", "content", "scoring", "app", "retention"})
print()
print(f"=== 代码引用但 config.yaml 里找不到的顶层键 {len(missing)} 个 ===")
for m in missing:
    print(f"  {m}")

# ---------------- 3) 异常处理 ----------------
bare = []
broad = []
for f in FILES:
    src = read(f)
    for i, line in enumerate(src.splitlines(), 1):
        if re.search(r"except\s*:", line):
            bare.append((f.name, i, line.strip()))
        elif re.search(r"except\s+Exception", line) and "pass" not in src.splitlines()[i - 1:i + 1][-1]:
            broad.append((f.name, i, line.strip()))
print()
print(f"=== 裸 except（会吞掉 KeyboardInterrupt）{len(bare)} 处 ===")
for n, i, l in bare:
    print(f"  {n}:{i}  {l}")
print(f"=== except Exception 共 {len(broad)} 处（需逐个确认是否静默）===")

# ---------------- 4) TODO / FIXME / 临时标记 ----------------
marks = []
for f in FILES:
    for i, line in enumerate(read(f).splitlines(), 1):
        if re.search(r"\b(TODO|FIXME|XXX|HACK|临时|待办|暂时)\b", line):
            marks.append((f.relative_to(SRC).as_posix(), i, line.strip()[:90]))
print()
print(f"=== TODO/FIXME/临时标记 {len(marks)} 处 ===")
for n, i, l in marks:
    print(f"  {n}:{i}  {l}")

# ---------------- 5) 分层依赖 ----------------
print()
print("=== 模块内部依赖（谁 import 谁）===")
deps = defaultdict(set)
for f in FILES:
    mod = f.stem if f.parent == SRC else f"{f.parent.name}.{f.stem}"
    for m in re.findall(r"from \.(\w+)|from \.(\w+)\.|import (\w+)", read(f)):
        target = next((x for x in m if x), "")
        if target and target not in ("__future__",):
            deps[mod].add(target)
for mod in sorted(deps):
    if deps[mod]:
        print(f"  {mod:<22}-> {', '.join(sorted(deps[mod]))}")
