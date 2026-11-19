# Fusion 合成（p1277–1451）

## 1. 节点连接与顺序

1. 输出唯一；输入可有多个但每个只接一条线，输出可任意分支（p1394、p1402）。
2. **执行顺序只取决于上下游关系，节点在画布上的位置不影响结果**（p1400）。
3. Merge 三输入：Background（橙、默认、决定输出分辨率）、Foreground（绿、上层）、Effect Mask（蓝、可选）；连线直接落到 Merge 上时按 background→foreground→mask 顺序填充（p1403）。
4. 必须成对：MediaIn 与 MediaOut。Title 模板要排除二者；Transition 模板必须含两个 MediaIn + MediaOut；Generator 含 MediaOut（p1434、1440、1443）。
5. 命名：类型 + 序号（`Blur1`）；只能字母数字、无空格、不能数字开头，非法字符自动删除（p1415–1416）；实例名 `Instance_原节点名[_01]`（p1413）。
6. 兼容性：Merge3D 不能直接接 Glow（p1384）；Mask 节点落到节点体自动接 effect mask（p1397）；删节点后主输入/输出自动接回、遮罩输入断开（p1390）。

## 2. 参数与取值

1. 通用开关：Show Controls、Pass Through（数据从上游直通下游）、Locked（禁止编辑）、Update（关闭则不重算、显示冻结帧）、Force Cache（p1423–1424）。
2. 实例节点参数全联动，可对单参数 Deinstance / Reinstance（p1414）。
3. 每节点可存 6 套 Version；Settings > Save As / Load 读写 `.setting`（p1422）。
4. 多层文件：Process Layers（Default/All/Custom）、Input Layer（Auto/Match/Explicit）（p1412）。
5. Find 可按 tool name / type name / type ID 查找，支持正则（p1421）。
6. 关键帧与表达式的**语法文档未提**（仅见 Keyframe Editor、Hold 帧、Anim Curves 与 Keyframe Stretcher 修饰器，p1420、1446–1447）。

## 3. 破坏性 / 不可逆

1. Deinstance 后不能 Reinstance，只能撤销；重启后只能重新复制粘贴实例（p1413–1414）。
2. Settings > Reset Default 会删除该节点的 `.setting` 文件（p1423）。
3. 删组连带删组内节点；删 Underlay 连带删框内节点，须 Option-click 只选框（p1428、1418）。
4. 替换节点有确认框，同名参数被继承（Transform→Merge 拷 center/angle）（p1385）。
5. Macro Editor 的 Save 直接覆盖原宏；删 `.drfx` 连带删包内全部模板（p1432、1449）。

## 4. 工具缺口（建议）

1. **按输入名接线 / 重接**（background / foreground / effectmask）—— 最缺，需查 API。
2. 删除 / 提取节点并自动重连、替换节点保留同名参数；改名须按 p1416 语法校验。
3. 节点模式开关（Pass Through / Locked / Update / Force Cache）。
4. 关键帧、表达式、Anim Curves / Keyframe Stretcher 写入，需查 API。
5. 存读 `.setting`、分组 / 宏导出为 Edit 页模板（写入 `Templates/Edit/{Titles,Transitions,Generators,Effects}`，须重启 Resolve 才显示，p1436/1441/1444/1446）。
6. 渲染指定节点输出供视觉复核（Viewer / flipbook），需查 API。

## 5. 一句话结论

最该立刻补的是给 `qing_fusion_add_tool` 配一个**按输入名接线**的能力（含自动建 Merge 并校验是否真连上）；否则加再多节点也只是一盘散点，构不成有效的节点树。
