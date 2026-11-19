#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SRT 字幕解析与生成。

Resolve 时间线本身没有"直接写入字幕条目"的脚本 API —— 官方路径是
``Timeline.ImportIntoTimeline(filePath)`` 导入 SRT（v18.5+），或
``Timeline.CreateSubtitlesFromAudio()`` 走语音识别。所以本项目采取：

1. 解析用户给的 SRT；
2. 交给 ``ImportIntoTimeline`` 让 Resolve 生成真正的字幕轨道；
3. 如果当前 Resolve 版本不支持导入 SRT，则退化为"每条字幕一个 Fusion 文本片段"
   的方案（``fallback``），把它放在时间线的视频轨道上——效果一样可交付，
   只是不再是可编辑的字幕条目。

解析器刻意做得宽容：BOM、CRLF、缺序号、时间戳用逗号或句点、重叠条目都能吃下，
但会把这些偏差作为 ``warnings`` 报出来，避免"静默吞掉格式错误"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_TIMECODE_RE = re.compile(
    r"(?P<h>\d{1,3}):(?P<m>\d{1,2}):(?P<s>\d{1,2})[,.](?P<ms>\d{1,3})"
)
_ARROW_RE = re.compile(r"\s*-{1,2}>\s*")


@dataclass
class Cue:
    """一条字幕。"""

    index: int
    start_seconds: float
    end_seconds: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)

    def frames(self, fps: float) -> tuple[int, int]:
        """换算成 (起始帧, 结束帧)，结束帧不包含在内。"""

        fps = fps or 24.0
        start = int(round(self.start_seconds * fps))
        end = int(round(self.end_seconds * fps))
        return start, max(start + 1, end)

    def as_dict(self, fps: float | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "index": self.index,
            "start_seconds": round(self.start_seconds, 3),
            "end_seconds": round(self.end_seconds, 3),
            "text": self.text,
        }
        if fps:
            start, end = self.frames(fps)
            payload["start_frame"] = start
            payload["end_frame"] = end
        return payload


@dataclass
class SrtDocument:
    cues: list[Cue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return max((cue.end_seconds for cue in self.cues), default=0.0)

    def as_dict(self, *, fps: float | None = None, limit: int = 40) -> dict[str, object]:
        payload: dict[str, object] = {
            "cue_count": len(self.cues),
            "duration_seconds": round(self.duration_seconds, 3),
            "warnings": self.warnings,
            "cues": [cue.as_dict(fps) for cue in self.cues[:limit]],
        }
        if len(self.cues) > limit:
            payload["omitted_cues"] = len(self.cues) - limit
        return payload


def _parse_timestamp(match: re.Match[str]) -> float:
    hours = int(match.group("h"))
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    millis = int(match.group("ms").ljust(3, "0"))
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def parse_srt(text: str) -> list[Cue]:
    """解析 SRT 文本为 Cue 列表。"""

    return parse_srt_document(text).cues


def parse_srt_document(text: str) -> SrtDocument:
    """解析 SRT，返回条目 + 容错警告。"""

    document = SrtDocument()
    normalized = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized.strip())
    previous_end = 0.0
    auto_index = 0

    for block in blocks:
        lines = [line for line in block.split("\n")]
        if not lines or not block.strip():
            continue

        # 缺序号：直接以时间轴开头
        if _TIMECODE_RE.search(lines[0]) and not lines[0].strip().isdigit():
            index_line = None
            time_line_idx = 0
        else:
            index_line = lines[0].strip()
            time_line_idx = 1
            if time_line_idx >= len(lines):
                document.warnings.append(f"跳过缺少时间轴的块：{index_line!r}")
                continue

        time_line = lines[time_line_idx]
        stamps = list(_TIMECODE_RE.finditer(time_line))
        if len(stamps) < 2 or not _ARROW_RE.search(time_line):
            document.warnings.append(f"跳过时间轴无法解析的块：{time_line!r}")
            continue

        start = _parse_timestamp(stamps[0])
        end = _parse_timestamp(stamps[1])
        if end <= start:
            document.warnings.append(
                f"条目 {index_line or auto_index + 1} 的结束时间不晚于开始时间，已按 +0.5s 修正。"
            )
            end = start + 0.5
        if start < previous_end - 1e-6:
            document.warnings.append(
                f"条目 {index_line or auto_index + 1} 与上一条重叠（{start:.2f}s < {previous_end:.2f}s）。"
            )
        previous_end = end

        payload = "\n".join(lines[time_line_idx + 1:]).strip("\n")
        if not payload:
            document.warnings.append(f"条目 {index_line or auto_index + 1} 没有任何文字内容。")

        auto_index += 1
        try:
            index = int(index_line) if index_line else auto_index
        except ValueError:
            index = auto_index
            document.warnings.append(f"序号 {index_line!r} 不是整数，已改为 {auto_index}。")

        document.cues.append(
            Cue(index=index, start_seconds=start, end_seconds=end, text=payload)
        )

    if not document.cues:
        document.warnings.append("没有解析出任何字幕条目。")

    return document


def load_srt(path: str | Path) -> SrtDocument:
    """从磁盘读取并解析 SRT（自动尝试 UTF-8 / UTF-8-BOM / GBK）。"""

    file_path = Path(path)
    raw = file_path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return parse_srt_document(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    document = parse_srt_document(raw.decode("utf-8", errors="replace"))
    document.warnings.append("文件不是合法 UTF-8/GB18030 编码，已用替换字符解码。")
    return document


def _format_timestamp(seconds: float) -> str:
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(total_ms, 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def dump_srt(cues: Iterable[Cue]) -> str:
    """把 Cue 列表写回 SRT 文本。"""

    blocks = []
    for order, cue in enumerate(cues, start=1):
        blocks.append(
            f"{order}\n{_format_timestamp(cue.start_seconds)} --> "
            f"{_format_timestamp(cue.end_seconds)}\n{cue.text.strip()}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_srt(path: str | Path, cues: Iterable[Cue]) -> Path:
    """把 Cue 列表写到磁盘（UTF-8，便于 Resolve 直接导入）。"""

    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(dump_srt(cues), encoding="utf-8")
    return file_path


def cues_from_pairs(pairs: Iterable[dict[str, object]], *, fps: float | None = None) -> list[Cue]:
    """从 ``[{"start": 秒|帧, "end": ..., "text": ...}]`` 构造 Cue 列表。

    ``fps`` 提供时，条目里显式写 ``unit: "frames"`` 的 ``start``/``end`` 会按帧号
    换算成秒；默认 ``unit`` 为 ``seconds``，不会把整数秒误判成帧。
    """

    cues: list[Cue] = []
    for order, pair in enumerate(pairs, start=1):
        if not isinstance(pair, dict):
            continue
        text = str(pair.get("text", "")).strip()
        unit = str(pair.get("unit", "seconds")).lower()
        start = float(pair.get("start", 0.0) or 0.0)
        end = float(pair.get("end", 0.0) or 0.0)
        if unit == "frames" and fps:
            start = start / fps
            end = end / fps
        if end <= start:
            end = start + 1.0
        cues.append(Cue(index=order, start_seconds=start, end_seconds=end, text=text))
    return cues


__all__ = [
    "Cue",
    "SrtDocument",
    "cues_from_pairs",
    "dump_srt",
    "load_srt",
    "parse_srt",
    "parse_srt_document",
    "write_srt",
]
