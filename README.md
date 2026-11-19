# dsh-davinci-qing

> **让青青住进你的dsh** —— 为创作者打造的 DaVinci Resolve Studio 深度工作流自动化引擎。

---

## 为什么做这个项目

在日常的商业拍摄与高强度后期制作中，真正消耗创作者心力的，往往不是审美判断与叙事决策本身，而是前期素材的分类归档、多机位轨道整理、基础还原节点挂载，以及反复核对导出参数这些高度重复的机械性劳动。

我们一直在思考：**如果 AI 不再只是停留在对话框里给出建议，而是真正理解时间线的结构、看懂调色节点树的逻辑，并安全地替你完成这些繁琐的前置工作，后期体验会发生怎样的改变？**

`dsh-davinci-qing` 正是为了解决这个问题而生。它通过底层 External Scripting API（IPC 进程通信），将 DeepSeek Harness (DSH) 直接接入 **DaVinci Resolve Studio** 的媒体池、剪辑台、调色节点树、Fusion 合成与交付队列。你只需要用自然语言描述意图，它就能在你的工程里精准落地。

---

## 系统架构

项目采用 **“Cordis TypeScript 轻量转发壳 + Python 常驻 IPC 桥接运行时”** 的双层架构：

```text
DSH (Cordis)
   │  ctx.tools.register(...)  ← 43 个 qing_* 原子工具
   ▼
dist/index.js  (TypeScript 壳：纯转发，不实现业务逻辑)
   │  stdio JSON-RPC
   ▼
lib/resolve_bridge.py  (常驻 Python 进程)
   │
   ▼
lib/modules/*  (六大核心业务模块)
   │  External Scripting API
   ▼
DaVinci Resolve Studio  ← fusionscript.dll / DaVinciResolveScript
```

- **单源 Schema 零漂移**：所有 43 个工具的 JSON Schema 均由 Python 侧动态生成并注册给 DSH，TypeScript 壳内不维护第二份重复定义，从结构上杜绝“声明与实现不一致”。

---

## 核心能力（6 大模块 · 43 个原子工具）

### 1. 环境与工程诊断（4 个工具）
- **七项链路自检（`qing_resolve_health`）**：自动排查脚本路径、模块导入、IPC 连通性、Studio 版本授权、活动工程与时间线状态，失败时提供明确的中文排查指引。
- **工程全局总览与页面直达**：一键读取工程帧率、分辨率、色彩科学配置及轨道结构，支持在 `media` / `cut` / `edit` / `fusion` / `color` / `fairlight` / `deliver` 七大工作区之间无缝切换。

### 2. 媒体池与智能归档（8 个工具）
- **只读预扫与递归入库**：导入前先预扫磁盘目录统计素材体积与类型，支持按机位（`by_camera`）、日期（`by_date`）、类型或关键词自动建立子素材箱（Sub-Bin）并分类归档。
- **元数据真实表头闭环**：支持先导出元数据 CSV 并自动解析 BOM 编码回读当前工程的真实列名，再批量写入 Scene、Take、Camera # 等字段，避免因键名不匹配导致的无效写入；同时支持将素材标记区间批量转为子剪辑。

### 3. 时间线精剪与交换格式交付（13 个工具）
- **多轨道精准编排**：支持新建指定规格的时间线（涵盖 1080×1920 竖屏商业短片与各类电影画幅）、轨道追加或按 `record_frame` 精准插入、批量修改片段变换属性（Zoom、Pan/Tilt、Crop、Dynamic Zoom）。
- **标记同步与字幕挂载**：批量读写与清理彩色时间线 Marker，支持导入 SRT 字幕文件（若当前轨道环境受限则自动降级生成 Fusion 文本片段）。
- **17 种工业级交换格式导出**：原生支持 `AAF`、`EDL`、`FCP7XML`、`FCPXML (1.8/1.9/1.10)`、`OTIO`、`ALE`、`Dolby Vision (2.9/4.0/5.1)`、`HDR10` 等 17 种标准工程交换格式。

### 4. 节点级调色与视觉闭环（7 个工具）
- **标签化节点树操控**：支持解析整条时间线的节点层级与节点内工具组；通过 `qing_color_label_nodes` 为核心节点赋予稳定语义标签，摆脱删除节点后序号重排导致的寻址偏移。
- **预设挂载与跨片段复制**：支持在指定节点索引挂载 `.cube` / `.ilut` / `.olut` 3D LUT、按单片段 / 颜色标签组 / 整条时间线批量应用 `.drx` PowerGrade 预设，以及跨片段复制完整节点树。
- **帧截图视觉审片闭环（`qing_color_grab_frame`）**：自动导出当前播放头画面并附带像素统计与审片清单，让多模态模型能够直观检视曝光、高光细节与构图，实现“挂载预设 -> 截图审片 -> 参数微调”的工程闭环。

### 5. Fusion 程序化合成（3 个工具）
- **节点检视与参数批量驱动**：支持检视片段合成内的节点拓扑，动态添加 `TextPlus`、`Transform`、`ColorCorrector` 等 21 种基础节点并批量修改文本内容、字号、坐标与颜色输入。

### 6. 交付与渲染队列接管（8 个工具）
- **预设读取与底层参数直写**：动态读取本机全部可用渲染预设与编解码器矩阵，支持绕过预设直接配置 24 项底层 `RenderSettings` 字段（含色彩空间/Gamma 标签、Alpha 通道模式、字幕烧录/内嵌方式等）。
- **防覆盖队列管理**：支持 `overwrite` / `unique` / `fail` 三种同名文件保护策略，防止批量出片时静默覆盖已有成片，并支持异步启动、停止与队列状态追踪。

---

## 安全机制：工程安全永远是第一位的

在真实商业项目中，任何不可逆的工程误操作都是灾难性的。`dsh-davinci-qing` 在底层内置了四重安全护栏：

1. **强制工程存盘与时间线快照（Checkpoint）**：所有涉及时间线剪辑、调色节点树与 Fusion 合成修改的 **10 个核心写入工具**，在执行前均会强制触发 `SaveProject()`，自动克隆一条带时间戳的备份时间线（`[Backup_HHMMSS]_原时间线名`），随后切回原时间线再执行操作。若备份创建失败，系统默认拒绝执行。
2. **Pre-flight 写入前预检**：在向达芬奇发送任何修改指令前，一次性校验轨道越界、轨道锁定状态、本地 `.cube`/`.drx` 路径合法性、节点标签存在性及输出目录冲突，预检不通过绝不产生半成品写入。
3. **覆盖前状态回读**：挂载新 LUT、切换节点启用状态、修改节点标签或导出工程文件时，系统均会回读并返回被替换前的原始状态（如 `replaced_luts`、`previous_enabled`），确保每一步变更代价清晰可溯。
4. **全量操作审计日志**：每一次检查点创建与破坏性写入操作均会结构化记录至 `~/.dsh-davinci-qing/audit.jsonl`（支持通过 `QING_STATE_DIR` 环境变量自定义路径）。

---

## 能力边界与真机验证

我们坚持如实呈现工具的能力边界。受限于 Blackmagic Design 官方 External Scripting API 的接口开放范围，以下操作目前无法通过脚本直接完成：
- 调色台中的**限定器（Qualifier）取色键控**、**Power Window 窗口绘制**与**跟踪器（Tracker）解算**；
- Fusion 合成中通过脚本新建节点的**自动连线**（脚本生成的节点默认处于未连线状态，需在 Fusion 页面手动接入输出管线）；
- 读取硬件级 **10-bit 示波器 / IRE 精确读数**（截图审片工具输出的是显示参考图像与基础像素统计，不可替代专业示波器）；
- 达芬奇原生 **Undo 撤销栈**写入（脚本修改不进入 Ctrl+Z 历史记录，回滚请直接使用自动生成的 `[Backup_HHMMSS]` 备份时间线）。

> **真机实测**：本项目已在 **DaVinci Resolve Studio 21.2.0.5** 环境下完成全链路实机验证（通过 7 项环境自检、23 种渲染格式与 39 个本机预设探测、Windows UTF-16 LE BOM 元数据 CSV 解析及 EDL/工程数据导出）。

---

## 环境前置要求

- **软件版本**：**DaVinci Resolve Studio**（v18.5 / v19 / v20 / v21 及以上版本）。
  > *注：免费版 DaVinci Resolve 不开放外部脚本进程通信接口，必须使用 Studio 授权版本。*
- **运行环境**：Python 3.10+、Node.js 18+，以及 DeepSeek Harness (`>=0.2.1-rc.2 <0.3.0-0`)。
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

### 2. 运行七项环境自检
确保 DaVinci Resolve Studio 处于运行状态并已打开任意工程，执行自检脚本验证 IPC 链路：
```bash
python lib/check_env.py
```

### 3. 接入 DSH
将本项目安装至你的 DeepSeek Harness 插件目录（确保加载 `SKILL.md` 与 `dist/index.js`），重启 DSH 后即可通过自然语言唤醒青青。

---

## 典型工作流示例

- **前期素材整备与建线**
  > “先扫描 `D:\Shoot\2026_Commercial` 目录下的素材结构，在媒体池按机位自动建箱导入，然后创建一条 1080×1920 25fps 的竖屏时间线并把 A 机位素材按顺序落轨。”
- **批量挂载基础还原与胶片预设**
  > “把 `D:\PowerGrades\Kodak2383_Base.drx` 应用到当前时间线所有带 `Orange` 颜色标签的片段上，并给第 2 个节点打上 `Exposure` 标签。”
- **帧截图视觉自检与微调**
  > “截取当前播放头画面做一次审片检查，看看人物面部高光和暗部黑位是否正常，确认后把镜头画面放大到 1.05 倍消除边缘黑边。”
- **安全批量出片**
  > “读取本机可用的渲染预设，用 `H.265 Master` 预设把当前时间线加入渲染队列，输出到 `D:\Exports`，同名文件策略设为 `unique` 防止覆盖。”

---

## 配套文档

- [`SKILL.md`](./SKILL.md) —— DSH 技能声明、触发时机与故障速查手册
- [`docs/TOOLS.md`](./docs/TOOLS.md) —— 43 个 `qing_*` 工具的完整参数与返回值说明
- [`docs/DETAIL.md`](./docs/DETAIL.md) —— 底层架构设计、安全约定与已知限制详解
- [`docs/manual-notes/`](./docs/manual-notes/) —— 基于《DaVinci Resolve 21.1 Reference Manual》整理的 9 篇核心模块技术笔记

---

## 愿景

工具进化的终极意义，从来不是替代创作者，而是把那些枯燥、重复、消耗灵感的机械操作彻底剥离出去。

**把繁琐留给工具，把时间留给创作。无限进步。**
