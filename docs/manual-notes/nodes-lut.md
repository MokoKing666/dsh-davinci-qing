# 节点编辑器 / 图像处理顺序 / 键 / LUT（p3384–3486）

## 1. 核心概念与不可混淆的区分

- 所有节点都是 Corrector；Serial、Parallel、Layer 只是连线方式不同，**不是不同节点类型**（p3388）。
- 每个节点 2 进 2 出：上方绿色 RGB，下方蓝色 Key；三角形是输入、方块是输出。Key 本质是灰度图通道，可跨节点任意传输（p3388、p3420）。
- 树中只要有一个节点未连接，整个 grade 失效；末端节点 RGB 必须接到 Node Tree Output（p3389、p3396）。
- Parallel Mixer 各输入等权混合；Layer Mixer 位置最低的输入优先，且可用 composite mode（p3412–3414）。Outside 本质是默认开了 Key Input Invert 的 Corrector（p3417）。
- **节点编号按创建顺序分配，与图上的位置无关；删除节点后全部重新编号**（p3395）。Clip grade 与 Timeline grade 是两套独立 grade，片段组内还有 Pre-Clip / Post-Clip（p3404–3405）。

## 2. 参数与取值

- Key Output Gain 默认 1.00，0 = 该节点完全无效果，上限 2.00，可打关键帧（p3436–3437）。
- Key Input：Input matte 默认开（Matte = 与内部 key 取交集，Mask = 相加），Invert 默认关；另有 Gain、Offset、Blur Radius、Blur H/V Ratio（p3436）。
- Key Mixer 默认 2 个 key 输入，默认全部相加（p3431、p3435）。
- LUT 格式：`.cube`（1D/3D，17/33/65 点，32-bit float）、Panasonic VLUT、Video Range LUT、CLF、DCTL；Windows 默认目录 `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\LUT`（p3478–3479）。
- ACEScc AP1 Timeline Space 要求 LUT 范围 −0.358~1.468；AP0 Linear 为 −65504~65504（p3479）。
- 关键帧：Dynamic 菱形（默认线性插值），Static/mark 圆形（单帧跳变）；模式 All（默认）/Color/Sizing/EXT MATTE（p3455–3457）。
- CDL 公式 `Output = (Input*Slope+Offset)^Power`，基准 Slope=1、Offset=0、Power=1（p3475）。

## 3. 破坏性 / 不可逆操作（须先建检查点）

1. Reset All Grades and Nodes 删除全部节点与关键帧，只留一个默认节点（p3393）。
2. 「全部启用」会把原本单独禁用的节点也一并启用 —— **恢复旁通状态不能依赖全开**（p3392）。
3. 删除节点导致后续重新编号，任何按序号的操作都可能指向错节点（p3395）。
4. 下调 Node Stack Layer 数量会移除这些层的 grade，加回来才能恢复（p3400）。
5. 重置 Timeline grade 会连带重置 Output Sizing（p3404）；分解 Compound node 丢失施加在 compound 自身上的调整（p3402）；**向已有 LUT 的节点挂新 LUT 会覆盖原 LUT**（p3482）。
6. 含关键帧的 still 默认不保存关键帧（只存播放头左侧最近关键帧的值），需先启用 Apply Grades Using → Keyframes Aligning（p3464）；ColorTrace 默认覆盖目标 grade，Preserve Num Nodes 只保护前 X 个节点（p3469）。

## 4. 脚本 API 可能支持、但目前未覆盖

1. 节点拓扑与连接关系、Layer Mixer 输入优先级 —— 决定运算顺序；`qing_color_inspect_nodes` 应返回 edges（p3413）。
2. 节点 label（默认宽度最多 12 字符，随 still 保存）—— 比序号更适合做稳定寻址键（p3390）。
3. Key Output Gain（相当于节点强度），可让节点效果淡入淡出而不动其他参数（p3436）。
4. 关键帧全套：增删、Dynamic/Static 互转、切换 All/Color/Sizing、加 EDL marks（p3455–3465）。
5. LUT 槽位：1D Input、1D Output、3D、DCTL、CLF 各自独立，现有工具只有一个入口（p3483）；可导出 17/33/65 点 cube 与 VLUT（p3484，需查 API）。
6. ColorTrace（p3466–3473）、CDL 导入、Splitter/Combiner、Compound node、Node Stack Layers、Timeline grade 写入（p3441、p3401、p3404）。

## 5. 现有工具描述的问题

1. 「节点序号 1 基」**不是稳定 ID** —— 编号按创建顺序且删除后重排（p3395）。
2. `qing_color_copy_grade` 声称复制「完整节点树」，但默认覆盖目标 grade，且不携带关键帧（p3469、p3464）。
3. `qing_color_set_node_enabled` 若通过「全部启用」恢复，会误启用原本单独禁用的节点（p3392）。
4. `qing_color_apply_drx` 的「整条时间线」有歧义：应用仍时只作用于当前模式（Clip 或 Timeline）对应那套 grade（p3404–3405）。
5. `qing_color_set_lut` 未指明 LUT 槽位，会覆盖节点上已有 LUT（p3482）；`qing_color_inspect_nodes` 缺 Key Output Gain 与结构信息，无法判断 Layer Mixer 优先级（p3413）。

## 6. 一句话结论

**节点寻址方式**是最该立刻改的：把「1 基序号」改为 label + 拓扑锚定，并在 bypass/copy/reset 前记录每个节点原始启用状态；否则删除节点造成的重排（p3395）和「全开恢复」（p3392）会让写工具在不报错的情况下改错节点。
