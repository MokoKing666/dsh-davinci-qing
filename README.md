# 快乐青青 · dsh-davinci-qing

> **让青青住进你的dsh** —— 为创作者打造的 DaVinci Resolve Studio 双通道深度工作流自动化引擎（`v1.0.4` · 62 个原子工具）。

---

## 为什么做这个项目

在日常的商业拍摄与高强度后期制作中，真正消耗创作者心力的，往往不是审美判断与叙事决策本身，而是前期素材的分类归档、多机位轨道整理、基础还原节点挂载，以及反复核对导出参数这些高度重复的机械性劳动。

我们一直在思考：**如果 AI 不再只是停留在对话框里给出建议，而是真正理解时间线的结构、看懂调色节点树的逻辑，并安全地替你完成这些繁琐的前置工作，后期体验会发生怎样的改变？**

**快乐青青（`dsh-davinci-qing`）** 正是为了解决这个问题而生。它将 DeepSeek Harness (DSH) 直接接入 **DaVinci Resolve Studio**，把素材入库、时间线编排、节点级调色、Fusion 合成与交付队列封装为 **62 个可安全调用的 `qing_*` 原子工具**。你只需要用自然语言描述创作意图，它就能在你的工程里精准落地。

---

## 双通道系统架构

项目采用 **“Cordis TypeScript 轻量转发壳 + Python 常驻桥接运行时”** 架构，并在底层并行打通了达芬奇的两条官方通信通道：

```text
DSH (Cordis)
   │  ctx.tools.register(...)  ← 62 个 qing_* 工具（Python 单源生成 Schema，零漂移）
   ▼
dist/index.js  (TypeScript 壳：纯转发，不实现业务逻辑)
   │  stdio JSON-RPC
   ▼
lib/resolve_bridge.py  (常驻 Python 运行时)
   ├── 通道一：External Scripting API (DaVinciResolveScript / fusionscript) ──→ 43 个核心工程工具
   └── 通道二：Resolve 官方原生 MCP (ResolveMCP.exe · Studio 21.1+)       ──→ 19 个增强感知与生成工具
```

- **双通道并行互补**：脚本 API 负责高可靠、带快照保护的工程与时间线操作；官方 MCP 层则补齐了脚本 API 结构上无法触及的能力（如读取节点参数的**实际取值与 ASC CDL**、不移动播放头渲染**任意时间码单帧与 ROI 局部放大**、生成**整条时间线缩略图网格**，以及动态生成 `.cube` LUT 与 `.dctl` 着色器）。当官方 MCP 不可用时，基础 43 个工具仍可独立稳定运行。
- **单源 Schema 零漂移**：全部 62 个工具的 JSON Schema 均由 Python 侧动态生成并推给 DSH，TypeScript 壳内不维护第二份重复定义。

---

## 核心能力（七大模块 · 62 个原子工具）

### 1. 环境与工程诊断（4 个工具）
- **七项链路自检（`qing_resolve_health`）**：自动探测脚本路径、动态库导入、IPC 连通性、Studio 授权状态、当前工程与时间线及外部脚本开关配置，同步检测官方 MCP 可用性，失败时提供明确的中文排查步骤。
- **工程全局总览与页面直达**：一键读取工程帧率、分辨率、色彩科学配置及轨道拓扑，支持在 `media` / `cut` / `edit` / `fusion` / `color` / `fairlight` / `deliver` 七大工作区之间无缝切换。

### 2. 媒体池与智能归档（8 个工具）
- **只读预扫与递归入库**：导入前先扫描磁盘目录统计视频/音频/静帧数量与总体积，支持递归入库并按机位（`by_camera`）、日期（`by_date`）、类型或关键词自动创建子素材箱分类归档（补齐达芬奇原生 Smart Category 不支持机位与日期分类的短板）。
- **元数据真实表头闭环**：先导出元数据 CSV 并自动兼容 Windows 下的 **UTF-16 LE + BOM** 编码回读真实列名，再批量写入 Scene、Take、Camera # 等字段，杜绝键名猜错导致的静默失效；支持将素材标记区间批量转为子剪辑。

### 3. 时间线精剪与工业交换格式（13 个工具）
- **多轨道精准编排**：支持新建指定规格的时间线（涵盖 1080×1920 竖屏商业短片与各类电影画幅）、轨道追加或按 `record_frame` 精准插入、批量修改变换属性（Zoom、Pan/Tilt、Crop、Dynamic Zoom）。
- **彩色标记与字幕同步**：支持 16 色标记的批量读写与按条件清理（支持 `dry_run` 预览），支持导入 SRT 字幕或直接通过时间码列表生成字幕轨。
- **17 种工业级交换格式导出**：原生支持 `AAF`、`DRT`、`EDL`、`FCP7 XML`、`FCPXML (1.8–1.10)`、`OTIO`、`ALE / ALE+CDL`、`Dolby Vision (2.9–5.1)`、`HDR10` 等 17 种标准工程交换格式。

### 4. Color 节点级调色与视觉闭环（7 个工具）
- **稳定标签寻址（`qing_color_label_nodes`）**：针对达芬奇删除节点后全树重编号的特性，支持为关键节点打上语义标签与色标，实现跨操作的稳定节点定位；开启 `include_grade_detail=true` 时可并行读取节点内工具参数实际取值与 ASC CDL 等效值。
- **预设挂载与跨片段复制**：支持按 `node_label` 或序号挂载 `.cube` / `.ilut` / `.olut` 3D LUT、按单片段 / 颜色标签组 / 整条时间线批量应用 `.drx` PowerGrade 预设，以及跨片段复制完整节点树。
- **帧截图与缩略图网格审片（`qing_color_grab_frame`）**：支持导出当前播放头画面并附带死白/死黑面积、亮度分位与饱和度像素统计（同时支持调用官方层生成最多 48 帧的时间线缩略图网格），帮助多模态模型对照审片清单检视曝光、高光、肤色、黑位与构图。

### 5. Fusion 程序化合成（3 个工具）
- **节点检视与参数驱动**：支持读取合成内的节点拓扑与输入值，动态添加 `TextPlus`、`Transform`、`ColorCorrector`、`Merge`、`Blur` 等 21 种白名单节点并批量修改文本、字号、坐标与颜色输入，同时在返回值中明确标识 `connected_to_output` 连线状态。

### 6. Deliver 交付与防覆盖队列（8 个工具）
- **本机格式与预设探测**：实时读取当前工作站支持的渲染格式（实测 23 种）、编解码器矩阵与本机渲染预设（实测 39 个），支持绕过预设直接配置 24 项底层 `RenderSettings` 参数。
- **安全入队与异步渲染**：支持 `overwrite` / `unique` / `fail` 三种同名文件保护策略，防止批量出片时静默覆盖已有成片；入队与启动渲染分离，支持异步轮询渲染进度与中断清理。

### 7. 官方 MCP 增强层（19 个工具 · 需 Studio 21.1+）
- **深度调色读取与动态生成**：完整继承官方 `ResolveMCP.exe` 的 18 项能力，支持读取调色实际参数与 ASC CDL、按任意时间码渲染单帧与 **ROI 局部放大图**（不移动播放头）、生成多帧时间线概览网格、在线编写/热更新 `.dctl` 着色器代码以及通过 Python 数学函数逐格点生成 `.cube` 3D LUT。
- **实时 API 检索与进程拉起**：支持在本机 `.pyi` 存根中正则检索官方 API 签名、查阅开发者文档、一键拉起 Resolve 进程，以及在受控条件下执行沙箱 Python 脚本。

---

## 安全机制：四重工程护栏

在真实商业项目中，任何不可逆的工程误操作都是灾难性的。`dsh-davinci-qing` 在底层内置了四重安全护栏：

1. **强制工程存盘与时间线快照（Checkpoint）**：所有涉及时间线剪辑、调色节点树与 Fusion 合成修改的 **10 个核心写入工具**，在执行前均会强制触发 `project.SaveProject()`，自动克隆一条带时间戳的备份时间线（`[Backup_HHMMSS]_原时间线名`），随后切回原时间线再执行操作。若备份创建失败，系统默认拒绝执行。
2. **26 项 Pre-flight 写入前预检**：在向达芬奇发送任何修改指令前，一次性校验轨道越界、轨道锁定状态、帧号范围、节点索引与标签存在性、本地路径合法性及输出文件冲突；预检不通过绝不产生半成品写入，并一次性返回所有违规项与中文修复建议。
3. **覆盖前回读与全量审计日志**：挂载新 LUT、切换节点启用状态、修改节点标签或导出工程文件时，系统均会回读并返回被替换前的原始状态（如 `replaced_luts`、`previous_enabled`）；每一次检查点创建与破坏性写入均会结构化记录至 `~/.dsh-davinci-qing/audit.jsonl`（支持通过 `QING_STATE_DIR` 自定义）。
4. **内置模拟器隔离（`QING_DAVINCI_FAKE=1`）**：内置完整的 Resolve 对象模型替身模拟器，开启后所有读写测试均在隔离环境中运行，完全不触碰真实工程（官方 MCP 层同步禁用）。

---

## 能力边界与真机验证

我们坚持如实呈现工具的能力边界，不让模型做做不到的承诺：
- **原生撤销栈限制**：达芬奇外部脚本操作**不进入 Ctrl+Z 撤销历史栈**，回滚请直接使用自动生成的 `[Backup_HHMMSS]` 备份时间线。
- **未开放接口的功能**：官方脚本 API 目前未开放调色台**限定器（Qualifier）取色**、**Power Window 窗口绘制**、**跟踪器（Tracker）解算**以及 **Fusion 节点自动连线**（脚本 `AddTool` 新建的节点默认未连线，需在 Fusion 页面手动接入 `MediaOut`）。
- **截图非硬件示波器**：截图工具返回的是 8-bit 显示参考图像与像素统计，不含硬件级 IRE / 0–1023 刻度；精确信号过冲判断请在达芬奇内开启波形图（并打开 `Extents`）。
- **渲染控制红线**：插件不会擅自修改 Resolve 全局偏好设置，也不会未经确认自行启动渲染。

> **真机实测与九道验收门禁**：本项目已在 **Windows 11 · DaVinci Resolve Studio 21.2.0.5 · Python 3.13** 环境下完成全链路实机验证（通过 7 项环境自检、23 种渲染格式与 39 个预设探测、官方 MCP 18 项工具连通、ROI 单帧与 2×3 缩略图网格核验），并通过了含 32 项单元测试、65/65 全工具冒烟、Schema 合法性与 DSH 插件规范审计在内的全部九道自动化门禁。

---

## 环境前置要求

- **软件版本**：**DaVinci Resolve Studio**（v18.5 及以上版本；**官方 MCP 增强层需 Studio 21.1+**）。
  > *注：免费版 DaVinci Resolve 不开放外部脚本接口与官方 MCP，必须使用 Studio 授权版本。*
- **运行环境**：Python 3.10+（依赖 `mcp>=1.2,<3`、`pydantic>=2.6,<3`，可选 `Pillow>=10.0` 用于像素统计）、Node.js `>=22`，以及 DeepSeek Harness (`>=0.2.1-rc.2 <0.3.0-0`)。
- **开启外部脚本权限**：
  1. 打开 DaVinci Resolve Studio，进入 `DaVinci Resolve` -> `偏好设置 (Preferences)` -> `系统 (System)` -> `常规 (General)`。
  2. 将 `外部脚本使用 (External scripting using)` 设置为 **`本地 (Local)`**，点击保存后**必须重启 DaVinci Resolve**。

---

## 快速开始

### 1. 克隆仓库并构建
```bash
git clone [https://github.com/MokoKing666/dsh-davinci-qing.git](https://github.com/MokoKing666/dsh-davinci-qing.git)
cd dsh-davinci-qing
npm install && npm run build
```

### 2. 运行环境自检
确保 DaVinci Resolve Studio 处于运行状态并已打开任意工程，执行自检脚本验证双通道链路（无达芬奇环境时可加 `--simulate` 参数跑模拟自检）：
```bash
python check_env.py
```

### 3. 接入 DSH
将本项目安装至你的 DeepSeek Harness 插件目录（确保加载 `SKILL.md` 与 `dist/index.js`），重启 DSH 后即可唤醒快乐青青。

---

## 典型工作流示例

- **前期素材整备与自动分箱**
  > “先扫描 `D:\Shoot\2026_Commercial` 目录下的素材结构，在媒体池按机位自动分箱导入，然后创建一条 1080×1920 25fps 的竖屏时间线并把 A 机位素材按顺序落轨。”
- **节点级调色与视觉审片闭环**
  > “把 `D:\PowerGrades\Kodak2383_Base.drx` 应用到当前时间线所有带 `Orange` 颜色标签的片段上，给第 2 个节点打上 `BALANCE` 标签，然后截取当前帧并生成一张 6 格时间线缩略图网格帮我检查整体曝光与肤色。”
- **不移动播放头局部放大精检**
  > “用官方单帧渲染接口导出时间码 `01:00:12:08` 处人物面部的 ROI 局部放大图，确认高光细节有没有溢出。”
- **安全批量出片**
  > “读取本机可用的渲染预设，用 `H.265 Master` 预设把当前时间线加入渲染队列，输出到 `D:\Exports`，同名文件策略设为 `unique` 防止覆盖。”

---

## 配套文档

- [`SKILL.md`](./SKILL.md) —— DSH 技能声明、触发时机、工作流规范与故障速查
- [`docs/TOOLS.md`](./docs/TOOLS.md) —— 62 个 `qing_*` 工具的完整参数与返回值文档（自动生成）
- [`docs/DETAIL.md`](./docs/DETAIL.md) —— 七大模块设计细节、安装配置与安全约定
- [`docs/tool-schemas.json`](./docs/tool-schemas.json) —— 同源 JSON Schema 定义文件
- [`docs/manual-notes/`](./docs/manual-notes/) —— 基于《DaVinci Resolve 21.1 Reference Manual》（4351 页）整理的 9 篇带页码核心模块技术笔记

---

## 愿景

工具进化的终极意义，从来不是替代创作者，而是把那些枯燥、重复、消耗灵感的机械操作彻底剥离出去。

**把繁琐留给工具，把时间留给创作。无限进步。**
