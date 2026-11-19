# 限定器 / 窗口 / 遮罩 / 跟踪（p3238–3331）

## 1. 核心概念与不可混淆的区分

1. 键（Key）是灰度图：白 = 要改，黑 = 不改；来源只有限定器、Power Window、外部 matte 三种（p3238）。
2. Qualifier 按颜色或亮度范围取样生成键，不需要跟踪或关键帧（p3239）。
3. Window 是几何形状，默认不跟随主体，必须靠动画或跟踪（p3266）。
4. Matte 是键或遮罩结果的统称；Matte Finesse 是对已生成的键做后期修形（p3259）。
5. Mask 在窗口中不是一种键，而是多个窗口之间的加减开关：开 = 并入其他窗口，关 = 从其他窗口减去（p3269、p3276）。
6. 配合规则：限定器与窗口**取交集**（p3263、p3278）；Magic Mask 与同节点的限定器或窗口组合也是布尔交集（p3289）；更复杂时用 Key Mixer（p3277）。

## 2. 参数与取值

7. 限定器四模式：HSL（默认）、RGB、LUM、3D（p3241）；LUM 等同关闭 H 与 S 的 HSL（p3242）；3D 不能只取亮度（p3243）。
8. HSL：Hue Center、Width、Soft、Symmetry，以及 Sat / Lum 各自的 Low、High、Low Soft、High Soft（p3254）；RGB 三通道结构相同（p3255）。
9. 3D：模式 Soft、Flat（默认）、Tight、Luma，配 Despill；Colorspace 默认 YUV，可选 HSL/HSP/LAB；另有 Chroma Tolerance、Softness、Adaptive Chroma Softness（Tight 或 Luma 下强制关闭）、X、Y、Angle、Low、High、Low Soft、High Soft（p3256–3258）。
10. 羽化：Blur Radius 0–2000（默认 0）；In/Out Ratio 负 = 只向内，正 = 只向外；Black Clip 0–100 默认 0；White Clip 0–100 默认 100；Morph 含 Shrink/Grow/Opening/Closing 配 Morph Radius；Pre-Filter、Denoise、Post-Filter；Shadow/Midtone/Highlight（p3260–3262）。
11. 窗口五型：Linear、Circular、Polygonal、Curve、Gradient（p3269–3270）；变换 Size、Aspect、Pan、Tilt 默认 50.00，Rotate 默认 0，另有 Opacity、Convergence（p3271）；羽化：Circular 与 Gradient 为 Soft 1，Linear 为 Soft 1–4，Polygon 与 Curve 为 Inside/Outside Softness（p3273）；多边形最多 128 个控制点（p3272）。
12. 跟踪器三类：Cloud、Point、IntelliTrack（仅 Studio，p3312、p3316）；Pan and Tilt、Zoom、Rotate、Perspective 3D 必须在跟踪**前**勾选（p3313）；Clip 模式整体偏移整条路径，Frame 模式生成关键帧（p3313）。

## 3. 破坏性 / 不可逆操作

13. 删除窗口预设无警告、不能撤销（p3278）；加载预设会覆盖该节点已有的窗口（p3278）。
14. 重新跟踪直接覆盖该窗口旧跟踪数据（p3309）；跟踪后再改变换勾选不生效，必须重新分析（p3313）。
15. 对已关键帧化的多边形窗口删控制点，该点在关键帧处突兀跳变（p3273）。
16. Magic Mask 的 Clear All Strokes 连带删除跟踪数据；清掉轨道数据只能撤销或重新跟踪（p3304）；Consistency 需点击持续数帧才生效（p3287）。

## 4. 脚本可支持但目前未覆盖

17. `qing_color_inspect_keys`：逐节点读限定器参数、窗口列表及变换与羽化参数、跟踪状态（需查 API）。
18. `qing_color_set_qualifier`（mode 及各通道范围与 softness）、`qing_color_set_window`（shape、transform、softness、invert、mask）、`qing_color_combine_keys`（Key Mixer）、`qing_color_track_window`（跟踪器类型、变换勾选、方向、帧范围）—— 参数名均需查 API。
19. `qing_color_export_matte`（Render in Place Magic Mask，p3287）、`qing_color_magic_mask`（clicks、strokes 及跟踪，p3282–3283）—— 是否暴露需查 API。
20. 给 `qing_color_grab_frame` 加 `highlight=flat/bw` 参数，才能直接看键本身（p3251）。

## 5. 现有工具描述的问题

21. `qing_color_copy_grade` 说「复制整棵节点树」：文档只写了窗口级 Copy/Paste Window 与 Copy/Paste Track Data，节点树级复制文档未提（p3277、p3311）。
22. `qing_color_apply_drx` 涉及 .drx 格式：文档未提。
23. `qing_color_set_lut`：限定器采样的是**上游节点喂入的 YRGB**（p3240），先挂 LUT 再拉键会改变取样结果 —— 该工具描述未提示这一顺序依赖。
24. `qing_color_inspect_nodes` 的「节点内工具列表」不够：节点属于主级还是次级，完全取决于它是否被限定器、窗口或外部 matte 限制（p3239），应把键输入与窗口单独列出。

## 6. 一句话结论

最该立刻补上 **`qing_color_inspect_keys`**：现有六个工具都只负责写入，没有回读限定器、窗口或跟踪状态的能力，任何键操作都无法验证。
