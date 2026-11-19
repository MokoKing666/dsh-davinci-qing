# 交付 / 渲染设置 / 渲染队列（p4088–4170）

## 1. 渲染设置全量清单

**格式/编码/容器**（p4115–4116）：`Format` = AVI、Cineon、DCP、DPX、easyDCP、EXR、GIF、HEIF、IMF、JPEG、JPEG 2000、MJ2、MKV、MP4(H.264)、MXF OP-Atom、MXF OP1A、PNG、QuickTime、TIFF、WebP。`Codec`/`Type` 随 Format 变化：QuickTime 可选 ProRes、DNxHD/HR、H.264、HEVC、H.265、VP9 8/10/12-bit、Cineform、Kakadu J2K、Uncompressed；MXF OP1A 可选 DNxHD/HR、Sony XAVC Intra CBG/VBR、XDCAM MPEG2。另有 `GetInterop packaging`、`Package Type`（IMF 默认 App2 Extended）、`Encoding Level`、`Lossless Compression`、`Slope-Rate Control`、`QStep`。

**分辨率**：`Resolution` 默认 `Timeline Resolution`；`Render at Source Resolution` 在 Video 与 Advanced 组各出现一次（p4091、p4114）；`Pixel aspect ratio`（Square/Cinemascope）。

**帧率**：`Frame rate` 默认 `Timeline Frame Rate`；支持 3:2 pulldown（如 23.976 工程输出 29.97）(23.976 3:2)；24fps 工程只能出 30。

**质量/码率**：`Quality`、`Maximum Bit Rate`、`Restrict to X Kb/s`、`Use Constant Bit Rate`、`Multi-pass encode`(Single/Multi)、`Key Frames`、`Frame Reordering`、`Rate Control`、`Lookahead`、`Use hardware acceleration`、`Entropy Mode`(CALVC/CABAC)、`Bypass re-encode when possible`、`Render speed`(Maximum/1–50%)。

**色彩/位深**：`Color Space Tag`（RCM 默认 Same as Project；ACES 下为 ACES ODT）、`Gamma Tag`、`Data levels`(Auto/Video/Full)、`Retain sub-black and super-white data`、`Tone Mapping`(None/Dolby Vision/HDR10+)、`Export/Embed HDR10 Metadata`、`Embed HDR Vivid Metadata`、IMF `Bit Depth`。

**音频**（p4123–4125）：`Format`(MXF OP1A/OP-Atom/QuickTime/MP4/WAVE/MP3/FLAC)、`Codec`(Linear PCM/AAC/IEEE Float/MP3)、`Sample Rate`、`Bit Rate Strategy`、`Quality`、`Data Rate`、`Bit Depth`、`Render one track per channel`、`Render as discrete audio tracks`、`Output Track #`(Main/Submix/All Timeline Tracks)、`Language`/`Content`（仅 IMF）、`Audio Normalization`。

**字幕/元数据**：`Export Subtitle` × `Format`（As a separate file / Burn into video / As embedded captions）× `Export As`(IMSC1/DFXP/SRT/WebVTT) × `Codec`(Text/CEA-608)；`Data burn-in`；`Chapters from Markers`（仅 QuickTime/MP4）；`Trigger script at`(Start/End)。

**文件名/目录**：`Filename Uses`(Custom/Timeline Name/Source Filename)、`%scene_%shot_%take` 元数据变量、`File suffix`、`Use unique filenames`、`Use filename digits`、`Start timeline timecode at`、`Place clips in separate folders`、`Preserve Source directory levels`、`File Subfolder`、`Use commercial workflow`、`Add X frame handles`、`Render full extents`。

**渲染范围**：Entire Timeline / All Clips / All Filtered Clips / In–Out / Render This Clip；`Render All Video Tracks` 以及 Disable 下拉可排除整轨（p4121）。In/Out 在 Individual Clips 下自动吸附片段边界，不能渲染半段（p4129）。

**多片段模式**：`Single clip` 与 `Individual clips`；后者还有 `Render Timeline Effects`、`Render at Source Resolution`、`Export Alpha`（限 TIFF/OpenEXR/ProRes 4444/4444 XQ/DNxHR 444，p4093）、`Alpha Mode`(Straight/Premultiplied)、`Render Stereoscopic 3D`、`Disable sizing and blanking output`（开启时 sizing 不烘焙，改写入 XML/AAF 元数据，p4092）。**过滤生效时无法选 Single Clip**（p4114）。

## 2. 渲染预设机制

先选 Custom，再改 Video/Audio/File 面板，然后 Save as New Preset 命名；点击预设整表刷新，另有 Update / Delete Current Preset（p4112）。预设以 **.xml** 存储，可通过 Option 菜单 Import Preset / Export Preset 导入导出，同时出现在右键时间线的 "Add to Render Queue Using" 菜单（p4112–4113）。预设内容即全部 Video/Audio/File 设置。**自定义预设只能基于 Single Clip 设置**，不能是 Individual Clips（p4095）。Quick Export 预设可勾 "Add to quick export"（p4096）。

## 3. 破坏性操作

- **覆盖同名文件**：文档明确 Resolve "automatically overwrites any other media files that have the same name"（p4128）；入队时仅弹 "This render may overwrite existing clips in this folder" 提示（p4131）。
- 覆盖队列中已渲染作业的重渲对话框（Yes 全部重渲 / No 只渲未渲的，p4131）。
- `Clear Rendered`、`Clear All`、`Clear Render Status`（把已渲改回未渲）三者语义不同（p4131–4132）。
- Update / Delete Preset 均提示 "This action can not be undone"（p4113）。
- 拖动队列重排序：编号不变但**渲染设置随位置替换**（p4102）。
- `Generate New AAF` 丢弃不支持的效果（p4162）；导入元数据第三档 Merge Option 会清空 csv 未涉及字段（p4169）。

## 4. API 未覆盖能力（建议）

- **Additional Outputs**（一次渲染多套输出，p4127）：`setup_job` 加 `additionalOutputs[]`。
- **防覆盖三件套**：`Use unique filenames`、`Place clips in separate folders`、`Use commercial workflow`（p4125–4128）。
- **渲染范围细分**：`All Filtered Clips`、`Render This Clip`、按轨禁用（p4121、p4129）。
- **音频全参数**（p4123–4125）：位深、采样率、`Render one track per channel`、Output Track 选择。
- **字幕三档 Format 与 Export As**（p4123）。
- **预设导入导出**：Import Preset / Export Preset `.xml`（p4113）。
- **队列状态**：`Clear Render Status`、已渲染重渲确认（p4131）。
- **时间线导出**：OTIO（.otio/.otioz）、AAF/XML、EDL（含 Missing Clips EDL、Timeline Markers to EDL）、ALE / ALE+CDL、CDL、Edit Index CSV/TXT、Media Pool 元数据 .csv 导入导出（p4159–4169）—— 可能由 `Timeline.Export` 或 `MediaPool` 系列 API 完成，**需查 API**。
- **远程渲染指派**、`Render speed` 限速（p4133、p4126）。

## 5. 现有工具描述的问题

- `delete_jobs` 未区分 Clear All / Clear Rendered / Clear Render Status —— 删除与「重置为未渲染」是两件事（p4131–4132）。
- `setup_job` 只设输出目录与文件名，无防覆盖开关，直击 p4128 的自动覆盖行为。
- `setup_job` 的「渲染区间」在 Individual Clips 模式实际会被吸附到片段边界，传半段参数可能静默失效（p4129）。
- `start` 的「不传 ids 则全部」正确，但未说明含已渲染作业时的重渲确认语义（p4131）。
- `list_presets` 未提及预设可导入导出为 `.xml`，也未提及预设**不能**存 Individual Clips 设置（p4095、p4113）。

## 6. 一句话结论

`qing_deliver_setup_job` 默认开启 `Use unique filenames` 或独立子目录，并在入队前扫描目标目录同名文件 —— 因为 Resolve 会静默覆盖同名媒体（p4128）。
