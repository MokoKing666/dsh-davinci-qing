---
name: davinci-qing
description: 用自然语言驱动 DaVinci Resolve Studio 剪辑、调色、合成与交付。当用户要求操作 Resolve 工程（导入素材、建时间线、落片、改片段属性、打标记、导入字幕）、调色（节点树、LUT、.drx 预设、复制调色、看画面）、Fusion 文字/节点、或交付渲染（预设、队列、开跑）时加载。也用于诊断"连不上 Resolve""脚本接口没反应""这个工具报了 resolve_not_connected"这类环境问题。
whenToUse: 用户提到 DaVinci Resolve / 达芬奇 / 调色 / 时间线 / 媒体池 / 渲染交付 / LUT / PowerGrade / Fusion 文字，或要求"帮我把这段素材剪一下""让这段画面变暖一点""出个 H.264 母版"。
metadata:
  version: "1.0.0"
  plugin: dsh-davinci-qing
  tool-prefix: qing_
  slogan: 让青青住进你的dsh
user-invocable: true
---

# DaVinci Resolve 青青桥接（davinci-qing）

**让青青住进你的dsh。** 本技能把 DeepSeek Harness 接到 DaVinci Resolve Studio（v18.5 / v19 / v20 / v21+）。

## 插件架构

本插件是 **Cordis TS 壳 + Python Resolve 运行时**：

| 层 | 位置 | 职责 |
| --- | --- | --- |
| Cordis 入口 | `dist/index.js`（源码 `src/index.ts`） | 注册 36 个工具、托管 Python 子进程 |
| Python 桥 | `lib/resolve_bridge.py` | 常驻 JSON-RPC，转发工具调用 |
| 业务模块 | `lib/modules/` | IPC 连接、安全护栏、媒体池、时间线、调色、Fusion/交付 |
| 技能声明 | `SKILL.md`（本文件） | 触发条件、工作流、故障排查 |

TS 侧**不重复实现业务逻辑**——工具 schema 全部来自 Python，因此两边永不漂移。

## 一、每次会话的固定开场

1. 调 `qing_resolve_health`。
   - `ok: true` → 记下 `project_name` / `timeline_name`，继续。
   - `ok: false` → **不要重试**，把 `hints` 逐条翻译给用户。
2. 目标是陌生工程时，调 `qing_project_overview` 拿时间线清单。
3. 需要精确轨道/片段序号时，调 `qing_timeline_inspect`。

**永远不要假设**轨道号、片段序号、节点数、素材名——先读后写。
拿到 `preflight_failed` 时读 `detail.violations[].hint`，逐条修正，不要盲改重试。

## 二、工具的副作用分级（调用前必读）

| 级别 | 工具 | 代价 |
| --- | --- | --- |
| 只读 | `qing_resolve_health` `qing_resolve_version` `qing_project_overview` `qing_media_scan_folder` `qing_media_list_bins` `qing_media_search` `qing_timeline_inspect` `qing_timeline_get_clip_properties` `qing_timeline_read_markers` `qing_color_inspect_nodes` `qing_fusion_inspect` `qing_deliver_list_presets` `qing_deliver_queue_status` | 无写入 |
| 普通写入 | `qing_media_import_folder` `qing_media_markers_to_subclips` `qing_timeline_create` `qing_timeline_add_clips` `qing_timeline_add_markers` `qing_timeline_import_srt` `qing_timeline_set_track_state` `qing_open_page` | 新增内容，可删回 |
| 破坏性 + 自动检查点 | `qing_media_auto_bin` `qing_timeline_set_clip_properties` `qing_timeline_delete_clips` `qing_color_set_lut` `qing_color_set_node_enabled` `qing_color_copy_grade` `qing_color_apply_drx` `qing_fusion_add_tool` `qing_fusion_set_input` | 覆盖既有内容；先 `SaveProject` + 复制 `[Backup_HHMMSS]_原时间线名` |
| 触发外部重活 | `qing_deliver_start` `qing_deliver_stop` `qing_deliver_delete_jobs` `qing_color_grab_frame` | 真实渲染 / 写盘 / 切页 |

破坏性工具都支持 `dry_run: true` 与 `allow_without_backup`（默认 false，备份失败即拒绝执行——不要为了"让它过"就随便打开）。

## 三、五条常用工作流

### 1. 素材入库 → 分箱 → 上时间线

```
qing_media_scan_folder(folder)                 # 先看有多少、什么类型
qing_media_import_folder(folder, bin_path)     # 递归导入
qing_media_auto_bin(rule="by_camera")          # 按机位分箱（也可 by_date/by_kind/by_keyword）
qing_timeline_create(name, width, height, fps) # 竖屏就写 1080x1920
qing_timeline_add_clips(placements=[...])      # 给 record_frame 就是精准插入
```

### 2. 调色闭环（核心）

```
qing_color_inspect_nodes()                                  # 看节点数与标签
qing_color_apply_drx(drx_path, scope="whole_timeline")      # Log 还原 / 胶片模拟
qing_color_grab_frame(tag="after_drx")                      # 取图（图片直接返回）
→ 按 review_checklist 判断曝光 / 高光 / 肤色 / 黑位 / 构图
qing_timeline_set_clip_properties(properties={...})         # 微调
qing_color_grab_frame(tag="after_tweak")                    # 再取一张对比
```

审片按五项逐个说结论，不要把"看起来还行"当结论。
高光溢出 → 降高光/加节点 Gamma；偏绿 → 调 Temp/Tint；构图偏上 → Pan/Tilt 或 Crop。

### 3. 标记与字幕

```
qing_timeline_add_markers(markers=[{frame, color, name, note, duration}])
qing_timeline_read_markers(color="Red")
qing_timeline_import_srt(srt_path)    # 或传 cues=[{text,start_seconds,end_seconds}]
```

### 4. Fusion 文字/角标

```
qing_fusion_inspect()                                       # 先看有没有合成、节点名
qing_fusion_add_tool("TextPlus", inputs=[{name:"StyledText", value:"青青"}])
qing_fusion_set_input("TextPlus1", inputs=[{name:"Size", value:0.08}])
```

### 5. 交付

```
qing_deliver_list_presets()                                 # 预设名因机器而异，必须现读
qing_deliver_setup_job(jobs=[{target_dir, filename, preset}])# 只入队
qing_deliver_queue_status()                                 # 核对
qing_deliver_start(wait_seconds=0)                          # 开跑，长任务不要死等
```

## 四、领域词汇速查

- **轨道号 1 基**：video1 / audio1 / subtitle1。`GetTrackCount` = 3 表示 1..3。
- **片段序号 0 基**：`item_index` / `item_indexes` 是"该轨道上按起始帧排序后的第几个"。
- **节点序号 1 基**：`node_index` 从 1 到 `GetNumNodes()`。
- **帧号**：绝对帧，`GetStartFrame()` 起算；`timecode` 由工具换算好。
- **ClipColor**：Orange / Apricot / Yellow / Lime / Olive / Green / Teal / Navy / Blue /
  Purple / Violet / Pink / Tan / Beige / Brown / Chocolate。
- **MarkerColor**：Blue / Cyan / Green / Yellow / Red / Pink / Purple / Fuchsia / Rose /
  Lavender / Sky / Mint / Lemon / Sand / Cocoa / Cream。
- **gradeMode**（`qing_color_apply_drx`）：0 无关键帧 / 1 按源时间码 / 2 按起始帧。

## 五、故障速查

| 返回的 error | 该怎么办 |
| --- | --- |
| `resolve_not_installed` | 装 Studio 版；非默认盘就设 `RESOLVE_SCRIPT_API` / `RESOLVE_SCRIPT_LIB` |
| `resolve_not_connected` | 启动 Resolve 并等加载完；`首选项 → 系统 → 常规 → External scripting using → Local`；**改完必须重启 Resolve** |
| `resolve_free_version` | 免费版没有外部脚本接口，必须 Studio |
| `no_project` / `no_timeline` | 让用户先建工程 / 建时间线 |
| `preflight_failed` | 读 `detail.violations[]`，逐条按 `hint` 修正 |
| `backup_failed` | 备份建不出来（协作锁定/只读）。修好后重试；**不要**默认开 `allow_without_backup` |
| `unsupported` | 当前版本没有该 API（v19 以下没有 `GetNodeGraph`），按 `hint` 里的替代方案做 |
| `api_failure` | 参数被 Resolve 拒绝；先 `qing_*_inspect` 读实际状态再改 |
| `bridge_failure` | Python 桥断了；调一次 `qing_resolve_health`，仍失败就重启 DSH 让插件重拉 Python |

## 六、边界

- 不修改 Resolve 偏好设置、不点界面按钮、不替用户决定渲染参数。
- 不在用户没要求时启动渲染（`qing_deliver_start` 必须由用户明确说"开跑"）。
- Resolve 脚本操作**不进撤销栈**，回滚只有备份时间线一条路——别跳过检查点。
- 详细参数表见 `docs/TOOLS.md`；同源 JSON Schema 见 `docs/tool-schemas.json`。
