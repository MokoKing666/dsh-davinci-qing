#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复现并验证 issue：缺依赖时 list_tools 必须走降级而不是炸掉插件。

用 `-S`（不加载 site-packages）的 Python 起桥，人为制造「pydantic 不可用」，
然后看：

1. 桥进程本身是否还活着（应当活着，只是 list_tools 返回结构化错误）；
2. 错误码是不是 missing_dependency；
3. hint 里有没有解释器路径与安装命令；
4. TS 壳在 `failOnStartupError` 的两种取值下行为是否与约定一致。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "lib" / "resolve_bridge.py"

_failures: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {extra}" if extra else ""))
    if not condition:
        _failures.append(name)


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    candidate = (
        Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime"
        / "dependencies" / "node" / "bin" / "node.exe"
    )
    return str(candidate) if candidate.is_file() else None


def parse_first_response(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def probe_bridge_without_site_packages() -> None:
    """用 -S 启动桥（屏蔽 site-packages），发一条 list_tools。"""

    print("=== 1. 缺依赖时桥的行为（-S 屏蔽 site-packages）===")
    completed = subprocess.run(
        [sys.executable, "-S", str(BRIDGE), "--serve"],
        input=json.dumps({"id": "1", "method": "list_tools"}) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(ROOT),
    )

    response = parse_first_response(completed.stdout)
    check(
        "桥进程正常结束，没有异常退出",
        completed.returncode in (0, 1),
        f"returncode={completed.returncode}",
    )

    if response is None:
        check("桥输出了结构化 JSON 响应", False, "stdout 里没有可解析的 JSON")
        print("--- stderr ---")
        print(completed.stderr[-1500:])
        return

    check("桥输出了结构化 JSON 响应", True)
    check("响应 ok=false", response.get("ok") is False, f"ok={response.get('ok')}")

    error = response.get("error") or {}
    check(
        "错误码是 missing_dependency",
        error.get("code") == "missing_dependency",
        f"code={error.get('code')!r}",
    )
    hint = error.get("hint") or ""
    check("hint 提到 pip install", "pip install" in hint)
    check(
        "hint 带了解释器路径",
        bool(error.get("detail", {}).get("interpreter")),
        f"interpreter={error.get('detail', {}).get('interpreter')!r}",
    )
    detail = error.get("detail") or {}
    check("detail 里带 module 名", bool(detail.get("module")), f"module={detail.get('module')!r}")

    print("\n--- 桥 stderr 前 3 行 ---")
    for line in completed.stderr.splitlines()[:3]:
        print(f"  {line}")


DEGRADE_PROBE = """import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const ROOT = process.argv[2];
const pythonPath = process.argv[3];
const mod = await import(pathToFileURL(join(ROOT, 'dist', 'index.js')).href);

function makeCtx() {
  const logs = [];
  const registered = new Map();
  return {
    logs,
    registered,
    ctx: {
      logger: {
        info: (m) => logs.push(['info', m]),
        warn: (m) => logs.push(['warn', m]),
        debug: () => {},
        error: (m) => logs.push(['error', m]),
      },
      tools: {
        register(def) {
          registered.set(def.name, def);
          return () => registered.delete(def.name);
        },
      },
      effect(fn) {
        const dispose = fn();
        return () => { if (typeof dispose === 'function') dispose(); };
      },
    },
  };
}

const baseConfig = {
  pythonPath,
  // 用解释器参数制造「没有 pydantic」的环境。
  // 不用 .cmd 包装器：Node 18.20+ 出于 CVE-2024-27980 的加固，直接 spawn
  // .cmd/.bat 会报 spawn EINVAL，那是夹具写法问题，不是被测逻辑的问题。
  pythonArgs: ['-S'],
  env: { PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
};

// 分支 A：failOnStartupError=false（默认）→ 只告警、不抛、不注册
{
  const { ctx, logs, registered } = makeCtx();
  let threw = null;
  try {
    await mod.apply(ctx, { ...baseConfig, failOnStartupError: false, startupTimeoutMs: 60000 });
  } catch (error) { threw = error; }
  const warned = logs.filter(([level]) => level === 'warn').map(([, m]) => m).join('\\n');
  console.log(JSON.stringify({
    branch: 'failOnStartupError=false',
    threw: threw ? threw.message.split('\\n')[0] : null,
    registered: registered.size,
    warned,
    ok: threw === null && registered.size === 0 && /pip install/.test(warned),
  }));
}

// 分支 B：failOnStartupError=true → 必须抛
{
  const { ctx, registered } = makeCtx();
  let threw = null;
  try {
    await mod.apply(ctx, { ...baseConfig, failOnStartupError: true, startupTimeoutMs: 60000 });
  } catch (error) { threw = error; }
  console.log(JSON.stringify({
    branch: 'failOnStartupError=true',
    threw: threw ? threw.message.split('\\n')[0] : null,
    registered: registered.size,
    ok: threw !== null && registered.size === 0,
  }));
}
"""


def probe_ts_branches() -> None:
    """用伪造 ctx 跑 apply()，确认两种 failOnStartupError 取值都按约定走。

    「缺依赖」用 `pythonArgs: ['-S']` 制造（屏蔽 site-packages），不写 `.cmd`
    包装器 —— Node 18.20+ 出于 CVE-2024-27980 加固，spawn `.cmd` 会 EINVAL。
    """

    print("\n=== 2. TS 壳的降级分支 ===")
    node = find_node()
    if node is None:
        print("[SKIP] 找不到 node，跳过")
        return

    script = ROOT / "tests" / "_degrade_probe.mjs"
    script.write_text(DEGRADE_PROBE, encoding="utf-8")

    try:
        completed = subprocess.run(
            [node, str(script), str(ROOT), sys.executable],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            cwd=str(ROOT),
        )
    finally:
        try:
            script.unlink()
        except OSError:
            pass

    lines = [ln for ln in completed.stdout.splitlines() if ln.strip().startswith("{")]
    if not lines:
        print("[FAIL] TS 壳没有输出结果")
        print(completed.stdout[-1500:])
        print(completed.stderr[-1500:])
        _failures.append("TS 壳降级分支")
        return

    for line in lines:
        payload = json.loads(line)
        check(
            f"{payload['branch']} 行为正确",
            bool(payload.get("ok")),
            f"threw={payload.get('threw')!r} registered={payload.get('registered')}",
        )
        if not payload.get("ok"):
            print(f"  ---- 告警内容 ----\n{payload.get('warned', '')}")


def main() -> int:
    print(f"解释器：{sys.executable}")
    probe_bridge_without_site_packages()
    probe_ts_branches()

    print()
    if _failures:
        print(f"❌ {len(_failures)} 项未通过：" + "、".join(_failures))
        return 1
    print("✅ 缺依赖降级路径验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
