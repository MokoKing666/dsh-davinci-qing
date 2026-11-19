# 画廊 / 版本 / PowerGrade / ColorTrace（p3332–3383）

## 1. 核心概念与不可混淆的区分

1. 静帧 = 该帧的 DPX 图 + 调色元数据，随工程保存；默认落在隐藏的 `.gallery` 目录，命名默认 `Track.Shot.Version`（p3332、p3335、p3337）。静帧**同时保存 Clip 调色与 Timeline 调色**，应用哪套取决于 Node Editor 当前处于 Clip 还是 Timeline 模式（p3362）。
2. **本地版本**：各片段调色互不共享、不跨时间线（默认）。**远端版本**：同一 Media Pool 源文件的片段在所有时间线自动链接并共享调色；启用需在 General Options 关闭「Use local version for new clips in timeline」，且只影响此后加入的片段（p3351–3352）。同一片段可并存两类版本（p3352）。
3. **共享节点**：普通节点「Save as Shared node」转换而来，自动锁定并跨片段联动，可直接插入普通节点树、不需建组（p3373–3374）。**调整片段**：叠加在时间线上，其调色/效果作用于下方所有片段，可单独或整轨关闭（p3375–3376）。**群组**：手工指定片段集合 + 自动联动，分 Pre-Clip / Clip / Post-Clip / Timeline 四种模式（p3377–3379）。
4. 普通静帧只属于本工程；**PowerGrade 静帧属于同一工程库内所有工程**，与用户绑定，最后一个 PowerGrade 专辑不可删（p3342、p3335）。**记忆（Memories）** 是带 A–H 标签的静帧，内容相同（p3347）。

## 2. 参数与取值

5. 编号规则「轨道.镜头.版本」（p3337）；自动标签七种：Clip Name、Clip Version Name、Source TC（HH.MM.SS.FF）、Timeline TC、Timeline Name、Display LUT Name、自定义 tags（p3338）。
6. Apply Grades Using 三档：No Keyframes、Keyframes Aligning Source Timecode、Start Frames（p3340、p3363）。
7. Copy Grade 保护项：**Preserve number of nodes 0–10**（设 1 时丢弃被复制调色的第 1 个节点；设 5 时丢弃前 5 个，前提是目标片段已有至少 5 个节点），以及 Camera Raw、Input Sizing、Convergence、Floating Windows、Auto Align、Dolby Vision Analysis/Trim（p3361）；另有 All/Color/Sizing 作用域（p3363）。
8. `.drx` = DaVinci Resolve eXchange 文件，**只装调色信息与节点结构，图像另存**（p3383）；`.cube` 本章未定义（指向 Using LUTs 章）。静帧默认不含 Display LUT，除非启用 Apply Display LUT，且该 LUT 只在 Viewer 播放时使用，DPX 本体不变（p3340）。

## 3. 破坏性与不可逆操作

9. 复制调色或 Apply Grade **会覆盖目标片段已有调色**（p3365）。
10. Use Local Grades、Copy Remote Grades to Local **不能 Undo**（Undo 只针对单个片段操作），后者会覆盖全部已调的本地版本（p3356–3357）。
11. Copy Local Grades to Remote：同源片段中**只有时间线上最后一个片段**的调色成为远端版本（p3357）。
12. Delete All/Unused Versions 只保留当前版本（p3360）；删除专辑连带删除其中静帧（p3341）；Collapse Group Grades 让片段退出群组（p3382）；保存记忆会覆盖旧记忆（p3348）；开启 One Still Per Scene 后再保存，同片段旧静帧会消失（p3340）。

## 4. API 可能支持但现有工具未覆盖

13. **追加式应用**：Append Node Graph（p3366；拖到连线上可中间插入、Command-拖成 compound，p3367）→ `copy_grade` 加 `mode=overwrite|append|append_compound`。
14. 节点编号是联动键：Ripple Node Changes to Selected Clips/Current Group（p3372）、Append Node to Selected Clips（p3373）。
15. `copy_grade` 缺 `preserveNodes(0–10)`、All/Color/Sizing、关键帧对齐；另有 Apply Color/PTZR/Source/All（p3369–3371）与按 palette 粒度的 Paste Attributes（p3368）—— 需查 API。
16. 共享节点生命周期：Save as Shared / Lock / Convert to Corrector / Delete Shared Node（p3374–3375）；群组增删改名与 Collapse Group Grades（p3377–3382）。
17. 版本：Add / Rename / Load / Delete / Delete Unused / Default / Copy to Local（p3357–3360）。**Version 1 即默认版本**，切换本地/远端与重导入时只有它生效（p3359）。
18. Grab All/Missing Stills（首帧/中帧，p3333）；导出纯 `.drx`（p3347）；单独导入 `.drx` 可保留节点结构但无法 wipe（p3383）；Align Keyframes to Current Frame（p3367）；Memories A–H 可当预设（p3348）—— 需查 API。

## 5. 现有工具描述的错误或不准确

19. `copy_grade` 说「复制整棵节点树」不准确：实际受 Node Editor 模式与 preserve number of nodes 限制（p3362、p3361），且是**覆盖式**（p3365）。
20. `apply_drx` 称「静帧预设」不妥：`.drx` 只含调色与节点结构，图是另一份 DPX（p3383）；关键帧处理由 Apply Grades Using 控制，工具未暴露。
21. `grab_frame` 只是导出图像，**不等于 Grab Still**，不会生成带调色元数据的 Gallery 静帧（p3333、p3335）。
22. `inspect_nodes` 若只读一层节点树，对群组片段不完整：群组片段有 4 种模式（p3378），应补节点编号、Shared 标识/锁定、compound、Clip 与 Timeline 双调色。
23. `set_node_enabled` 的节点旁通语义本章未提。

## 6. 一句话结论

把 `copy_grade` 拆成「覆盖 / 追加」两种模式，并暴露 `preserveNodes(0–10)` 与覆盖不可撤销警告。
