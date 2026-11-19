# dsh-davinci-qing 工具目录（v1.0.4）

> 让青青住进你的dsh

共 **62** 个 MCP 工具。本文档由 `export_schema.py` 自动生成，请勿手工编辑；参数表与 JSON Schema 同源。

图例：🔍 只读 · ⚠️ 破坏性（会改动工程内容） · 💾 自动检查点（执行前 SaveProject + 复制时间线）


## 环境与工程

### `qing_open_page` — 切换 Resolve 页面

把 DaVinci Resolve 界面切到指定页面（媒体/剪辑/剪辑页/ Fusion/调色/ Fairlight/交付）。

- 【副作用】只切换界面页面，不改动工程数据；但会打断用户当前的鼠标焦点。
- 【触发时机】调用只在特定页面行为正确的接口前（如导出当前帧、渲染相关操作）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `page` | string | 是 | 目标页面：media / cut / edit / fusion / color / fairlight / deliver。 |

### `qing_project_overview` — 工程与时间线总览 🔍

列出当前工程的名称、帧率、分辨率，全部时间线（名称/时长/轨道数），以及当前时间线的片段与标记数量。

- 【副作用】只读。
- 【触发时机】接手一个陌生工程、或批量操作前确认目标对象时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `include_timeline_items` | boolean | 否 | 是否把当前时间线的片段明细也一并返回（片段多时输出会变长）。（默认 false） |
| `item_limit` | integer | 否 | include_timeline_items 为真时最多返回多少个片段。（默认 40） |

### `qing_resolve_health` — Resolve 环境自检 🔍

环境自检：定位脚本 API 与 fusionscript 动态库、连接 Resolve、确认 Studio 版、检查是否有打开的工程与活动时间线，并给出中文排查提示。同时报告官方 Resolve MCP（21.1+ 自带）是否可用——它补的是脚本 API 读不到的「调色参数实际取值」与「整条时间线缩略图网格」。

- 【副作用】只读。仅当显式传 page 参数时会把界面切到该页面（不改动工程内容）。
- 【触发时机】会话开始时、连接失败后、或任何工具报 resolve_not_connected 之后先调用一次。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `open_page` | string | 否 | 可选：自检通过后把界面切到该页面（media/cut/edit/fusion/color/fairlight/deliver）。 |
| `reset_connection` | boolean | 否 | 为 true 时丢弃缓存的连接对象后重新握手，用于 Resolve 重启后的恢复。（默认 false） |

### `qing_resolve_version` — 读取 Resolve 版本 🔍

读取 DaVinci Resolve 的产品名、版本号、是否需要 Studio 授权，以及当前所在页面。

- 【副作用】只读，不改动工程。
- 【触发时机】需要按版本决定能力（如 v19/v20/v21 差异）时。


## 媒体池与归档

### `qing_media_auto_bin` — 按机位/日期自动分箱 ⚠️

把媒体池里的素材按规则移动进子素材箱：by_camera（文件名机位前缀）、by_date（文件名或修改时间里的拍摄日期）、by_kind（视频/音频/静帧）、by_keyword（元数据 Keywords 的第一段）。只移动素材，不复制。

- 【副作用】批量移动素材池条目（MoveClips）。不改动磁盘文件，也不影响已存在于时间线上的片段。
- 【触发时机】一次拍摄结束后整理媒体池；或导入了一坨素材需要按机位/日期分组时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `rule` | enum | 是 | 分箱规则。 |
| `source_bin` | string | 否 | 要处理的素材箱路径（默认整个 Master 根，递归）。 |
| `target_prefix` | string | 否 | 新素材箱的父路径，如 'Master/Imported'；留空则建在源素材箱下。 |
| `dry_run` | boolean | 否 | 为 true 时只返回分组计划，不移动。（默认 false） |

### `qing_media_export_metadata` — 导出素材元数据表

把媒体池素材的元数据导出成 CSV（MediaPool.ExportMetadata），并回读表头把**真实字段名**列出来。Resolve 的元数据字段在不同版本/不同素材类型下不一致（Description 与 Comments 是两个字段、Camera # 的键名就是 Camera），猜键名常常白写；先导出表头，再照着用 qing_media_set_metadata。注意两点：① 列集合取决于媒体页当前摆出的元数据列，实测 21.2 默认 22 列且**不含** Scene/Take，想导场记字段得先在媒体页把对应列调出来；② Windows 上这份 CSV 是 UTF-16 LE + BOM，用 Excel 打开正常，但用脚本读要按 UTF-16 解码（本工具已回读好表头与样例行）。

- 【副作用】在磁盘上写出一个 CSV（不改素材池里的任何内容）。
- 【触发时机】批量写元数据之前先确认字段名；或把场记表交给 Excel 校对时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `target_path` | string | 是 | 导出 CSV 的绝对路径（含 .csv 扩展名）。 |
| `clip_names` | string[] | 否 | 只导出这些素材；留空导出整个媒体池。 |
| `bin_path` | string | 否 | 只导出该素材箱下的素材（默认全池）。 |

### `qing_media_import_folder` — 递归导入素材到媒体池

递归扫描目录并把视频/音频/静帧一次性导入指定素材箱（自动创建不存在的子素材箱）。单次最多导入 500 个文件，超出会先报错让你缩小范围。

- 【副作用】向媒体池写入新素材（可撤销：删除导入的素材箱即可），不改动时间线。
- 【触发时机】素材还在盘上、尚未进入媒体池时。重复导入同名同路径素材时 Resolve 会复用已有条目。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `folder` | string | 是 | 素材所在目录的绝对路径。 |
| `bin_path` | string | 否 | 目标素材箱路径，如 'Master/2026-03-15/A_Cam'；留空导入当前素材箱。 |
| `recursive` | boolean | 否 | 是否递归扫描子目录。（默认 true） |
| `kinds` | enum[] | 否 | 只导入这些类型；留空表示全部。 |
| `max_files` | integer | 否 | 本次最多导入多少个文件（上限 500）。（默认 500） |
| `dry_run` | boolean | 否 | 为 true 时只做预检并列出将要导入的文件，不写入媒体池。（默认 false） |

### `qing_media_list_bins` — 列出素材箱树 🔍

列出媒体池的素材箱层级与每个箱子里的素材数量、名称样例。

- 【副作用】只读。
- 【触发时机】导入/分箱之后核对结构，或需要拿到准确的素材箱路径时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `max_depth` | integer | 否 | 递归深度（1 表示只看根层）。（默认 3） |
| `sample` | integer | 否 | 每个素材箱返回多少个素材名。（默认 3） |

### `qing_media_markers_to_subclips` — 按标记生成子剪辑

读取某条素材（或当前时间线片段所用素材）上的标记，把每段标记区间导成子剪辑放进媒体池。用于挑条、卡点切片、审片修改点的批量落库。

- 【副作用】向媒体池写入新的子剪辑条目（原素材不变）。
- 【触发时机】素材上已经打好标记、需要把标记区间变成可单独剪辑的片段时间。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `clip_name` | string | 是 | 源素材名称或文件路径（媒体池里的条目）。 |
| `bin_path` | string | 否 | 子剪辑放入的素材箱路径；留空为当前素材箱。 |
| `default_duration` | integer | 否 | 标记没有时长时，子剪辑取多少帧。（默认 24） |
| `min_gap` | integer | 否 | 标记间隔小于该帧数时合并成一条子剪辑。（默认 0） |
| `dry_run` | boolean | 否 | 为 true 时只返回将要生成的区间。（默认 false） |

### `qing_media_scan_folder` — 预扫素材目录 🔍

只读扫描本地目录，统计可导入的视频/音频/静帧数量、总大小，并按机位与日期给出分箱建议。不会导入任何素材。

- 【副作用】只读文件系统，不触碰 Resolve 工程。
- 【触发时机】批量导入之前先用它确认路径正确、素材数量与预期一致。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `folder` | string | 是 | 要扫描的本地目录绝对路径。 |
| `recursive` | boolean | 否 | 是否递归扫描子目录。（默认 true） |
| `limit` | integer | 否 | 最多返回多少个文件的明细。（默认 50） |

### `qing_media_search` — 在媒体池里搜索素材 🔍

按名称/路径/元数据关键字在媒体池里检索素材，返回名称、路径、时长与所在素材箱。

- 【副作用】只读。
- 【触发时机】需要确认素材名、挑选目标素材、或在批量操作前核对范围时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 关键词（不区分大小写），可以是素材名片段、路径片段或元数据值。 |
| `bin_path` | string | 否 | 限定素材箱路径；留空搜全池。 |
| `limit` | integer | 否 | 最多返回条目数。（默认 30） |

### `qing_media_set_metadata` — 批量写素材元数据 ⚠️

给指定素材（按名称/路径匹配）批量写入 Scene、Take、Camera #、Keywords、Description、Shot 等元数据字段。匹配不到或写入失败的素材会逐条回报。

- 【副作用】修改媒体池条目的元数据字段（可被再次覆盖，属于破坏性写入）。
- 【触发时机】场记信息导入后需要落库、或按场次/机位整理素材时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `metadata` | object | 是 | 要写入的字段，如 {"Scene": "12A", "Take": "3", "Camera #": "A"}。 |
| `clip_names` | string[] | 否 | 要处理的素材名或文件路径；留空表示整个媒体池的所有素材。 |
| `bin_path` | string | 否 | 只处理该素材箱下的素材（默认全池递归）。 |
| `dry_run` | boolean | 否 | 为 true 时只返回将要写入的素材清单。（默认 false） |


## 时间线编辑

### `qing_timeline_add_clips` — 向轨道追加/插入片段

按指令列表把媒体池素材放进时间线的指定视频/音频轨道：给 record_frame 就是精准插入（后面的片段自动让位），不给就是追加到轨道末尾。一个调用里可以混排多条指令。

- 【副作用】向时间线写入片段（不改动已有片段内容；插入会产生位移）。
- 【触发时机】粗剪落片、补空镜、给音乐卡点铺素材时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `placements` | ClipPlacement[] | 是 | 片段放置指令列表。 |
| `timeline_name` | string | 否 | 目标时间线名称；留空为当前时间线。 |
| `bin_path` | string | 否 | 在这些素材箱里找素材（默认全池递归搜索）。 |
| `dry_run` | boolean | 否 | 为 true 时只做预检并回报将要执行的动作。（默认 false） |

<details><summary>参数结构体</summary>

**`ClipPlacement`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `clip_name` | string | 是 | 媒体池里的素材名或文件路径。 |
| `track_type` | enum | 否 | 放到哪类轨道。（默认 'video'） |
| `track_index` | integer | 否 | 轨道序号（1 基）。（默认 1） |
| `record_frame` | integer | 否 | 插入位置的帧号；留空表示追加到该轨道末尾。给了就是精准插入，后面的片段会自动让位。 |
| `start_frame` | number | 否 | 源素材入点（帧），留空表示从素材开头取。 |
| `end_frame` | number | 否 | 源素材出点（帧），留空表示取到素材结尾。 |

</details>

### `qing_timeline_add_markers` — 批量添加彩色标记

在时间线上批量添加彩色标记（帧位置 + 颜色 + 标题 + 备注 + 持续帧数 + 自定义数据）。适合音乐卡点、审片修改定位、场记对应。重复帧号的标记会被覆盖。

- 【副作用】在时间线标记轨上写入标记（同一帧的旧标记会被新的覆盖）。
- 【触发时机】卡点打点、审片意见落点、批量生成场记标记之后。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `markers` | MarkerSpec[] | 是 | 要添加的标记列表。 |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_past_end` | boolean | 否 | 是否允许标记落在时间线终点之后（尾部预留标记）。（默认 false） |
| `clear_color_first` | string | 否 | 可选：先删除该颜色的所有现有多标记（如 'Red'），避免重复标记。 |

<details><summary>参数结构体</summary>

**`MarkerSpec`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `frame` | integer | 是 | 标记所在帧号（时间线绝对帧）。 |
| `color` | enum | 否 | 标记颜色（Resolve 的 16 种标记色之一）。（默认 'Blue'） |
| `name` | string | 否 | 标记标题（会显示在时间线标记气泡上）。（默认 ''） |
| `note` | string | 否 | 备注文字，适合写审片意见 / 卡点说明。（默认 ''） |
| `duration` | integer | 否 | 标记持续帧数，默认 1 帧。（默认 1） |
| `custom_data` | string | 否 | 自定义数据（不在界面显示，可放机器可读的 JSON）。 |

</details>

### `qing_timeline_create` — 新建时间线

新建一条时间线：可指定名称、分辨率、帧率；可选择用媒体池里已选中的素材或指定素材箱的素材自动填充。

- 【副作用】在工程里新增一条时间线并把它设为当前时间线（不删除任何已有内容）。
- 【触发时机】开始一个新剪辑任务、或需要为不同交付版本（16:9 / 竖屏 / 4K）各建一条时间线时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 新时间线名称（工程内必须唯一）。 |
| `width` | integer | 否 | 时间线宽度（像素）。（默认 1920） |
| `height` | integer | 否 | 时间线高度（像素）。（默认 1080） |
| `fps` | number | 否 | 帧率，支持 24 / 25 / 23.976 / '29.97 DF' 等写法。（默认 24.0） |
| `from_media_pool` | boolean | 否 | 为 true 时用媒体池当前选中的素材建时间线；false 则建空时间线。（默认 false） |
| `bin_path` | string | 否 | 配合 from_media_pool：改用该素材箱下的全部素材（按名称排序）建时间线。 |

### `qing_timeline_delete_clips` — 批量删除片段时间线片段 ⚠️ 💾

删除指定轨道上的片段（可选 ripple 删除）。执行前会强制建立检查点（保存工程 + 复制时间线）。

- 【副作用】从时间线上移除片段（不可用 Resolve 撤销恢复脚本操作，只能靠备份时间线回滚）。
- 【触发时机】重剪、清理空轨道、批量删错素材时。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `track_type` | enum | 否 | 轨道类型。（默认 'video'） |
| `track_index` | integer | 否 | 轨道序号（1 基）。（默认 1） |
| `item_indexes` | integer[] | 否 | 要删除的片段序号（0 基，按起始帧排序）。（默认 []） |
| `ripple` | boolean | 否 | 删除后是否自动前移后续片段（闭合空隙）。（默认 false） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |
| `dry_run` | boolean | 否 | 为 true 时只回报将要删除的片段。（默认 false） |

### `qing_timeline_delete_markers` — 删除时间线标记 ⚠️ 💾

按颜色、按帧号或按 customData 删除标记。三种模式互斥：by_color 删除整条时间线上该颜色的全部标记（传 all 等于清空所有标记，很危险）；by_frame 只删指定帧上的标记；by_custom_data 按自定义数据精确匹配。适合审片意见改完一轮后清场，或重打同色卡点前先清空。注意：标记不参与渲染，但删掉后只能靠备份时间线找回。

- 【副作用】从时间线上移除标记（删掉后只能靠备份时间线找回）。
- 【触发时机】重打标记、清理审片意见、批量删错颜色时。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `mode` | enum | 否 | 删除方式：按颜色 / 按帧号 / 按 customData。（默认 'by_color'） |
| `color` | string | 否 | by_color 模式必填：标记颜色，取 MarkerColor 的 16 色之一（Blue/Cyan/Green/Yellow/Red/Pink/Purple/Fuchsia/Rose/Lavender/Sky/Mint/Lemon/Sand/Cocoa/Cream）；传 all 会删除全部标记。 |
| `frames` | integer[] | 否 | by_frame 模式必填：要删标记的帧号列表（时间线内的绝对帧号）。（默认 []） |
| `custom_data` | string | 否 | by_custom_data 模式必填：标记的 customData 字符串（精确匹配）。 |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |
| `dry_run` | boolean | 否 | 为 true 时只回报将被删除的标记。（默认 false） |

### `qing_timeline_export` — 导出时间线交换格式

把时间线导出成 AAF / FCP7 XML / FCPXML / EDL / OTIO / ALE / CDL / Dolby Vision / HDR10 等交换文件，用于交给 Pro Tools、Premiere、Final Cut 或归档。只导出剪辑结构，不含渲染画面（要画面请用交付队列）；文件已存在时会被直接覆盖。

- 【副作用】在磁盘上写出一个交换文件（不修改工程与时间线，但会覆盖同名文件）。
- 【触发时机】跨软件交接、声音回批、交付归档时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `target_path` | string | 是 | 导出文件的绝对路径（含扩展名，如 D:\out\cut.aaf）。 |
| `export_type` | enum | 否 | 导出格式。aaf 给 Pro Tools，fcp7xml/fcpxml 给其他 NLE，edl 给套底清单，otio 给流程中间格式。（默认 'aaf'） |
| `export_subtype` | enum | 否 | 子类型：aaf 用 aaf_new（新建）或 aaf_existing（链接已有），CDL/SDL 配 ale_cdl，其余用 none。（默认 'none'） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |

### `qing_timeline_get_clip_properties` — 读取片段属性 🔍

读取时间线上某片段（或某轨道上的第 N 个片段 / 播放头所在片段）的变换、裁切、动态缩放等属性的当前值。

- 【副作用】只读。
- 【触发时机】修改属性前先看原值，或需要复刻某个片段的构图参数时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `item_index` | integer | 否 | 片段在该轨道上的序号（从 0 开始）；留空则取播放头所在片段。 |
| `track_type` | enum | 否 | 轨道类型。（默认 'video'） |
| `track_index` | integer | 否 | 轨道序号（1 基）。（默认 1） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |

### `qing_timeline_import_srt` — 导入 SRT 字幕

把 SRT 字幕文件导入时间线生成字幕轨道；也可以直接给条目列表（自动写成临时 SRT）。优先走 Timeline.ImportIntoTimeline（v18.5+ 原生字幕轨），不支持时改用 Fusion 文本片段兜底。

- 【副作用】向时间线新增字幕/文本片段轨道（不改动已有视频音频）。
- 【触发时机】交付需要内嵌字幕、或客户给了 SRT 需要落到时间线上时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `srt_path` | string | 否 | SRT 文件绝对路径；与 cues 二选一。 |
| `cues` | SubtitleCueSpec[] | 否 | 直接给字幕条目（秒为单位）；会生成临时 SRT 再导入。 |
| `timeline_name` | string | 否 | 目标时间线名称；留空为当前时间线。 |
| `frame_offset` | integer | 否 | 整体平移帧数（时间线不是从 0 秒开始对齐时用）。（默认 0） |
| `dry_run` | boolean | 否 | 为 true 时只解析并回报字幕条目，不导入。（默认 false） |

<details><summary>参数结构体</summary>

**`SubtitleCueSpec`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string | 是 | 字幕文字（可含换行）。 |
| `start_seconds` | number | 是 | 起始时间（秒）。 |
| `end_seconds` | number | 是 | 结束时间（秒），必须大于起始时间。 |

</details>

### `qing_timeline_inspect` — 读取时间线结构 🔍

读取当前（或指定）时间线：帧率、起止帧、各轨道的片段与轨道状态、标记列表。这是所有时间线类操作前的看地图工具。

- 【副作用】只读。
- 【触发时机】准备插入/修改/删除片段之前，先确认轨道号与片段序号。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `include_items` | boolean | 否 | 是否返回片段明细。（默认 true） |
| `item_limit` | integer | 否 | 最多返回多少个片段。（默认 60） |
| `include_markers` | boolean | 否 | 是否返回标记列表。（默认 true） |

### `qing_timeline_mark_inout` — 读取/设置/清除入出点

入出点决定交付与导出的范围（Deliver 的 MarkIn/MarkOut 就取自这里）。action=get 读当前入出点（含时间码换算），set 用帧号设定，clear 清除。注意脚本接口只吃帧号，不接受时间码字符串；要按时间码给点，先用 qing_timeline_read_markers 之类确认帧号。

- 【副作用】改变时间线的入出点标记（会影响后续渲染与导出的范围）。
- 【触发时机】只想交付时间线中的某一段而不是整条时间线时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `action` | enum | 否 | 操作：读取 / 设置 / 清除。（默认 'get'） |
| `mark_in` | integer | 否 | set 时必填：入点帧号（时间线绝对帧）。 |
| `mark_out` | integer | 否 | set 时必填：出点帧号，必须大于入点。 |
| `mark_type` | enum | 否 | 作用范围：视频 / 音频 / 两者（默认两者）。（默认 'all'） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |

### `qing_timeline_read_markers` — 读取时间线标记 🔍

读取时间线上全部标记（帧号、颜色、标题、备注、持续帧数、自定义数据）。

- 【副作用】只读。
- 【触发时机】给标记做二次加工（如按颜色筛选、转成剪辑点）之前。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `color` | string | 否 | 只看该颜色的标记。 |

### `qing_timeline_set_clip_properties` — 批量修改片段属性 ⚠️ 💾

对一批片段批量写入变换/裁切/动态缩放属性（ZoomX/Y、Pan/Tilt、RotationAngle、Crop*、DynamicZoom*、Opacity 等）。会自动打开对应属性区块的总开关。Resolve 的 SetProperties 是全量校验：任一键非法则该片段整体不变。

- 【副作用】覆盖片段的变换/裁切参数（破坏性：原有数值会被替换）。
- 【触发时机】统一构图（如全部推近 110%）、竖屏适配、批量恢复默认值、给一组镜头统一裁切时。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `properties` | object | 是 | 一批片段属性覆盖值。 |
| `item_indexes` | integer[] | 否 | 片段序号列表（0 基，按轨道上起始帧排序）；留空表示整条轨道。 |
| `track_type` | enum | 否 | 轨道类型。（默认 'video'） |
| `track_index` | integer | 否 | 轨道序号（1 基）。（默认 1） |
| `whole_timeline` | boolean | 否 | 为 true 时对整条时间线的所有视频片段生效（忽略 track_index / item_indexes）。（默认 false） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_without_backup` | boolean | 否 | 备份时间线创建失败时是否仍继续（默认 false：拒绝执行）。（默认 false） |
| `dry_run` | boolean | 否 | 为 true 时只回报将要修改的片段清单。（默认 false） |

<details><summary>参数结构体</summary>

**`PropertyPatch`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ZoomX` | number | 否 | 水平缩放，1.0 = 原始大小，取值 0.0–100.0 |
| `ZoomY` | number | 否 | 垂直缩放，1.0 = 原始大小，取值 0.0–100.0 |
| `ZoomGang` | boolean | 否 | 是否联动 X/Y 缩放 |
| `Pan` | number | 否 | 水平位移，单位像素（-4×宽 ~ 4×宽） |
| `Tilt` | number | 否 | 垂直位移，单位像素（-4×高 ~ 4×高） |
| `RotationAngle` | number | 否 | 旋转角度（度），-360 ~ 360 |
| `AnchorPointX` | number | 否 | 锚点 X（像素） |
| `AnchorPointY` | number | 否 | 锚点 Y（像素） |
| `CropLeft` | number | 否 | 左裁切（像素） |
| `CropRight` | number | 否 | 右裁切（像素） |
| `CropTop` | number | 否 | 上裁切（像素） |
| `CropBottom` | number | 否 | 下裁切（像素） |
| `CropSoftness` | number | 否 | 裁切边缘羽化，-100 ~ 100 |
| `CropRetain` | boolean | 否 | 保留图像位置 |
| `DynamicZoomEnabled` | boolean | 否 | 开启动态缩放（推/拉镜头） |
| `DynamicZoomEase` | integer | 否 | 动态缩放缓动：0 线性 / 1 缓入 / 2 缓出 / 3 缓入缓出 |
| `Opacity` | number | 否 | 不透明度 0–100 |
| `TransformEnabled` | boolean | 否 | 启用变换区间 |
| `CroppingEnabled` | boolean | 否 | 启用裁切区间 |
| `CompositeEnabled` | boolean | 否 | 启用合成区间 |
| `FlipX` | boolean | 否 | 水平翻转 |
| `FlipY` | boolean | 否 | 垂直翻转 |

</details>

### `qing_timeline_set_track_state` — 设置轨道锁定/启用

开启或关闭某条轨道的锁定、显示/启用状态，并可重命名轨道。

- 【副作用】改变轨道状态。锁定后任何写入该轨道的操作都会被 Pre-flight 拦下。
- 【触发时机】批量写入前临时解锁目标轨道；或交付前关掉某些轨道。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `track_type` | enum | 是 | 轨道类型。 |
| `track_index` | integer | 是 | 轨道序号（1 基）。 |
| `locked` | boolean | 否 | 锁定状态；留空表示不改。 |
| `enabled` | boolean | 否 | 启用（显示）状态；留空表示不改。 |
| `name` | string | 否 | 新的轨道名；留空表示不改。 |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |


## Color 调色

### `qing_color_apply_drx` — 应用 .drx 预设（PowerGrade） ⚠️ 💾

把 .drx 静帧预设（PowerGrade）应用到片段、指定颜色标签的片段组或整条时间线。适合 Log 还原、胶片模拟等完整节点流的批量落地。注意：.drx 只包含调色与节点结构、不含画面；默认只作用于当前 Node Editor 模式（Clip Grade 或 Timeline Grade）那一套节点，需要全部图层时把 apply_to_all_layers 设为 true。

- 【副作用】在目标片段节点图末尾叠加/应用 DRX 的节点流（破坏性，会改变画面）。
- 【触发时机】导入胶片模拟预设、统一 Log→709 管线、把某个镜头的外观复制给整场时。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `drx_path` | string | 是 | .drx 静帧预设的绝对路径。 |
| `scope` | enum | 否 | 作用范围：current_clip（播放头片段）/ clip_group（按颜色标签）/ whole_timeline / item_index。（默认 'current_clip'） |
| `clip_color` | string | 否 | scope=clip_group 时必填，如 'Orange'。 |
| `item_index` | integer | 否 | scope=item_index 时的片段序号（0 基，视频轨道 1）。 |
| `grade_mode` | enum | 否 | 关键帧对齐方式：0 无关键帧 / 1 按源时间码 / 2 按起始帧。（默认 0） |
| `apply_to_all_layers` | boolean | 否 | 为 true 时把预设应用到这个片段的**所有图层**；默认只作用于当前 Node Editor 模式（Clip Grade 或 Timeline Grade）那一套节点。（默认 false） |
| `keyframe_mode` | enum | 否 | 带关键帧时对哪一类关键帧生效：all（全部）/ color（仅调色）/ sizing（仅缩放变换）。（默认 'all'） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |
| `dry_run` | boolean | 否 | 为 true 时只回报目标片段。（默认 false） |

### `qing_color_copy_grade` — 批量复制调色 ⚠️ 💾

把源片段（或播放头所在片段）的完整调色节点树复制到一批目标片段（``CopyGrades``）。常用于整场戏统一基础调色后逐条做局部调整。注意：这是**整体覆盖**——目标片段原有的 grade 会被换掉；Resolve 的 API 没有「保留目标节点、只粘贴部分」的选项，关键帧也不会带过去，需要的话得手动补。

- 【副作用】覆盖目标片段的全部调色节点（破坏性）。
- 【触发时机】同一机位/同一场景多个镜头需要统一基底，再做局部微调时。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source_item_index` | integer | 否 | 源片段序号（0 基，视频轨道 1）；留空取播放头所在片段。 |
| `target_item_indexes` | integer[] | 否 | 目标片段序号列表（0 基，视频轨道 1）；留空配合 whole_timeline 使用。 |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `whole_timeline` | boolean | 否 | 为 true 时把源片段调色复制到整条时间线的所有其它视频片段。（默认 false） |
| `clip_color` | string | 否 | 只复制给带该颜色标签的片段（如 'Blue'）。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |
| `dry_run` | boolean | 否 | 为 true 时只回报将要覆盖的片段。（默认 false） |

### `qing_color_grab_frame` — 导出当前帧做视觉审片

把当前播放头画面导出为 PNG/JPG 并把图片直接返回（多模态可直接查看），同时给出文件路径，以及一份从导出文件量出来的像素统计（死白/死黑面积、亮度分位数、饱和度），用于检查曝光、高光、肤色与构图，形成 挂载预设 → 截图审片 → 参数微调 的闭环。注意图片是「显示参考」，不是示波器：精确的信号级判断仍以 Resolve 的波形图为准。

- 【副作用】在临时目录写一张静帧文件；会临时把界面切到调色页（不修改工程内容）。
- 【触发时机】应用 LUT / DRX / 复制调色之后确认画面；或需要给用户展示某一刻构图时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_format` | enum | 否 | 导出格式。（默认 'png'） |
| `tag` | string | 否 | 文件名标签，便于区分多张截图（如 'after_lut'）。（默认 'frame'） |
| `out_dir` | string | 否 | 导出目录；留空用临时目录。 |
| `also_grab_selected` | boolean | 否 | 为 true 时额外导出时间线上前 N 个片段的首帧对比图。（默认 false） |
| `compare_count` | integer | 否 | also_grab_selected 时导出多少个片段。（默认 3） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `grid_count` | integer | 否 | 大于 0 时改为返回一张**缩略图网格**（一次覆盖整条时间线的 N 个等距帧，1–48），走官方 Resolve MCP 的 get_thumbnails。比逐帧导静帧快一个数量级，适合「这条时间线整体什么调性」这类问题；需要 Resolve 21.1+ 的官方 MCP。（默认 0） |
| `grid_max_dimension` | integer | 否 | grid_count 生效时整张网格图的长边像素上限。（默认 1568） |

### `qing_color_inspect_nodes` — 解析片段节点树 🔍

读取片段（或整条时间线）的调色节点树：节点数量、标签与色标、启用状态、挂载的 LUT、节点内改过的工具组。可批量覆盖多条片段，便于判断调色是否统一。返回的 index 是 1 基序号，**删节点后会整体重排**；要长期引用某个节点请用 label。传 include_grade_detail=true 时会额外读取官方 Resolve MCP 的 describe_grade，拿到脚本 API 读不到的东西：每个节点里参数**实际偏离默认值多少**、以及调过色节点的 ASC CDL 等效值（需 Resolve 21.1+ 且装了官方 MCP，否则该项会被跳过并说明原因）。

- 【副作用】只读（唯一例外：某些版本读取节点 LUT 会触发一次 LUT 列表刷新）。
- 【触发时机】接手一镜调色、检查批量套用结果、或决定要挂 LUT 到第几个节点之前。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `item_index` | integer | 否 | 片段在该轨道上的序号（0 基）；留空取播放头所在片段。 |
| `track_index` | integer | 否 | 视频轨道序号（1 基）。（默认 1） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `whole_timeline` | boolean | 否 | 为 true 时返回时间线上每个视频片段的节点摘要（用于检查调色一致性）。（默认 false） |
| `max_clips` | integer | 否 | whole_timeline 时最多检查多少个片段。（默认 30） |
| `include_grade_detail` | boolean | 否 | 为 true 时额外调用官方 MCP 的 describe_grade，返回参数实际取值与 ASC CDL 等效值。仅对单个片段（whole_timeline=false）生效；需要 Resolve 21.1+ 的官方 MCP。（默认 false） |
| `grade_source` | enum | 否 | describe_grade 读取哪一层节点图：片段级 / 时间线级 / 照片页。（默认 'timeline_item'） |

### `qing_color_label_nodes` — 给节点命名/上色 💾

给片段的节点设置标签（label）与色标（color）。节点序号会随增删节点重排，而标签不会——想让「第 3 个节点」这类指令长期可靠，先用本工具把关键节点命名成 LOOK / BALANCE / GRAIN 之类，后续所有节点工具都可以改用 node_label 定位。标签默认宽度下最多显示 12 个字符，随 still 一起保存。

- 【副作用】修改节点标签与色标（可逆：再调用一次改回去即可；不影响画面）。
- 【触发时机】建立稳定节点流命名（Log 还原/风格/颗粒各一层）、批量整理别人交付的工程、或准备用 node_label 寻址前。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `label` | string | 是 | 新标签（建议 ≤12 个字符，过长会被截断显示）。 |
| `node_index` | integer | 否 | 节点序号（1 基）。给了 node_label 时忽略；两者都不给则报错。 |
| `node_label` | string | 否 | 要改名的现有节点标签（用于定位，不是新名字）。 |
| `color` | string | 否 | 节点色标（如 'Orange' / 'Blue' / '' 表示清除）。留空表示不改色标。 |
| `item_index` | integer | 否 | 片段序号（0 基）；留空取播放头所在片段。 |
| `track_index` | integer | 否 | 视频轨道序号（1 基）。（默认 1） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |

### `qing_color_set_lut` — 在节点上挂载 3D LUT ⚠️ 💾

把 .cube/.ilut/.olut 3D LUT 挂到指定片段的指定节点上，可一次批量套用到多片段（如整条时间线的 Log 还原）。节点用 node_label（推荐，删节点不会错位）或 1 基 node_index 定位。注意：Resolve 没有「只读挂载」，该节点原有 LUT 会被直接替换；返回值里回读被覆盖的原 LUT。

- 【副作用】覆盖目标节点原有的 LUT（破坏性：原 LUT 被替换；备份时间线可回滚，但 Resolve 原生撤销不保证可用）。
- 【触发时机】Log 素材需要统一还原、批量套拍风格 LUT、或临时替换 LUT 做 A/B 对比。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `lut_path` | string | 是 | LUT 文件绝对路径（.cube / .ilut / .olut）。 |
| `node_index` | integer | 否 | 目标节点序号（1 基，1..节点总数）。给了 node_label 时忽略。（默认 1） |
| `node_label` | string | 否 | 目标节点标签（推荐）。节点序号在删除节点后会重排，标签才是稳定键。 |
| `item_index` | integer | 否 | 片段序号（0 基）；留空取播放头所在片段。whole_timeline 为真时忽略。 |
| `track_index` | integer | 否 | 视频轨道序号（1 基）。（默认 1） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `whole_timeline` | boolean | 否 | 为 true 时对时间线上所有视频片段套用。（默认 false） |
| `clip_color` | string | 否 | 只对带该颜色标签的片段生效（如 'Orange'）；留空表示不看标签。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |
| `dry_run` | boolean | 否 | 为 true 时只做预检并回报目标片段。（默认 false） |

### `qing_color_set_node_enabled` — 启用/旁通节点 ⚠️ 💾

开启或旁通（bypass）指定片段的指定节点，用于 A/B 对比单步调色效果。只改这一个节点，不会碰到其它被单独旁通的节点（调色台的「全部启用」会误开那些节点）。节点用 node_label（推荐）或 1 基 node_index 定位。

- 【副作用】改变节点启用状态（破坏性：会改变最终画面）。
- 【触发时机】对比某一节点的贡献、临时关掉 LUT 检查原片、或批量统一旁通某个固定节点。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | 是 | true 启用，false 旁通（bypass）。 |
| `node_index` | integer | 否 | 节点序号（1 基）。给了 node_label 时忽略。（默认 1） |
| `node_label` | string | 否 | 目标节点标签（推荐）。节点序号在删除节点后会重排，标签才是稳定键。 |
| `item_index` | integer | 否 | 片段序号（0 基）；留空取播放头所在片段。 |
| `track_index` | integer | 否 | 视频轨道序号（1 基）。（默认 1） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |


## Fusion 合成

### `qing_fusion_add_tool` — 添加 Fusion 节点 ⚠️ 💾

给片段的 Fusion 合成添加一个节点（TextPlus / Transform / ColorCorrector / Merge / Blur 等），可同时写入初始输入值。片段没有合成时会自动新建。注意：Fusion 界面里只有「恰好选中一个节点」时才把新节点接在其后（手册 p1304/p1307），而脚本 AddTool 新建的节点默认是**未连线**的；未连线的节点不参与输出、画面不会变。connect_to_output=true 会尝试把它接到 MediaOut，但脚本接口对连线的支持随版本而异——返回值里 connected_to_output=false 就说明没接上，需要到 Fusion 页面手动连线（或在已选中单个节点时再试）。

- 【副作用】在片段上新增 Fusion 节点并可能改写合成连线（破坏性，可用备份时间线回滚）。
- 【触发时机】要加字幕条、角标、水印、简单合成或局部模糊时。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `tool_type` | string | 是 | Fusion 节点类型，见工具描述里的白名单。 |
| `inputs` | FusionInputSpec[] | 否 | 要同时写入的输入值，如 StyledText / Size / Center。 |
| `item_index` | integer | 否 | 片段序号（0 基）；留空取播放头所在片段。 |
| `comp_index` | integer | 否 | 合成序号（1 基）；留空用第一个。 |
| `connect_to_output` | boolean | 否 | 是否自动把新节点接到 MediaOut（简单单节点场景建议 true）。（默认 true） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |

<details><summary>参数结构体</summary>

**`FusionInputSpec`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 输入名（如 StyledText / Size / Center）。 |
| `value` | string | 是 | 输入值。文本用字符串；数值用数字；Center 这类点坐标用 {"1": x, "2": y}。 |
| `time` | integer | 否 | 给关键帧时的时间（帧）；留空表示静态值。 |

</details>

### `qing_fusion_inspect` — 查看片段 Fusion 合成 🔍

列出片段上的 Fusion 合成与其中所有节点（名称、类型、常用输入当前值）。

- 【副作用】只读。
- 【触发时机】要在已有合成里改文字/位置之前，先确认节点名。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `item_index` | integer | 否 | 片段序号（0 基，视频轨道 1）；留空取播放头所在片段。 |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |

### `qing_fusion_set_input` — 修改 Fusion 节点属性 ⚠️ 💾

修改已有 Fusion 节点的输入值（文本、坐标、数值、颜色），可批量。例如把 TextPlus 的 StyledText 改成新文案、把 Transform 的 Size 改成 1.2。

- 【副作用】覆盖节点输入值（破坏性：原值被替换，可用备份时间线回滚）。
- 【触发时机】改字幕文案、微调位置缩放、批量统一角标透明度时。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `tool_name` | string | 是 | 节点名，如 'TextPlus1'（可用 qing_fusion_inspect 查）。 |
| `inputs` | FusionInputSpec[] | 是 | 要写入的输入列表。 |
| `item_index` | integer | 否 | 片段序号（0 基）；留空取播放头所在片段。 |
| `comp_index` | integer | 否 | 合成序号（1 基）；留空用第一个。 |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |

<details><summary>参数结构体</summary>

**`FusionInputSpec`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 输入名（如 StyledText / Size / Center）。 |
| `value` | string | 是 | 输入值。文本用字符串；数值用数字；Center 这类点坐标用 {"1": x, "2": y}。 |
| `time` | integer | 否 | 给关键帧时的时间（帧）；留空表示静态值。 |

</details>


## Deliver 交付

### `qing_deliver_delete_jobs` — 删除队列任务 ⚠️

从交付队列里删除渲染任务（不删除已经渲染出的文件）。

- 【副作用】移除队列条目（破坏性：无法从脚本恢复队列配置）。
- 【触发时机】渲染完成后清理队列，或入队参数有误需要重来时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `job_ids` | string[] | 否 | 要删除的任务 id；留空清空整个队列。 |

### `qing_deliver_formats` — 查询可用渲染格式与分辨率 🔍

列出当前工程可用的渲染格式（格式名 → 文件扩展名）、音频格式、以及指定格式下的编码与分辨率。GetRenderCodecs 收的是**文件扩展名**（如 mp4/mov），不是格式名，本工具会自动换算。不同 Resolve 安装可选格式不同，配置渲染前先查一次能少踩很多坑。

- 【副作用】只读。
- 【触发时机】要指定编码/分辨率，或想确认当前工程到底能出哪些格式时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `format_name` | string | 否 | 要细查的格式名（如 MP4 / QuickTime）；留空只列格式清单与当前选择。 |
| `include_resolutions` | boolean | 否 | 是否列出该格式（及编码）支持的分辨率。（默认 true） |

### `qing_deliver_list_presets` — 列出交付渲染预设 🔍

列出当前工程可用的 Deliver 渲染预设（GetRenderPresetList），并报告当前已加载的预设与输出设置。

- 【副作用】只读。
- 【触发时机】准备渲染之前先看有哪些预设可用（不同机器预设不同）。

### `qing_deliver_queue_status` — 查询交付队列状态 🔍

列出渲染队列中的全部任务（id、名称、时间线、状态、完成百分比）。

- 【副作用】只读。
- 【触发时机】入队之后启动之前核对；渲染中轮询进度。

### `qing_deliver_render_settings` — 直接写入渲染设置

绕过预设，直接写 Resolve 的 RenderSettings（输出目录/文件名/格式/编码/分辨率/帧率/画质/色彩空间/alpha/字幕/渲染区间/同名文件策略）。只改设置，不入队；写完可用 qing_deliver_setup_job 入队。适合预设覆盖不到的细节（ColorSpaceTag、AlphaMode、UniqueFilenameStyle 这些），或要精确复现某台机器的交付规格。

- 【副作用】改写当前工程的渲染设置（影响到之后入队的每个任务），不动时间线内容。
- 【触发时机】预设不够用、要精确控制编码/色彩标签/字幕嵌入方式时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `target_dir` | string | 否 | 输出目录（绝对路径；不存在会自动创建）。 |
| `custom_name` | string | 否 | 输出文件名（不含扩展名）。 |
| `format_name` | string | 否 | 渲染格式名（见 qing_deliver_formats，如 MP4 / QuickTime）。 |
| `codec` | string | 否 | 编码名（见 qing_deliver_formats 返回的 codecs 值，如 H264）。 |
| `width` | integer | 否 | 输出宽度（像素）。 |
| `height` | integer | 否 | 输出高度（像素）。 |
| `frame_rate` | number | 否 | 输出帧率（如 24 / 25 / 29.97）。 |
| `video_quality` | integer | 否 | 画质：0 表示按码率自动，或 Least/Low/Medium/High/Best。 |
| `export_video` | boolean | 否 | 是否导出视频。 |
| `export_audio` | boolean | 否 | 是否导出音频。 |
| `color_space_tag` | string | 否 | 色彩空间标签（如 Rec.709 / Rec.2020 HLG A）。 |
| `gamma_tag` | string | 否 | Gamma 标签（如 Gamma 2.4 / ST2084）。 |
| `export_alpha` | boolean | 否 | 是否带 alpha 通道（仅支持的编码有效）。 |
| `alpha_mode` | enum | 否 | alpha 模式：premultiplied（预乘）或 straight（直通）。 |
| `encoding_profile` | string | 否 | 编码 profile（如 High / Main10）。 |
| `pixel_aspect_ratio` | string | 否 | 像素宽高比（如 Square）。 |
| `select_all_frames` | boolean | 否 | 是否渲染整条时间线（true 会忽略入出点）。 |
| `mark_in` | integer | 否 | 渲染入点（帧）。 |
| `mark_out` | integer | 否 | 渲染出点（帧）。 |
| `use_unique_filenames` | boolean | 否 | 同名时是否自动改文件名（防覆盖）。 |
| `unique_filename_style` | enum | 否 | 自动改名时加前缀还是后缀。 |
| `export_subtitle` | boolean | 否 | 是否导出字幕。 |
| `subtitle_format` | enum | 否 | 字幕方式：烧录 / 嵌入字幕轨 / 单独文件。 |
| `use_full_extents` | boolean | 否 | 是否渲染完整画面范围（不按空白裁切）。 |
| `dry_run` | boolean | 否 | 为 true 时只回报将要写入的设置。（默认 false） |

### `qing_deliver_setup_job` — 配置并添加渲染任务

按规格加载渲染预设、设置输出目录与文件名、可选设置渲染区间与字幕导出，然后 AddRenderJob 把任务加进交付队列。只入队，不自动开始渲染。

- 【副作用】改写当前工程的渲染设置并新增队列任务（不改动时间线内容）。
- 【触发时机】交付前批量出片：一条时间线多版本、或多条时间线各出一个母版时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `jobs` | RenderJobSpec[] | 是 | 要加入队列的渲染任务列表。 |
| `load_preset` | boolean | 否 | 是否按每条的 preset 字段加载预设。（默认 true） |
| `default_timeline` | string | 否 | 任务里没写 timeline_name 时使用的默认时间线。 |
| `dry_run` | boolean | 否 | 为 true 时只做预检并回报将要设置的参数。（默认 false） |

<details><summary>参数结构体</summary>

**`RenderJobSpec`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `target_dir` | string | 是 | 输出目录（绝对路径，不存在会自动创建）。 |
| `filename` | string | 否 | 自定义输出文件名（不含扩展名）。留空用 Resolve 默认命名。（默认 ''） |
| `preset` | string | 否 | 渲染预设名（来自 qing_deliver_list_presets）。 |
| `timeline_name` | string | 否 | 要渲染的时间线名称；留空为当前时间线。 |
| `mark_in` | integer | 否 | 只渲染该入点之后（帧）。留空渲染全片。 |
| `mark_out` | integer | 否 | 只渲染到该出点（帧）。 |
| `export_video` | boolean | 否 | 是否导出视频。（默认 true） |
| `export_audio` | boolean | 否 | 是否导出音频。（默认 true） |
| `export_subtitle` | boolean | 否 | 是否导出字幕（需 v19.1+）。true/false 对应 BurnIn/EmbeddedCaptions 之外的单独文件。 |
| `subtitle_format` | enum | 否 | 字幕嵌入方式（配合 export_subtitle 使用）。 |
| `on_existing` | enum | 否 | 输出目录里已有同名文件时怎么办（Resolve 默认会静默覆盖、不报错）：overwrite 直接覆盖；unique 让 Resolve 自动加后缀另起新名；fail 在入队前就报错停下。（默认 'overwrite'） |

</details>

### `qing_deliver_start` — 开始渲染

启动交付队列里的渲染任务（不传 job_ids 则渲染全部）。默认不等待完成，立刻返回起始状态；随后用 qing_deliver_queue_status 轮询进度。

- 【副作用】触发真实渲染：写盘、占用 GPU/CPU，可能持续很久。
- 【触发时机】队列确认无误后，用户明确要求开始出片时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `job_ids` | string[] | 否 | 要渲染的任务 id；留空渲染全部。 |
| `wait_seconds` | integer | 否 | 启动后最多等待多少秒再回报进度（0 = 立即返回）。（默认 0） |
| `interactive` | boolean | 否 | 是否使用交互模式（会弹 Resolve 渲染进度窗口）。（默认 false） |

### `qing_deliver_stop` — 停止渲染 ⚠️

停止正在进行的渲染（StopRendering）。

- 【副作用】中断渲染，已写出的文件可能不完整。
- 【触发时机】发现预设/输出目录设错需要立即止损时。


## 其它

### `qing_official_delete_lut_file` — 删除 LUT / DCTL 文件 ⚠️

从 MCP LUT 目录删除一个 LUT 或 DCTL 文件（按扩展名区分）。`path` 是文件名，如 "warm.cube" 或 "cool.dctl"。**只删 MCP LUT 目录里的文件**，不动用户的其它 LUT。

- 【副作用】删除磁盘文件（不可撤销；已挂该 LUT 的节点会失效）。
- 【触发时机】清理自己生成的 LUT/DCTL、或换掉一个不再需要的变换时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `path` | string | 是 | 文件名，如 "warm.cube" 或 "cool.dctl"。 |

### `qing_official_describe_grade` — 读调色实际内容 🔍

返回一个 grade 里**节点实际装了什么**，让「这个 look 长什么样」可以靠读而不是靠渲染对比来回答。每个节点报告：用到的工具（挂的 LUT、OFX 插件、每一组偏离默认值的参数）、节点标签与色标、启用状态；调过色时还给出 ASC CDL 等效值；并附当前色彩管理设置。

**这是脚本 API 结构上拿不到的东西**：脚本只能读到「节点里有 HSL Qualifier」，读不到「Blur 半径被改成了 12.5」。`source` 选片段级 / 时间线级 / 照片页；`timecode`+`track` 可以指向别的片段。

- 【副作用】只读；会起一个官方 MCP 子进程再关闭。
- 【触发时机】接手别人的调色、要复刻某个 look、或想确认「到底改了哪些参数」时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source` | enum | 否 | 描述哪一层节点图（默认播放头所在片段）。（默认 'timeline_item'） |
| `layer` | integer | 否 | 节点堆叠层号（1 基，默认第一层）。只对 timeline_item / photo 有效。 |
| `timecode` | string | 否 | 时间线时间码 HH:MM:SS:FF，用来指向别的片段；留空取播放头所在片段。 |
| `track` | integer | 否 | 视频轨道号（1 基，从下往上数，V1 在最下）。默认最上层轨道。 |

### `qing_official_generate_lut` — 按函数生成 .cube

用一个 Python 变换函数在立方体格点上逐点求值，生成 .cube 3D LUT，写到 MCP LUT 目录。`transform` 是**函数体**：接收 r/g/b（[0,1] 浮点，编码取决于节点所在位置），必须返回 (r, g, b)。返回里会列出当前生效的色彩管理设置，便于判断该在哪编码下计算。

典型用法：`r*0.9, g*0.95, b*1.05` 做轻微色偏；或 `r**0.9` 提亮中间调。

- 【副作用】在 MCP LUT 目录生成一个 .cube 文件（覆盖同名文件）。
- 【触发时机】需要一个精确的数学变换（色偏、Gamma、通道混合），而现成 LUT 都不合适时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `path` | string | 是 | .cube 文件名，如 "warm.cube"。 |
| `size` | integer | 是 | 每轴格点数（17 / 33 / 65 最常见）。 |
| `transform` | string | 是 | Python 函数体：接收 r, g, b，返回 (r, g, b)。 |

### `qing_official_get_image` — 渲染单帧画面 🔍

渲染一帧画面并返回图像 + 基本 grade 与色彩科学信息。**画面已完整调色与色彩管理，但不含显示 LUT**。超过 `max_dimension` 会自动缩小（默认 1024，上限 4096）。

比本插件的 `qing_color_grab_frame` 强的地方：① 可以指定任意 `timecode` / `frame_offset`，**不移动播放头**；② `source` 可选整条时间线合成 / 单个片段 / 媒体池素材 / 照片页；③ 支持 `roi_*` **局部放大**——区域按 `max_dimension` 渲染，裁切真的换来细节，适合检查高光、噪点、边缘这类小范围问题。

- 【副作用】只读；会起一个官方 MCP 子进程、渲染一帧并写一个图片文件到临时目录。
- 【触发时机】要看某一帧的确切画面、或需要局部放大看细节时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source` | enum | 否 | 渲染什么（默认整条时间线合成）。（默认 'timeline'） |
| `timecode` | string | 否 | 时间线时间码 HH:MM:SS:FF；留空取播放头位置。只对 source=timeline 有效。 |
| `frame_offset` | integer | 否 | 相对源起点的 0 基帧偏移（source=timeline 时是 timecode 的替代）。 |
| `clip_name` | string | 否 | 只对 source=media_pool_clip 有效：素材名。 |
| `max_dimension` | integer | 否 | 长边像素上限（默认 1024，上限 4096）。（默认 1024） |
| `roi_x` | number | 否 | 区域左边界（占画面宽度的比例，默认 0）。 |
| `roi_y` | number | 否 | 区域上边界（占画面高度的比例，默认 0）。 |
| `roi_width` | number | 否 | 区域宽度（占画面宽度的比例，默认 1 = 全宽）。 |
| `roi_height` | number | 否 | 区域高度（占画面高度的比例，默认 1 = 全高）。 |
| `out_dir` | string | 否 | 图片保存目录；留空用插件临时目录。 |
| `tag` | string | 否 | 文件名标签，便于区分多张图（如 "before_lut"）。（默认 'official_frame'） |

### `qing_official_get_thumbnails` — 渲染缩略图网格 🔍

把一个源的 N 帧排成**一张网格图**返回。每格有标注（时间码或片段内帧偏移），想看某一格的全尺寸再调 `qing_official_get_image`。问「这个片段/这条时间线整体什么样」用这个；问「这一帧长什么样」用 `qing_official_get_image`。

`count` 默认 12（1–48）。网格整体受 `max_dimension` 限制，格数越多每格越小。`start`/`end` 可用帧偏移或时间码（source=timeline 时）限定区间，中间帧均匀分布。

- 【副作用】只读；会起一个官方 MCP 子进程、渲染 N 帧并写一张网格图到临时目录。
- 【触发时机】要快速扫一遍整条时间线/一个片段的调性，而不是盯某一帧时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source` | enum | 否 | 采样什么（默认整条时间线合成）。（默认 'timeline'） |
| `count` | integer | 否 | 显示多少帧（1–48，默认 12）。（默认 12） |
| `start` | integer | 否 | 区间起点：0 基帧偏移，或时间码 "HH:MM:SS:FF"（source=timeline 时）。留空取源起点。 |
| `end` | integer | 否 | 区间终点，形式同 start。留空取源终点。 |
| `clip_name` | string | 否 | 只对 source=media_pool_clip 有效：素材名。 |
| `max_dimension` | integer | 否 | 整张网格的长边像素上限（不是单格；默认 1568，上限 4096）。（默认 1568） |
| `out_dir` | string | 否 | 图片保存目录；留空用插件临时目录。 |
| `tag` | string | 否 | 文件名标签。（默认 'official_grid'） |

### `qing_official_install_extras` — 下载安装 Extras 包

排队下载并安装「Extras 下载管理器」包。`names` 必须与 `qing_official_list_extras` 报告的名字**完全一致**（如 ["AI Slate ID"]）。安装是异步的，用 `qing_official_list_extras` 轮询状态。

**下载量可能有几个 GiB**：调用前必须让用户明确同意。

- 【副作用】从网上下载并安装可选包（可能数 GiB、耗时很长），会改变 Resolve 的安装内容。
- 【触发时机】用户明确要求安装某个可选包，且已知道体积与耗时之后。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `names` | string[] | 是 | Extras 包名列表，必须与 list_extras 报告的一致。 |

### `qing_official_launch_resolve` — 启动 Resolve

启动 DaVinci Resolve 并等它就绪（官方实现超时 60 秒）。本插件的其他工具都要求 Resolve 已在运行；当 `qing_resolve_health` 报 `resolve_not_connected` 且用户希望由脚本启动时用这个。已经启动时不会有副作用。

- 【副作用】启动一个 GUI 应用（Resolve 会出现在屏幕上，加载可能要几十秒）。
- 【触发时机】Resolve 没在运行，而用户要求开始干活、不想自己点图标时。

### `qing_official_list_extras` — 列出 Extras 包 🔍

列出当前 Resolve 版本可用的「Extras 下载管理器」包及其安装状态。这些是可选包（多数是 AI 模型），某些脚本 API 调用需要它们才能工作。**装之前先问用户**：下载量可能有好几个 GiB。

- 【副作用】只读；会起一个官方 MCP 子进程再关闭。
- 【触发时机】某个脚本 API 报缺少模型、或想看看有哪些可选能力时。

### `qing_official_list_luts` — 列出 LUT / DCTL 🔍

递归列出全局 LUTs 目录里的 LUT 与 DCTL 文件。`kind` 过滤：`lut`（.3dl/.cube/.dat/.lut/.olut）、`dctl`（.dctl）、`all`（两者）。想给片段挂某个 LUT 但不确定确切文件名/相对路径时先列一下。

- 【副作用】只读；会起一个官方 MCP 子进程再关闭。
- 【触发时机】挂 LUT 前确认文件名、或排查「明明装了 LUT 却找不到」时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kind` | enum | 否 | 列出哪类文件（默认 all）。（默认 'all'） |

### `qing_official_read_dctl` — 读 DCTL 源码 🔍

返回全局 LUTs 目录里某个 .dctl 文件的源码。`path` 是**相对 LUTs 目录**的路径，如 "MCP/cool.dctl"（先用 `qing_official_list_luts(kind="dctl")` 拿到确切名字）。

- 【副作用】只读；会起一个官方 MCP 子进程再关闭。
- 【触发时机】要改一个已有 DCTL、或想参考它的写法时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `path` | string | 是 | 相对全局 LUTs 目录的路径，如 "MCP/cool.dctl"。 |

### `qing_official_run_script` — 运行沙箱脚本 ⚠️

在**沙箱化** Python 3.14 里执行一段脚本，`resolve` 与 `project` 已预注入，用 `result = {...}` 返回结构化数据，`print()` 的内容走 `output` 字段。沙箱屏蔽文件系统、网络与子进程（`os`/`sys`/`pathlib`/`shutil` 不可用），所以它**不能**用来读写本地文件——那类需求走本插件自己的工具。

用途：脚本 API 里有、而本插件还没封装成工具的操作；或一次性批量计算。写脚本前先用 `qing_official_search_api` 确认签名。

**注意**：官方标注这是破坏性工具（脚本能改工程），而它绕过本插件的检查点护栏——需要备份时先自己调 `qing_color_copy_grade` 之外的时间线复制，或让用户确认。

- 【副作用】执行任意 Resolve 操作（沙箱内，无文件/网络/子进程权限）；可能修改工程且**不建检查点**。
- 【触发时机】脚本 API 有明确能力、但本插件尚未提供对应工具时。优先用现成工具。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `script` | string | 是 | 要执行的 Python 脚本。用 result 变量返回结构化数据。 |
| `timeout` | integer | 否 | 超时秒数（默认 10，上限 60）。 |

### `qing_official_run_script_unsafe` — 运行脚本（完全系统权限） ⚠️

在**不受限**的 Python 3.14 里执行脚本，除 Resolve API 外还能访问文件系统、网络与子进程。只有当 `qing_official_run_script` 确实做不到时才用（例如需要写本地文件、调用外部程序、或访问网络）。

**这是本插件风险最高的工具**：它能改工程、也能动磁盘上的任意文件，而且绕过本插件的检查点与 Pre-flight 护栏。调用前必须让用户明确知情并同意。

- 【副作用】执行任意 Python：可读写任意文件、访问网络、起子进程、改工程，且**不建检查点**。
- 【触发时机】沙箱版确实做不到（需要文件系统/网络/子进程），且用户已明确同意承担风险时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `script` | string | 是 | 要执行的 Python 脚本。用 result 变量返回结构化数据。 |
| `timeout` | integer | 否 | 超时秒数（默认 10，上限 60）。 |

### `qing_official_scripting_api` — 取脚本 API 存根 🔍

返回最新脚本 API 的 Python 存根（.pyi）。`api` 选三套之一：`DaVinciResolveScript.pyi`（主 API，163 KiB）、`fusion_api.pyi`、`ui_api.pyi`。**省钱三招**：`outline=true` 只要顶层定义清单（类名+一句话说明）；`types=["Timeline","TimelineItem"]` 只要这几个定义；`as_file=true` 写到磁盘只回路径，配合本地 grep 读，不吃上下文。写 `qing_official_run_script` 前想确认签名时用这个或 `qing_official_search_api`。

- 【副作用】只读；会起一个官方 MCP 子进程再关闭。as_file=true 时在磁盘写一个 .pyi 文件。
- 【触发时机】要确认某个 API 的精确签名/参数默认值，或本地存根与运行版本可能不一致时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `api` | enum | 否 | 取哪一套存根（默认主 API）。（默认 'DaVinciResolveScript.pyi'） |
| `types` | string[] | 否 | 只返回这些顶层定义（类 / TypedDict / 别名 / 函数），如 ["Timeline"]。留空返回全部。 |
| `outline` | boolean | 否 | 只要顶层定义清单与说明，不要存根正文（便宜得多）。（默认 false） |
| `as_file` | boolean | 否 | 写到磁盘只返回路径（配合本地 grep，最省上下文）。（默认 false） |

### `qing_official_scripting_docs` — 取开发者文档 🔍

返回 Resolve 开发者文档全文或某一节。`document` 选四份之一：`README.md`（37 KiB，脚本 API 指南：数据结构、设置字典、枚举取值）、`DCTLReadme.txt`（DCTL 语法与内置函数）、`FusionTools.md`、`FusionToolReference.md`。`section` 留默认 `"TOC"` 只回目录；给空字符串回全文；给其它值按 Markdown 标题匹配。存根装不下的细节（设置字典的键、枚举取值）都在这里。

- 【副作用】只读；会起一个官方 MCP 子进程再关闭。as_file=true 时在磁盘写文件。
- 【触发时机】需要设置字典的键名、枚举取值、DCTL 语法这类存根里没有的细节时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `document` | enum | 否 | 取哪份文档（默认 README.md）。（默认 'README.md'） |
| `section` | string | 否 | 要返回的章节；"TOC" 只回目录，空字符串回全文，其它值按 Markdown 标题匹配。（默认 'TOC'） |
| `as_file` | boolean | 否 | 写到磁盘只返回路径（最省上下文）。（默认 false） |

### `qing_official_search_api` — 搜脚本 API 🔍

在脚本 API 存根里按正则搜索，返回匹配的类型、函数、常量与字典字段及其说明。比 `qing_official_scripting_api` 取整份存根便宜得多——**想查一个名字就用这个**。`pattern` 是大小写不敏感的 Python 正则，例如 `marker`、`(Get|Set)Setting`、`GetNodeGraph`。

- 【副作用】只读；会起一个官方 MCP 子进程再关闭。
- 【触发时机】想知道某个 API 存不存在、叫什么、参数是什么时——先搜，别整份拉。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `pattern` | string | 是 | 大小写不敏感的 Python 正则，如 "marker" 或 "(Get\|Set)Setting"。 |
| `api` | enum | 否 | 只在这一套存根里搜；留空搜全部三套。 |

### `qing_official_status` — 官方 MCP 状态 🔍

检查 Resolve 是否在运行且可被官方 MCP 访问，返回版本号。比 `qing_resolve_health` 轻：只问官方 MCP 一句，不加载脚本 API、不查工程。诊断「插件连不上但 Resolve 明明开着」时，两个都跑一遍能快速分辨是**脚本 API 通道**坏了还是**官方 MCP 通道**坏了。

- 【副作用】只读；会起一个官方 MCP 子进程再关闭（约 0.1 秒）。
- 【触发时机】需要快速确认 Resolve 存活、或对比两条通道各自的连通性时。

### `qing_official_tools` — 列出官方 MCP 能力 🔍

列出本机官方 Resolve MCP 提供的**全部工具**及其参数 schema（当前版本 18 个），并报告官方 MCP 是否可用。用来在调用前确认参数名、取值范围与默认值，或在官方 MCP 升级后看看多了什么。注意：本插件的 `qing_official_*` 工具是对这些官方工具的封装；若某个官方工具还没有对应封装，可以用 `qing_official_run_script` 或直接按这里返回的 schema 调官方能力。

- 【副作用】只读；会启动一个官方 MCP 子进程再关闭（约 0.1 秒）。
- 【触发时机】想确认官方 MCP 可用性、查某个官方工具的确切参数、或排查封装与官方不一致时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `include_schema` | boolean | 否 | 为 true 时返回每个工具的完整 inputSchema；false 只返回名称与描述。（默认 true） |

### `qing_official_whats_new` — Resolve 更新日志 🔍

返回 DaVinci Resolve 的变更日志条目（新功能与行为变化），用于了解模型训练截止之后引入的能力。`since` 可以给版本号（如 "19.0"）或 ISO 日期（如 "2025-01-25"），只返回这之后的条目。

- 【副作用】只读；会起一个官方 MCP 子进程再关闭。
- 【触发时机】想确认某个 API 是不是新加的、或排查「文档里有但这个版本没有」时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `since` | string | 是 | 你已知的最后一个 Resolve 版本号（如 "19.0"）或最后一个 ISO 日期（如 "2025-01-25"）。 |

### `qing_official_write_dctl` — 写 DCTL

编译并写入（或覆盖）MCP LUT 目录里的 .dctl 文件。DCTL 会作为节点 LUT 生效，用 `Graph.SetLUT(nodeIndex, "MCP/name.dctl")` 应用；已挂该 DCTL 的节点会自动更新。**语法参考**：`qing_official_scripting_docs(document="DCTLReadme.txt")`。

- 【副作用】在 MCP LUT 目录写入/覆盖一个 .dctl 文件（编译失败则报错，不写半成品）。
- 【触发时机】要用代码精确定义一个色彩变换（比 .cube 更省空间、可带逻辑）时。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `path` | string | 是 | 文件名，如 "cool.dctl"。 |
| `content` | string | 是 | DCTL 源码。 |

