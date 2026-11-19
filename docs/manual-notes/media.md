# 媒体池 / 元数据（p341–440）

## 1. 核心概念区分

1. **Metadata 面板里的 `Description` 与 `Comments` 是两个不同字段**（p391），别混用；写入时键名也不一样。
2. **`Camera #` 的元数据键名就是 `Camera`**（p412–436）；另外还有 Good Take、Reel（Card ID）、Angle、Rating、People、Audio Category 等。
3. **Smart Category 只有 8 类**（p382）：Timelines、Photo Albums、Keywords、Collections、People、Shot、Scene、Offline Clips —— **没有 camera、没有日期**。所以"按机位/按日期自动分箱"不是 Resolve 自带能力，只能脚本自己建子箱（`qing_media_auto_bin` 就是这么做的）。
4. **导入只登记链接，不复制文件**（p364）：素材还在原位置，挪走/改名就红。
5. 手册里**没有**一张"全部元数据字段"的总表 —— 字段集合随版本、素材类型、以及媒体页当前摆出来的列而变。

## 2. 参数与取值

1. `Add Folder`（导入目录）只导本层；`Add Folder and SubFolders` 才递归（p365）。
2. 元数据字段的可选值（Rating 星级、People 关键词等）由 Resolve 管理，脚本侧没有"枚举全部合法值"的接口。
3. **导出 CSV 是拿到真实键名的唯一可靠办法**：`MediaPool.ExportMetadata(fileName, clips=None)`。

## 3. 破坏性 / 不可逆

1. **删素材箱会连子箱整组删掉**（p377）——删之前先确认层级。
2. `Remove Unused Clips`（移除未使用素材）手册明确标注 **un-doable**（p373）：它把素材从媒体池移除，不是删除磁盘文件，但**撤不回来**。
3. 移动素材（MoveClips / MoveFolders）本身可撤销，但被时间线引用的素材移动不影响时间线。

## 4. 真机实测（DaVinci Resolve Studio 21.2.0.5，v1.0.2 修）

`qing_media_export_metadata` 在真机上导出并回读到的实际情况：

- 编码：**UTF-16 LE + BOM**（Windows）。按 `utf-8` 读会得到 `F\0i\0l\0e\0` 这样的乱码表头 —— 工具现在按 BOM 判编码并逐级回退。
- 默认 **22 列**：`File Name`、`Clip Directory`、`Duration TC`、`Shot Frame Rate`、`Audio Sample Rate`、`Audio Channels`、`Resolution`、`Video Codec`、`Audio Codec`、`Start TC`、`End TC`、`Start Frame`、`End Frame`、`Frames`、`Bit Depth`、`Field Dominance`、`Data Level`、`Audio Bit Depth`、`Date Modified`、`Input Color Space`、`Input Gamma`、`Sound Roll #`。
- **默认列里没有 Scene / Shot / Take / Camera / Keywords / Description / Comments** —— 想导出这些场记字段，得先在媒体页把对应列摆出来。

## 5. API 可覆盖但当前工具未覆盖

1. `MediaPool.ImportFolderFromFile`、`RelinkClips` / `UnlinkClips`、`GetClipMatteList` / `GetTimelineMatteList`、`GetSelectedClips` —— 已有 API，暂未做成工具（整理/回批场景会用到 Relink）。
2. 智能分类（Smart Bin）在 API 里没有创建接口，只能手动。

## 6. 一句话结论

**别猜元数据键名**：先 `qing_media_export_metadata` 导出 CSV 看真实表头，再用 `qing_media_set_metadata` 照着写；并且记住"默认导出里没有场记字段"。
