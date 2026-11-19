"""检查 package.json files 字段声明的每一类文件是否真的被 git 跟踪。

发布前的最后一道闸：manifest 说包含什么，仓库里就必须真有什么。
"""

import fnmatch
import json
import subprocess
from pathlib import Path

REPO = Path(r"F:\dsh-davinci-qing")
pkg = json.loads((REPO / "package.json").read_text(encoding="utf-8"))

tracked = subprocess.run(
    ["git", "ls-files"], cwd=str(REPO), capture_output=True, text=True, encoding="utf-8"
).stdout.splitlines()
tracked_set = set(tracked)

print(f"git 跟踪文件数: {len(tracked_set)}")
print("\n=== files 字段逐项核对 ===")
problems = []
for entry in pkg.get("files", []):
    # 目录项可能写成 "dist" 或 "dist/"：两种都按前缀匹配
    prefix = entry.rstrip("/") + "/"
    dir_hits = [t for t in tracked_set if t.startswith(prefix)]
    if dir_hits:
        print(f"  OK   {entry:24} 目录，跟踪 {len(dir_hits)} 个文件")
        continue
    if "*" in entry:
        hits = [t for t in tracked_set if fnmatch.fnmatch(t, entry)]
        ok = bool(hits)
        print(f"  {'OK ' if ok else 'MISS'} {entry:24} 匹配 {len(hits)} 个文件")
        if not ok:
            problems.append(entry)
        continue
    ok = entry in tracked_set
    print(f"  {'OK ' if ok else 'MISS'} {entry:24}")
    if not ok:
        problems.append(entry)

print("\n=== 关键入口 ===")
for key in ("main", "types"):
    value = pkg.get(key)
    ok = value in tracked_set
    print(f"  {'OK ' if ok else 'MISS'} {key:6} -> {value}")
    if not ok:
        problems.append(value)

patch = pkg.get("dsh", {}).get("bundle", {}).get("patch")
if patch:
    clean = patch.lstrip("./")
    ok = clean in tracked_set
    print(f"  {'OK ' if ok else 'MISS'} patch  -> {clean}")
    if not ok:
        problems.append(clean)

print("\n" + "=" * 50)
if problems:
    print(f"❌ 有 {len(problems)} 项未跟踪: {problems}")
    raise SystemExit(1)
print("✅ manifest 声明的所有文件都已在 git 中")
