# 标记 / 字幕（p1010–1040、p1227–1262）

## 1. 标记的类型与区别

1. **时间线标记**打在标尺 ruler 上，对整条时间线有效，用于标记重要时刻、记备注与吸附对齐（p1014）。
2. **片段标记**挂在时间线片段上，显示在片段名条顶部；多片段重叠选中时会加到所有片段（p1013–1014）。
3. **素材标记**挂在 Media Pool 源片段上，仅在 List 视图以层级条目出现；双击开进 Source Viewer、拖进时间线成片段、拖进 Bin 成子剪辑（p1013、p1024–1025）。
4. 三种标记都出现在 Color 页 Mini-Timeline；Shift+↑/↓ 导航时片段标记与时间线标记混排（p1014、p1021）。
5. **不要混淆**：Flag 标记整个片段并连带所有共用同一源片段的片段（p1012）；Clip Color 只影响该片段实例。文档里「16 色」说的是 clip color 与 track color（p1026–1027），**不是**标记颜色枚举。

## 2. 标记参数与字幕轨道约束

1. 标记字段（p1016–1017）：Time（**相对该片段或时间线**的帧，可数值修改）、Duration（设 00:00:00:00 即取消）、Name（默认 Marker 1、2…）、Notes、Color、Keyword（可搜字典）。
2. 有 Notes 的标记显示小圆点；手绘注释（箭头、线、框）也存放在标记里（p1017、p1019–1020）；Option+拖拽可创建或修改持续时间标记（p1018）。
3. 字幕轨按语言各建一条，文档未提数量上限（p1232）；但同一时刻只显示一条，启用一条会禁用其他（p1228）；每轨最多 3 个 region，即最多 3 条字幕同屏（p1243）。
4. 样式三层可改：轨道级（字体/字号/颜色/行距/对齐/位置/透明度/阴影/描边/背景，p1237）、单条 Customize Caption（p1238）、单单词级（p1238）；可存 `.preset` 并导出导入（p1239–1240）。
5. **轨名参与导出编码，交付前必须命名**（p1231）；字幕 clip 可像普通 clip 一样 slip/slide/裁剪/卷动/波纹，并有淡入淡出（p1237、p1240）。
6. 导入格式 srt/srtx/vtt/xml/ttml（p1230）；File > Export Subtitle 仅 srt/vtt（p1247）；交付 Export As 有 IMSC1/DFXP/SRT/WebVTT（p1248）。

## 3. 破坏性 / 不可逆

1. Clear All / Remove All Markers（片段或整条时间线，p1017）、Mark > Delete All Markers（按颜色或全部，p1025）会把标记里的 Notes 与手绘注释一起删掉（p1019–1020）。
2. 字幕样式预设的 Update/Delete 明确无 Undo；把预设 Load 到轨道可以 Undo（p1239）。
3. 字幕文件拖进轨道即被分解成一条条字幕 clip，此后编辑不再回写文件（p1230）。
4. Create Subtitles from Audio 在有多条字幕轨时总写入**最高**那条（p1235，Studio 专属）。
5. 删除标记能否撤销：文档未提。

## 4. 现有工具未覆盖的可能能力

1. 标记 Keyword 字段（p1017）——需查 API。
2. 按颜色：隐藏 / 删除 / 索引筛选（p1025、p1023）→ `read_markers` 加 color 过滤，新增 `delete_markers(color|all|帧区间)`。
3. 导出：时间线标记导出 EDL（p1024）、Edit Index 导出 .txt/.csv（p1024）、Marker Index 导出 PDF（p1019）—— 可用 read_markers 的返回在本地生成，不必等 API。
4. 章节：QT/MP4 交付的 Chapters from Markers，章节名取标记 Name 且必须同色（p1022）→ `add_markers` 加 chapter 模式。
5. 跨片段/时间线复制标记（p1018）、Ripple Timeline Markers 开关（p1017）、Notes 进 Data Burn-In 的 %marker（p1018）—— 需查 API。
6. 字幕侧：按时间码插入（p1230）、轨道改名（p1231）、轨道样式（p1237）、导出 srt/vtt（p1247）、烧录/嵌入 CEA-608（仅 MXF OP1A 与 QuickTime，p1248）、词级时间同步（p1241）—— 均需查 API。

## 5. 现有工具描述的错误 / 不准确

1. **add_markers**：写「16 种颜色」，但标记颜色枚举文档未给（p1015）；漏 Keyword；未说明帧号是**相对该片段或时间线**，而不是绝对时间码（p1016）。
2. **read_markers**：「读全部标记」未区分时间线标记与片段标记（p1013–1014），也读不到标记内的绘制注释（p1019）。
3. **import_srt**：只提 SRT，实际支持 5 种格式（p1230）；未说明导入会把字幕分解成单条 clip，且有「按时间码对齐」与「以首条字幕为零点」两种定位（p1230）；未提示轨名影响导出、同屏只能一条字幕轨（p1231、p1228）。
4. **markers_to_subclips**：缺子剪辑边界规则 —— 起点 = 标记帧，终点 = 下一个标记的前一帧，或 duration > 1 帧时取该 duration（p1024–1025）；且 flag 不算标记（p1012）。
5. **inspect**：清单里的「标记列表」同样没区分标记归属（p1021）。

## 6. 一句话结论

给 `read_markers` 加按颜色过滤、新增按色批量删除，并给 `add_markers` 加「章节」模式（同色 + Name 即章节名）—— 这是文档里唯一直通交付物的标记用途（p1022、p1025）。
