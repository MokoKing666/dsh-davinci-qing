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
/** 稳定插件名；与 `cordis.patch.yml` 的 insert id 对齐。 */
export declare const name = "dsh-davinci-qing";
/** 本插件硬依赖的服务：只用到工具注册表。 */
export declare const inject: string[];
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
export declare const CONFIG_DEFAULTS: Required<Pick<Config, 'toolTimeoutMs' | 'startupTimeoutMs' | 'failOnStartupError' | 'enabled'>>;
/**
 * 统一的输出 schema：结构化结果本身就是 JSON。
 *
 * 注意 `detail` 故意不写 `type`：它的形状随工具而变（对象、数组或标量都可能），
 * 写成空 schema 表示「不约束」。千万不要写成 `type: 'json'` —— `json` 不是
 * JSON Schema 的合法类型，DSH 加载插件时会直接拒绝：
 * `unsupported JSON schema: schema.properties.detail.type must be one of
 * object/array/string/number/integer/boolean/null`。
 */
export declare const OUTPUT_SCHEMA: Record<string, unknown>;
/**
 * 注册全部达芬奇工具。
 *
 * @param ctx Cordis 上下文（需要 `tools`）。
 * @param config 插件配置（`cordis.patch.yml` 的 `config`）。
 */
export declare function apply(ctx: any, config?: Config): Promise<void>;
