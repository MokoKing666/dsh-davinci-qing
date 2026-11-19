/**
 * dsh-davinci-qing — Cordis TS 插件壳
 *
 * 「让青青住进你的dsh」
 *
 * 架构：TS 壳 + Python 运行时
 * ------------------------------
 * 本文件**不实现任何达芬奇业务逻辑**。它只做三件事：
 *
 * 1. 启动并托管一个常驻 Python 进程（`lib/resolve_bridge.py --serve`）；
 * 2. 把 Python 报上来的 36 个 `qing_*` 工具注册进 `ctx.tools`
 *    （schema 完全来自 Python，TS 侧不做二次翻译，因此两边永不漂移）；
 * 3. 每个工具被调用时，把 `(name, args)` 通过 JSON-RPC 交给 Python，
 *    拿回结构化结果并渲染成文本内容块。
 *
 * 为什么不把 IPC 逻辑搬进 TS？因为 DaVinci Resolve 的脚本接口只有 Lua 与
 * Python 绑定（`fusionscript.dll`），TS 无法直接调用；把 Resolve 侧留在 Python、
 * 把 DSH 侧留在 Cordis，是唯一不重复实现的切法。
 *
 * @module dsh-davinci-qing
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// ---------------------------------------------------------------------------
// 插件元信息（DSH 加载器按名字找到本文件时会读取这些导出）
// ---------------------------------------------------------------------------

/** 稳定插件名；与 `cordis.patch.yml` 的 insert id 对齐。 */
export const name = 'dsh-davinci-qing';

/** 本插件硬依赖的服务：只用到工具注册表。 */
export const inject = ['tools'];

/** 包根目录（dist/ 的上一级）。 */
const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

/** 一份插件配置（`cordis.patch.yml` 的 `config` 就是它）。 */
export interface Config {
  /** Python 解释器路径。留空则按 `python` → `python3` → 项目 `.venv` 顺序探测。 */
  pythonPath?: string;
  /** `lib/resolve_bridge.py` 的路径。留空用包内默认位置。 */
  bridgePath?: string;
  /** 单次工具调用的超时（毫秒）。达芬奇渲染类操作可能很久，默认 3 分钟。 */
  toolTimeoutMs?: number;
  /** Python 桥启动的超时（毫秒）。首次导入 PIL / mcp SDK 会慢一些。 */
  startupTimeoutMs?: number;
  /** 是否在 Python 桥不可用时让插件加载失败。false 时只是没有工具。 */
  failOnStartupError?: boolean;
  /** 额外环境变量，透传给 Python 桥（如 `QING_DAVINCI_FAKE=1` 走模拟器）。 */
  env?: Record<string, string>;
  /** 关掉这个开关可以临时禁用全部工具而不卸载插件。 */
  enabled?: boolean;
}

/** 配置默认值。 */
export const CONFIG_DEFAULTS: Required<Pick<Config,
  'toolTimeoutMs' | 'startupTimeoutMs' | 'failOnStartupError' | 'enabled'>> = {
  toolTimeoutMs: 180_000,
  startupTimeoutMs: 60_000,
  failOnStartupError: false,
  enabled: true,
};

// ---------------------------------------------------------------------------
// 从 Python 报上来的工具描述
// ---------------------------------------------------------------------------

/** Python `list_tools` 的一条记录。 */
interface ToolSpec {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  annotations?: {
    read_only?: boolean;
    destructive?: boolean;
    idempotent?: boolean;
    title?: string | null;
  };
}

/** Cordis 工具定义（`ctx.tools.register` 的入参，只取用到的字段）。 */
interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  output: {
    schema: Record<string, unknown>;
    render: (args: unknown, value: unknown) => Array<{ type: 'text'; text: string }>;
  };
  timeoutMs: number;
  execute: (args: unknown) => Promise<unknown>;
  isConcurrencySafe?: (args: unknown) => boolean;
}

// ---------------------------------------------------------------------------
// Python 桥：常驻子进程 + 按行 JSON-RPC
// ---------------------------------------------------------------------------

type Pending = {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
};

/** 与 `lib/resolve_bridge.py --serve` 的 stdio 对话。 */
class PythonBridge {
  private child: ChildProcessWithoutNullStreams | null = null;
  private readonly pending = new Map<string, Pending>();
  private buffer = '';
  private seq = 0;
  private ready: Promise<void> | null = null;
  private stderrTail: string[] = [];
  private disposed = false;

  constructor(
    private readonly python: string,
    private readonly script: string,
    private readonly env: NodeJS.ProcessEnv,
    private readonly cwd: string,
  ) {}

  /** 最近一段 Python stderr，用于错误信息里带上下文。 */
  get diagnostics(): string {
    return this.stderrTail.slice(-8).join('\n');
  }

  /** 启动子进程并等待 `ready` 事件。 */
  start(timeoutMs: number): Promise<void> {
    if (this.ready) return this.ready;
    this.ready = new Promise<void>((resolveReady, rejectReady) => {
      let settled = false;

      let child: ChildProcessWithoutNullStreams;
      try {
        child = spawn(this.python, [this.script, '--serve'], {
          cwd: this.cwd,
          env: this.env,
          stdio: ['pipe', 'pipe', 'pipe'],
          windowsHide: true,
        }) as ChildProcessWithoutNullStreams;
      } catch (error) {
        rejectReady(error instanceof Error ? error : new Error(String(error)));
        return;
      }

      this.child = child;

      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        rejectReady(
          new Error(
            `Python 桥在 ${timeoutMs}ms 内没有就绪。\n`
            + `启动命令：${this.python} ${this.script} --serve\n`
            + (this.diagnostics ? `Python 输出：\n${this.diagnostics}` : ''),
          ),
        );
        this.kill();
      }, timeoutMs);
      // 桥一旦就绪这个计时器就没意义了；不要让它在后台吊住进程。
      timer.unref?.();

      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (chunk: string) => {
        for (const line of chunk.split(/\r?\n/)) {
          if (!line.trim()) continue;
          // 就绪握手走 stderr，避免污染 JSON 通道
          if (!settled && line.includes('"event"') && line.includes('ready')) {
            settled = true;
            clearTimeout(timer);
            resolveReady();
            continue;
          }
          this.stderrTail.push(line);
          if (this.stderrTail.length > 40) this.stderrTail.shift();
        }
      });

      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk: string) => this.onStdout(chunk));

      child.on('error', (error) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          rejectReady(error);
        }
        this.rejectAll(error);
      });

      child.on('exit', (code, signal) => {
        this.child = null;
        const reason = new Error(
          `Python 桥已退出（code=${code ?? 'null'}, signal=${signal ?? 'null'}）。`
          + (this.diagnostics ? `\n最后的输出：\n${this.diagnostics}` : ''),
        );
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          rejectReady(reason);
        }
        this.rejectAll(reason);
      });
    });
    return this.ready;
  }

  private onStdout(chunk: string): void {
    this.buffer += chunk;
    let index = this.buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      index = this.buffer.indexOf('\n');
      if (!line) continue;
      let message: {
        id?: string | null;
        ok?: boolean;
        result?: unknown;
        error?: { code?: string; message?: string; hint?: string; traceback?: string };
      };
      try {
        message = JSON.parse(line);
      } catch {
        continue; // 非协议行（比如 Python 的调试 print）直接跳过
      }
      const id = message.id;
      if (typeof id !== 'string') continue;
      const pending = this.pending.get(id);
      if (!pending) continue;
      this.pending.delete(id);
      clearTimeout(pending.timer);
      if (message.ok) {
        pending.resolve(message.result);
      } else {
        const detail = message.error ?? {};
        const error = new Error(
          [detail.message ?? 'Python 桥返回失败',
            detail.hint ? `建议：${detail.hint}` : '',
            detail.traceback ? `\n${detail.traceback}` : '']
            .filter(Boolean)
            .join('\n'),
        );
        (error as Error & { code?: string }).code = detail.code;
        pending.reject(error);
      }
    }
  }

  /** 发一条请求并等结果。 */
  request<T>(method: string, params: Record<string, unknown>, timeoutMs: number): Promise<T> {
    if (this.disposed) return Promise.reject(new Error('Python 桥已关闭。'));
    const child = this.child;
    if (!child || !child.stdin.writable) {
      return Promise.reject(new Error('Python 桥没有在运行。'));
    }

    const id = String(++this.seq);
    const payload = JSON.stringify({ id, method, params }) + '\n';

    return new Promise<T>((resolveReply, rejectReply) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        rejectReply(
          new Error(
            `工具调用超过 ${timeoutMs}ms 未返回。\n`
            + '达芬奇正在渲染或被模态对话框挡住时会发生这种情况；'
            + '可先用 qing_resolve_health 确认状态，或调大 config.toolTimeoutMs。',
          ),
        );
      }, timeoutMs);
      timer.unref?.();

      this.pending.set(id, {
        resolve: resolveReply as (value: unknown) => void,
        reject: rejectReply,
        timer,
      });

      try {
        child.stdin.write(payload, 'utf8');
      } catch (error) {
        this.pending.delete(id);
        clearTimeout(timer);
        rejectReply(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  private rejectAll(error: Error): void {
    for (const [, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  kill(): void {
    if (this.disposed) return;
    this.disposed = true;
    const child = this.child;
    this.child = null;
    if (!child) return;
    try {
      if (child.stdin.writable) child.stdin.write(JSON.stringify({ id: 'bye', method: 'shutdown' }) + '\n');
    } catch {
      /* 管道可能已经断了 */
    }
    const force = setTimeout(() => {
      try {
        child.kill();
      } catch {
        /* 已经退出 */
      }
    }, 1500);
    force.unref?.();
    child.once('exit', () => clearTimeout(force));
  }
}

// ---------------------------------------------------------------------------
// Python 解释器探测
// ---------------------------------------------------------------------------

/** 按配置与常见位置找 Python。返回 null 表示没找到。 */
function resolvePython(configured: string | undefined): string | null {
  if (configured && configured.trim()) {
    const candidate = configured.trim();
    if (candidate.includes('/') || candidate.includes('\\')) {
      return existsSync(candidate) ? candidate : null;
    }
    return candidate; // 交给 PATH
  }

  const candidates = [
    join(PACKAGE_ROOT, '.venv', 'Scripts', 'python.exe'),
    join(PACKAGE_ROOT, '.venv', 'bin', 'python3'),
    process.platform === 'win32' ? 'python.exe' : 'python3',
    'python',
    'python3',
  ];
  for (const candidate of candidates) {
    if (candidate.includes('/') || candidate.includes('\\')) {
      if (existsSync(candidate)) return candidate;
      continue;
    }
    return candidate; // 裸命令名：由 PATH 决定
  }
  return null;
}

// ---------------------------------------------------------------------------
// 结果渲染
// ---------------------------------------------------------------------------

const MAX_RENDER_CHARS = 12_000;

/**
 * 把 Python 返回的结构化结果压成一段给模型看的文本。
 *
 * 保持原样输出 JSON 是有意的：这些结果里 `hint` / `detail.violations` 是模型
 * 下一步决策的依据，任何"摘要"都会丢掉可操作信息。只做长度保护。
 */
function renderValue(value: unknown): string {
  let text: string;
  if (typeof value === 'string') {
    text = value;
  } else {
    try {
      text = JSON.stringify(value, null, 2);
    } catch {
      text = String(value);
    }
  }
  if (text.length <= MAX_RENDER_CHARS) return text;
  return `${text.slice(0, MAX_RENDER_CHARS)}\n\n…（结果过长，已截断 ${text.length - MAX_RENDER_CHARS} 字符）`;
}

/** 统一的输出 schema：结构化结果本身就是 JSON。 */
const OUTPUT_SCHEMA: Record<string, unknown> = {
  type: 'object',
  properties: {
    ok: { type: 'boolean', description: 'false 表示这次调用没有完成预期动作；读 hint 再决定下一步。' },
    error: { type: 'string', description: '机器可读的错误码，如 preflight_failed / resolve_not_connected。' },
    message: { type: 'string' },
    hint: { type: 'string', description: '中文排查或修正建议。' },
    detail: { type: 'json' },
  },
  additionalProperties: true,
};

/** 只读工具可以并发跑；破坏性操作串行，避免同时改一个工程。 */
function isReadOnly(annotations: ToolSpec['annotations']): boolean {
  return annotations?.read_only === true;
}

// ---------------------------------------------------------------------------
// Cordis 入口
// ---------------------------------------------------------------------------

/**
 * 注册全部达芬奇工具。
 *
 * @param ctx Cordis 上下文（需要 `tools`）。
 * @param config 插件配置（`cordis.patch.yml` 的 `config`）。
 */
export async function apply(ctx: any, config: Config = {}): Promise<void> {
  if (config.enabled === false) return;

  const settings = { ...CONFIG_DEFAULTS, ...config };
  const logger = ctx.logger ?? console;

  const python = resolvePython(config.pythonPath);
  const bridgePath = config.bridgePath
    ? resolve(config.bridgePath)
    : join(PACKAGE_ROOT, 'lib', 'resolve_bridge.py');

  if (!python) {
    const message =
      'dsh-davinci-qing: 没找到 Python 解释器。\n'
      + '请在插件配置里设置 pythonPath（指向 python.exe 的绝对路径），'
      + '或把 Python 加入 PATH。';
    if (settings.failOnStartupError) throw new Error(message);
    logger.warn?.(message);
    return;
  }

  if (!existsSync(bridgePath)) {
    const message = `dsh-davinci-qing: 找不到 Python 桥 ${bridgePath}。`;
    if (settings.failOnStartupError) throw new Error(message);
    logger.warn?.(message);
    return;
  }

  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONPATH: PACKAGE_ROOT,
    ...(config.env ?? {}),
  };

  const bridge = new PythonBridge(python, bridgePath, env, PACKAGE_ROOT);

  try {
    await bridge.start(settings.startupTimeoutMs);
  } catch (error) {
    const message = `dsh-davinci-qing: Python 桥启动失败 —— ${(error as Error).message}`;
    if (settings.failOnStartupError) throw new Error(message);
    logger.warn?.(message);
    return;
  }

  let specs: ToolSpec[];
  try {
    specs = await bridge.request<ToolSpec[]>('list_tools', {}, settings.startupTimeoutMs);
  } catch (error) {
    bridge.kill();
    throw new Error(
      `dsh-davinci-qing: 从 Python 桥取工具清单失败 —— ${(error as Error).message}`,
    );
  }

  // 每一行注册都是一个 effect：插件卸载时工具随之消失。
  const disposers: Array<() => void> = [];

  for (const spec of specs) {
    const definition: ToolDefinition = {
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      timeoutMs: settings.toolTimeoutMs,
      output: {
        schema: OUTPUT_SCHEMA,
        render: (_args, value) => [{ type: 'text', text: renderValue(value) }],
      },
      execute: async (args: unknown) => {
        try {
          return await bridge.request(
            'call_tool',
            { name: spec.name, arguments: (args ?? {}) as Record<string, unknown> },
            settings.toolTimeoutMs,
          );
        } catch (error) {
          // 桥层故障也要变成结构化结果，不要抛出去把整轮对话打断
          return {
            ok: false,
            error: 'bridge_failure',
            message: (error as Error).message,
            hint:
              'Python 桥不可用了。先调用 qing_resolve_health；'
              + '若仍失败，重启 DSH 让插件重新拉起 Python 进程。',
          };
        }
      },
    };

    if (isReadOnly(spec.annotations)) {
      definition.isConcurrencySafe = () => true;
    }

    disposers.push(ctx.tools.register(definition));
  }

  logger.info?.(`dsh-davinci-qing: 已注册 ${specs.length} 个达芬奇工具（Python 桥 ${python}）`);

  // 插件卸载时：注销全部工具 + 关掉 Python 进程
  ctx.effect(() => () => {
    for (const dispose of disposers) {
      try {
        dispose();
      } catch {
        /* 注销失败不影响其它清理 */
      }
    }
    bridge.kill();
  });
}