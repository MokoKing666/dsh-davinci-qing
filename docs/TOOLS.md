# dsh-davinci-qing 工具目录（v1.0.0）

> 让青青住进你的dsh

共 **36** 个 MCP 工具。本文档由 `export_schema.py` 自动生成，请勿手工编辑；参数表与 JSON Schema 同源。

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

环境自检：定位脚本 API 与 fusionscript 动态库、连接 Resolve、确认 Studio 版、检查是否有打开的工程与活动时间线，并给出中文排查提示。

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

把 .drx 静帧预设（PowerGrade）应用到片段、指定颜色标签的片段组或整条时间线。适合 Log 还原、胶片模拟等完整节点流的批量落地。

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
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |
| `dry_run` | boolean | 否 | 为 true 时只回报目标片段。（默认 false） |

### `qing_color_copy_grade` — 批量复制调色 ⚠️ 💾

把源片段（或播放头所在片段）的完整调色节点树复制到一批目标片段（``CopyGrades``）。常用于整场戏统一基础调色后逐条做局部调整。

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

把当前播放头画面导出为 PNG/JPG 并把图片直接返回（多模态可直接查看），同时给出文件路径。用于检查曝光、高光溢出、肤色与构图，形成 挂载预设 → 截图审片 → 参数微调 的闭环。

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

### `qing_color_inspect_nodes` — 解析片段节点树 🔍

读取片段（或整条时间线）的调色节点树：节点数量、标签、启用状态、挂载的 LUT、节点内工具列表。可批量覆盖多条片段，便于判断调色是否统一。

- 【副作用】只读（唯一例外：某些版本读取节点 LUT 会触发一次 LUT 列表刷新）。
- 【触发时机】接手一镜调色、检查批量套用结果、或决定要挂 LUT 到第几个节点之前。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `item_index` | integer | 否 | 片段在该轨道上的序号（0 基）；留空取播放头所在片段。 |
| `track_index` | integer | 否 | 视频轨道序号（1 基）。（默认 1） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `whole_timeline` | boolean | 否 | 为 true 时返回时间线上每个视频片段的节点摘要（用于检查调色一致性）。（默认 false） |
| `max_clips` | integer | 否 | whole_timeline 时最多检查多少个片段。（默认 30） |

### `qing_color_set_lut` — 在节点上挂载 3D LUT ⚠️ 💾

把 .cube/.ilut/.olut 3D LUT 挂到指定片段的指定节点上（节点序号 1 基）。可一次给多个片段批量套用，如整条时间线的 Log 还原。

- 【副作用】覆盖目标节点原有的 LUT（破坏性：原 LUT 会被替换，可用备份时间线回滚）。
- 【触发时机】Log 素材需要统一还原、批量套拍风格 LUT、或临时替换 LUT 做对比。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `lut_path` | string | 是 | LUT 文件绝对路径（.cube / .ilut / .olut）。 |
| `node_index` | integer | 否 | 目标节点序号（1 基，1..节点总数）。（默认 1） |
| `item_index` | integer | 否 | 片段序号（0 基）；留空取播放头所在片段。whole_timeline 为真时忽略。 |
| `track_index` | integer | 否 | 视频轨道序号（1 基）。（默认 1） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `whole_timeline` | boolean | 否 | 为 true 时对时间线上所有视频片段套用。（默认 false） |
| `clip_color` | string | 否 | 只对带该颜色标签的片段生效（如 'Orange'）；留空表示不看标签。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |
| `dry_run` | boolean | 否 | 为 true 时只做预检并回报目标片段。（默认 false） |

### `qing_color_set_node_enabled` — 启用/旁通节点 ⚠️ 💾

开启或旁通（bypass）指定片段的指定节点，用于 A/B 对比单步调色效果。

- 【副作用】改变节点启用状态（破坏性：会改变最终画面）。
- 【触发时机】对比某一节点的贡献、临时关掉 LUT 检查原片、或批量统一旁通某个固定节点。
- 【自动备份】执行前会 SaveProject() 并 DuplicateTimeline 生成 `[Backup_HHMMSS]_原时间线名`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `node_index` | integer | 是 | 节点序号（1 基）。 |
| `enabled` | boolean | 是 | true 启用，false 旁通。 |
| `item_index` | integer | 否 | 片段序号（0 基）；留空取播放头所在片段。 |
| `track_index` | integer | 否 | 视频轨道序号（1 基）。（默认 1） |
| `timeline_name` | string | 否 | 时间线名称；留空为当前时间线。 |
| `allow_without_backup` | boolean | 否 | 备份失败时是否仍继续。（默认 false） |


## Fusion 合成

### `qing_fusion_add_tool` — 添加 Fusion 节点 ⚠️ 💾

给片段的 Fusion 合成添加一个节点（TextPlus / Transform / ColorCorrector / Merge / Blur 等），可同时写入初始输入值。片段没有合成时会自动新建。

- 【副作用】修改片段画面（新增节点并连线到 MediaOut）。属于破坏性写入。
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

### `qing_deliver_list_presets` — 列出交付渲染预设 🔍

列出当前工程可用的 Deliver 渲染预设（GetRenderPresetList），并报告当前已加载的预设与输出设置。

- 【副作用】只读。
- 【触发时机】准备渲染之前先看有哪些预设可用（不同机器预设不同）。

### `qing_deliver_queue_status` — 查询交付队列状态 🔍

列出渲染队列中的全部任务（id、名称、时间线、状态、完成百分比）。

- 【副作用】只读。
- 【触发时机】入队之后启动之前核对；渲染中轮询进度。

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

