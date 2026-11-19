#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DaVinci Resolve 脚本 API 的**离线模拟器**。

用途：在没有（或不想动）真实 Resolve 工程的机器上跑通全部工具、单元测试与
``check_env.py --simulate``。它会实现本项目用到的那个子集，行为尽量贴近真实 API：

* 轨道编号 1 基、节点编号 1 基、帧号沿用工程帧率；
* ``DuplicateTimeline`` 名称冲突时返回 None；
* ``SetProperties`` 校验键名与取值范围，非法键整体失败；
* ``GetMarkers`` 以 int 帧号为键；
* 未实现的调用返回 None/False，而不是抛异常（与真实 API 一致）。

启用方式：``QING_DAVINCI_FAKE=1``（resolve_connector 会自动导入本模块）。
"""

from __future__ import annotations

import copy
import os
import random
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 节点属性白名单（与真实 TimelineItemProperties 对齐的关键子集）
# ---------------------------------------------------------------------------

_PROPERTY_RANGES: dict[str, tuple[float, float] | None] = {
    "Pan": (-4.0 * 3840, 4.0 * 3840),
    "Tilt": (-4.0 * 2160, 4.0 * 2160),
    "ZoomX": (0.0, 100.0),
    "ZoomY": (0.0, 100.0),
    "RotationAngle": (-360.0, 360.0),
    "AnchorPointX": (-4.0 * 3840, 4.0 * 3840),
    "AnchorPointY": (-4.0 * 2160, 4.0 * 2160),
    "CropLeft": (0.0, 3840.0),
    "CropRight": (0.0, 3840.0),
    "CropTop": (0.0, 2160.0),
    "CropBottom": (0.0, 2160.0),
    "CropSoftness": (-100.0, 100.0),
    "Opacity": (0.0, 100.0),
    "Distortion": (-1.0, 1.0),
    "AudioVolume": (-100.0, 30.0),
    "AudioPan": (-100.0, 100.0),
}
_PROPERTY_BOOLS = {
    "TransformEnabled",
    "ZoomGang",
    "FlipX",
    "FlipY",
    "CroppingEnabled",
    "CropRetain",
    "DynamicZoomEnabled",
    "CompositeEnabled",
    "LensCorrectionEnabled",
    "RetimeAndScalingEnabled",
}

MARKER_COLORS = [
    "Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
    "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
]

_START_TIMECODE = "01:00:00:00"


def _reset_state() -> None:
    """重置模拟器状态（测试之间互不影响）。"""

    global _STATE
    _STATE = _SimState()


class _SimState:
    def __init__(self) -> None:
        self.project_manager = _ProjectManager()
        self.started_at = time.time()


_STATE: _SimState | None = None


def _state() -> _SimState:
    global _STATE
    if _STATE is None:
        _STATE = _SimState()
    return _STATE


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


class _FusionStub:
    """``resolve.Fusion()`` 的最小占位。"""

    def __init__(self) -> None:
        self.version = "Fusion 20 (simulated)"


class _Resolve:
    def __init__(self) -> None:
        self._page = "edit"
        self._fusion = _FusionStub()

    # 版本 -----------------------------------------------------------------
    def GetVersion(self) -> list[Any]:
        return [21, 1, 0, 17]

    def GetVersionString(self) -> str:
        return "21.1.0.17"

    def GetProductName(self) -> str:
        return os.environ.get("QING_FAKE_PRODUCT", "DaVinci Resolve Studio")

    def Fusion(self) -> _FusionStub:
        return self._fusion

    # 页面 -----------------------------------------------------------------
    def GetCurrentPage(self) -> str:
        return self._page

    def OpenPage(self, page: str) -> bool:
        if page not in {"media", "cut", "edit", "fusion", "color", "fairlight", "deliver"}:
            return False
        self._page = page
        return True

    def GetKeyframeMode(self) -> int:
        return 0

    def SetKeyframeMode(self, mode: int) -> bool:
        return True

    # 管理器 ---------------------------------------------------------------
    def GetProjectManager(self) -> Any:
        return _state().project_manager

    def GetMediaStorage(self) -> Any:
        return _MediaStorage()


class _MediaStorage:
    """文件系统访问（模拟器只读真实目录）。"""

    def GetMountedVolumeList(self) -> list[str]:
        if os.name == "nt":
            return [f"{drive}:\\" for drive in "CDEFGH" if Path(f"{drive}:\\").exists()]
        return ["/"]

    def GetSubFolderList(self, folderPath: str) -> list[str]:
        try:
            return [str(p) for p in Path(folderPath).iterdir() if p.is_dir()]
        except OSError:
            return []

    def GetFileList(self, folderPath: str) -> list[str]:
        try:
            return [str(p) for p in Path(folderPath).iterdir() if p.is_file()]
        except OSError:
            return []

    def AddItemListToMediaPool(self, itemInfos: list[dict[str, Any]]) -> list[Any]:
        project = _state().project_manager.GetCurrentProject()
        if project is None:
            return []
        pool = project.GetMediaPool()
        folder = pool.GetCurrentFolder()
        created = []
        for info in itemInfos or []:
            path = info.get("media") or info.get("FilePath")
            if not path:
                continue
            created.append(folder._add_clip(str(path), start=info.get("startFrame"), end=info.get("endFrame")))
        return created

    def RevealInStorage(self, path: str) -> bool:
        return Path(path).exists()


# ---------------------------------------------------------------------------
# 工程管理器
# ---------------------------------------------------------------------------


class _ProjectManager:
    def __init__(self) -> None:
        self._projects: dict[str, _Project] = {}
        self._current: _Project | None = None
        self._bootstrap()

    def _bootstrap(self) -> None:
        """预建一个演示工程，方便没有参数的自检也能跑到时间线。"""

        project = _Project("青青演示工程")
        timeline = project._create_timeline("V1_主时间线")
        project._current_timeline = timeline
        clip = project._media_pool.GetRootFolder()._add_clip("F:/demo/qing_001.mov")
        project._media_pool.AppendToTimeline([{"mediaPoolItem": clip, "trackIndex": 1, "recordFrame": 0}])
        self._projects[project.GetName()] = project
        self._current = project

    def CreateProject(self, projectName: str, mediaLocationPath: str | None = None) -> Any:
        if projectName in self._projects:
            return None
        project = _Project(projectName)
        self._projects[projectName] = project
        self._current = project
        return project

    def LoadProject(self, projectName: str) -> Any:
        project = self._projects.get(projectName)
        if project is not None:
            self._current = project
        return project

    def GetCurrentProject(self) -> Any:
        return self._current

    def CloseProject(self, project: Any) -> bool:
        if self._current is project:
            self._current = None
        return True

    def SaveProject(self) -> bool:
        return self._current is not None

    def GetProjectListInCurrentFolder(self) -> list[str]:
        return list(self._projects)

    def DeleteProject(self, projectName: str) -> bool:
        return self._projects.pop(projectName, None) is not None

    def CreateFolder(self, folderName: str) -> bool:
        return True

    def GetCurrentDatabase(self) -> dict[str, Any]:
        return {"DbType": "Disk", "DbName": "Local Database"}


# ---------------------------------------------------------------------------
# 工程
# ---------------------------------------------------------------------------


class _Project:
    def __init__(self, name: str) -> None:
        self._name = name
        self._settings: dict[str, str] = {
            "timelineFrameRate": "24",
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
            "timelinePlaybackFrameRate": "24",
            "colorScienceMode": "davinciYRGBColorManagedv2",
        }
        self._media_pool = _MediaPool(self)
        self._timelines: list[_Timeline] = []
        self._current_timeline: _Timeline | None = None
        self._gallery = _Gallery()
        self._render_presets: list[str] = [
            "H.264 Master",
            "H.265 2160p",
            "ProRes 422 HQ",
            "ProRes 4444",
            "DNxHR HQX",
            "YouTube 1080p",
        ]
        self._loaded_preset = "H.264 Master"
        self._render_jobs: dict[str, dict[str, Any]] = {}
        self._render_settings: dict[str, Any] = {
            "TargetDir": str(Path.home() / "Videos" / "qing_render"),
            "CustomName": "",
            "SelectAllFrames": True,
            "ExportVideo": True,
            "ExportAudio": True,
        }
        self._job_seq = 0

    # 基础 -----------------------------------------------------------------
    def GetName(self) -> str:
        return self._name

    def SetName(self, projectName: str) -> bool:
        self._name = projectName
        return True

    def GetMediaPool(self) -> Any:
        return self._media_pool

    def GetGallery(self) -> Any:
        return self._gallery

    def GetTimelineCount(self) -> int:
        return len(self._timelines)

    def GetTimelineByIndex(self, idx: int) -> Any:
        if 1 <= idx <= len(self._timelines):
            return self._timelines[idx - 1]
        return None

    def GetCurrentTimeline(self) -> Any:
        return self._current_timeline

    def SetCurrentTimeline(self, timeline: Any) -> bool:
        if timeline in self._timelines:
            self._current_timeline = timeline
            return True
        return False

    def GetUniqueId(self) -> str:
        return f"sim-project-{abs(hash(self._name)) % 100000}"

    # 设置 -----------------------------------------------------------------
    def GetSettings(self) -> dict[str, str]:
        return dict(self._settings)

    def SetSettings(self, settings: dict[str, Any]) -> bool:
        if not isinstance(settings, dict):
            return False
        for key, value in settings.items():
            if key in {"timelineFrameRate"}:
                try:
                    float(str(value).split()[0])
                except ValueError:
                    return False
            self._settings[key] = str(value)
        return True

    def GetSetting(self, settingName: str) -> str:
        return self._settings.get(settingName, "")

    def SetSetting(self, settingName: str, settingValue: Any) -> bool:
        self._settings[settingName] = str(settingValue)
        return True

    # 时间线 ---------------------------------------------------------------
    def _create_timeline(self, name: str) -> "_Timeline":
        timeline = _Timeline(name, self)
        self._timelines.append(timeline)
        return timeline

    # 渲染 -----------------------------------------------------------------
    def GetRenderPresetList(self) -> list[str]:
        return list(self._render_presets)

    def LoadRenderPreset(self, presetName: str) -> bool:
        if presetName in self._render_presets:
            self._loaded_preset = presetName
            return True
        return False

    def SaveAsNewRenderPreset(self, presetName: str) -> bool:
        if presetName in self._render_presets:
            return False
        self._render_presets.append(presetName)
        return True

    def DeleteRenderPreset(self, presetName: str) -> bool:
        if presetName in self._render_presets:
            self._render_presets.remove(presetName)
            return True
        return False

    def SetRenderSettings(self, settings: dict[str, Any]) -> bool:
        if not isinstance(settings, dict):
            return False
        self._render_settings.update(settings)
        return True

    def GetRenderSettings(self) -> dict[str, Any]:
        return dict(self._render_settings)

    def AddRenderJob(self) -> str:
        if self._current_timeline is None:
            return ""
        self._job_seq += 1
        job_id = f"sim-job-{self._job_seq:04d}"
        self._render_jobs[job_id] = {
            "JobId": job_id,
            "RenderJobName": self._render_settings.get("CustomName") or f"Render {self._job_seq}",
            "TimelineName": self._current_timeline.GetName(),
            "JobStatus": "Ready",
            "CompletionPercentage": 0,
            "TargetDir": self._render_settings.get("TargetDir"),
        }
        return job_id

    def GetRenderJobList(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(job) for job in self._render_jobs.values()]

    def GetRenderJobStatus(self, jobId: str) -> dict[str, Any]:
        job = self._render_jobs.get(jobId)
        if job is None:
            return {}
        return {
            "JobStatus": job["JobStatus"],
            "CompletionPercentage": job["CompletionPercentage"],
            "TimeTakenToRenderInMs": 0,
        }

    def StartRendering(self, jobIds: list[str] | None = None, isInteractiveMode: bool = False) -> bool:
        ids = jobIds or list(self._render_jobs)
        for job_id in ids:
            job = self._render_jobs.get(job_id)
            if job is None:
                return False
            job["JobStatus"] = "Complete"
            job["CompletionPercentage"] = 100
        return True

    def StopRendering(self) -> None:
        # 真实 API 的 StopRendering 不返回值；模拟器同样返回 None。
        # 渲染状态由 StartRendering 直接置为 Complete，因此这里无需额外处理。
        return None

    def IsRenderingInProgress(self) -> bool:
        return False

    def DeleteRenderJob(self, jobId: str) -> bool:
        return self._render_jobs.pop(jobId, None) is not None

    def DeleteAllRenderJobs(self) -> bool:
        self._render_jobs.clear()
        return True

    def GetCurrentRenderFormatAndCodec(self) -> dict[str, str]:
        return {"format": "mp4", "codec": "H264"}

    def SetCurrentRenderFormatAndCodec(self, format: str, codec: str) -> bool:
        return True

    def GetRenderFormats(self) -> dict[str, str]:
        return {"mp4": "MP4", "mov": "QuickTime", "mxf": "MXF OP1A"}

    def GetRenderCodecs(self, renderFormatFileExtension: str) -> dict[str, str]:
        return {"H264": "H.264", "H265": "H.265", "ProRes422HQ": "Apple ProRes 422 HQ"}

    # 静帧 -----------------------------------------------------------------
    def ExportCurrentFrameAsStill(self, filePath: str) -> bool:
        target = Path(filePath)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            return _write_demo_still(target)
        except OSError:
            return False

    def RefreshLUTList(self) -> bool:
        return True

    def GetColorGroupsList(self) -> list[Any]:
        return []


# ---------------------------------------------------------------------------
# 静帧 / 缩略图生成（用 Pillow，失败时退化为最小 PNG）
# ---------------------------------------------------------------------------


def _write_demo_still(target: Path) -> bool:
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001 - 没装 Pillow 时写一个占位 PNG
        _MIN_PNG = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        )
        target.write_bytes(_MIN_PNG)
        return True

    width, height = 640, 360
    image = Image.new("RGB", (width, height), (18, 20, 24))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        shade = int(20 + 60 * (y / height))
        draw.line([(0, y), (width, y)], fill=(shade, shade - 4, shade - 8))
    draw.rectangle([40, 40, width - 40, height - 40], outline=(120, 160, 200), width=2)
    draw.text((56, 60), "DaVinci Resolve (simulated still)", fill=(210, 220, 230))
    draw.text((56, 84), f"{target.stem}", fill=(160, 190, 220))
    draw.ellipse([width - 140, height - 120, width - 60, height - 40], fill=(200, 170, 120))
    image.save(target)
    return True


# ---------------------------------------------------------------------------
# 媒体池
# ---------------------------------------------------------------------------


class _MediaPool:
    def __init__(self, project: _Project) -> None:
        self._project = project
        self._root = _Folder("Master", self)
        self._current = self._root

    def GetRootFolder(self) -> Any:
        return self._root

    def GetCurrentFolder(self) -> Any:
        return self._current

    def SetCurrentFolder(self, folder: Any) -> bool:
        self._current = folder
        return True

    def AddSubFolder(self, folder: Any, name: str) -> Any:
        if folder is None:
            return None
        return folder._add_subfolder(name)

    def ImportMedia(self, clipInfos: list[dict[str, Any]]) -> list[Any]:
        created = []
        for info in clipInfos or []:
            path = info.get("FilePath") or info.get("media")
            if not path:
                continue
            created.append(self._current._add_clip(str(path), start=info.get("StartIndex"), end=info.get("EndIndex")))
        return created

    def CreateEmptyTimeline(self, name: str) -> Any:
        timeline = self._project._create_timeline(name)
        self._project._current_timeline = timeline
        return timeline

    def CreateTimelineFromClips(self, name: str, clipInfos: list[dict[str, Any]]) -> Any:
        timeline = self._project._create_timeline(name)
        self._project._current_timeline = timeline
        for info in clipInfos or []:
            timeline._place(item=info.get("mediaPoolItem"), track_type="video", track_index=1,
                            record_frame=info.get("recordFrame", 0), mode="append")
        return timeline

    def AppendToTimeline(self, clipInfos: list[dict[str, Any]]) -> list[Any]:
        timeline = self._project._current_timeline
        if timeline is None:
            return []
        placed = []
        for info in clipInfos or []:
            item = info.get("mediaPoolItem")
            if item is None:
                continue
            media_type = info.get("mediaType")
            track_type = "audio" if media_type == 2 else "video"
            track_index = info.get("trackIndex") or 1
            record_frame = info.get("recordFrame")
            mode = "insert" if record_frame is not None else "append"
            placed.append(
                timeline._place(
                    item=item,
                    track_type=track_type,
                    track_index=track_index,
                    record_frame=record_frame if record_frame is not None else timeline.GetEndFrame(),
                    mode=mode,
                    source_start=info.get("startFrame"),
                    source_end=info.get("endFrame"),
                )
            )
        return [p for p in placed if p is not None]

    def DeleteClips(self, clips: list[Any]) -> bool:
        if not isinstance(clips, list):
            return False
        for clip in clips:
            self._root._remove_clip(clip)
        return True

    def MoveClips(self, clips: list[Any], targetFolder: Any) -> bool:
        if targetFolder is None:
            return False
        for clip in clips or []:
            for folder in self._root._walk():
                if clip in folder._clips:
                    folder._clips.remove(clip)
                    targetFolder._clips.append(clip)
        return True

    def DeleteTimelines(self, timelines: list[Any]) -> bool:
        for timeline in timelines or []:
            if timeline in self._project._timelines:
                self._project._timelines.remove(timeline)
        return True

    def GetSelectedClips(self) -> list[Any]:
        return []

    def SetSelectedClip(self, mediaPoolItem: Any) -> bool:
        return True


class _Folder:
    def __init__(self, name: str, pool: _MediaPool, parent: "_Folder | None" = None) -> None:
        self._name = name
        self._pool = pool
        self._parent = parent
        self._subfolders: list[_Folder] = []
        self._clips: list[_MediaPoolItem] = []
        self._uid = f"folder-{abs(hash(name)) % 100000}-{abs(hash(str(parent._uid))) % 1000 if parent else 0}"

    def GetName(self) -> str:
        return self._name

    def GetUniqueId(self) -> str:
        return self._uid

    def GetSubFolderList(self) -> list[Any]:
        return list(self._subfolders)

    def GetClipList(self) -> list[Any]:
        return list(self._clips)

    def GetIsFolderStale(self) -> bool:
        return False

    def _add_subfolder(self, name: str) -> "_Folder":
        for folder in self._subfolders:
            if folder._name == name:
                return folder
        folder = _Folder(name, self._pool, parent=self)
        self._subfolders.append(folder)
        return folder

    def _add_clip(self, path: str, *, start: Any = None, end: Any = None) -> "_MediaPoolItem":
        name = Path(path).stem or path
        for clip in self._clips:
            if clip.GetClipProperty("File Path") == path:
                return clip
        clip = _MediaPoolItem(name, path, start=start, end=end)
        self._clips.append(clip)
        return clip

    def _remove_clip(self, clip: Any) -> None:
        for folder in self._walk():
            if clip in folder._clips:
                folder._clips.remove(clip)

    def _walk(self):
        yield self
        for sub in self._subfolders:
            yield from sub._walk()


class _MediaPoolItem:
    def __init__(self, name: str, path: str, *, start: Any = None, end: Any = None) -> None:
        self._name = name
        self._path = path
        self._start = start
        self._end = end
        self._metadata: dict[str, str] = {
            "Clip Name": name,
            "File Path": path,
            "Scene": "",
            "Take": "",
            "Camera #": "",
            "Keywords": "",
        }
        self._markers: dict[int, dict[str, Any]] = {}
        self._uid = f"clip-{abs(hash(path)) % 1000000}"

    def GetName(self) -> str:
        return self._name

    def SetName(self, name: str) -> bool:
        self._name = name
        self._metadata["Clip Name"] = name
        return True

    def GetUniqueId(self) -> str:
        return self._uid

    def GetMediaId(self) -> str:
        return self._uid

    def GetClipProperty(self, propertyName: str | None = None) -> Any:
        if propertyName:
            if propertyName == "Start TC":
                return _START_TIMECODE
            if propertyName == "Frames":
                return str(self._duration())
            return self._metadata.get(propertyName, "")
        return dict(self._metadata)

    def SetClipProperty(self, propertyName: str, propertyValue: str) -> bool:
        if not isinstance(propertyName, str):
            return False
        self._metadata[propertyName] = str(propertyValue)
        return True

    def GetMetadata(self, metadataType: str | None = None) -> Any:
        if metadataType:
            return self._metadata.get(metadataType, "")
        return dict(self._metadata)

    def SetMetadata(self, metadata: dict[str, Any]) -> bool:
        if not isinstance(metadata, dict):
            return False
        for key, value in metadata.items():
            self._metadata[key] = str(value)
        return True

    def GetThirdPartyMetadata(self, metadataType: str | None = None) -> Any:
        return "" if metadataType else {}

    def SetThirdPartyMetadata(self, metadata: dict[str, Any]) -> bool:
        return True

    def AddMarker(self, frameId: int, color: str, name: str, note: str, duration: int,
                  customData: str | None = None) -> bool:
        if color not in MARKER_COLORS:
            return False
        self._markers[int(frameId)] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": duration,
            "customData": customData or "",
        }
        return True

    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self._markers)

    def GetTimeline(self) -> Any:
        # 素材池条目不是时间线条目时，真实 API 返回 None（契约行为）。
        return None

    def _duration(self) -> int:
        if self._start is not None and self._end is not None:
            return int(self._end) - int(self._start) + 1
        return 240


# ---------------------------------------------------------------------------
# 时间线
# ---------------------------------------------------------------------------


class _Timeline:
    def __init__(self, name: str, project: _Project) -> None:
        self._name = name
        self._project = project
        self._items: list[_TimelineItem] = []
        self._markers: dict[int, dict[str, Any]] = {}
        self._tracks: dict[str, int] = {"video": 2, "audio": 2, "subtitle": 0}
        self._track_locks: dict[tuple[str, int], bool] = {}
        self._track_enabled: dict[tuple[str, int], bool] = {}
        self._track_names: dict[tuple[str, int], str] = {}
        self._playhead = 0
        self._uid = f"tl-{abs(hash(name)) % 1000000}"

    # 名称与范围 -----------------------------------------------------------
    def GetName(self) -> str:
        return self._name

    def SetName(self, timelineName: str) -> bool:
        if any(t._name == timelineName and t is not self for t in self._project._timelines):
            return False
        self._name = timelineName
        return True

    def GetUniqueId(self) -> str:
        return self._uid

    def GetStartFrame(self) -> int:
        return 0

    def GetEndFrame(self) -> int:
        if not self._items:
            return 0
        return max(int(item._end) for item in self._items)

    def GetStartTimecode(self) -> str:
        return _START_TIMECODE

    def SetStartTimecode(self, timecode: str) -> bool:
        return True

    def GetCurrentTimecode(self) -> str:
        fps = _fps(self._project)
        total = int(self._playhead)
        frames = total % int(round(fps))
        seconds = total // int(round(fps))
        return f"01:{seconds // 60:02d}:{seconds % 60:02d}:{frames:02d}"

    def SetCurrentTimecode(self, timecode: str) -> bool:
        self._playhead = 0
        return True

    # 轨道 -----------------------------------------------------------------
    def GetTrackCount(self, trackType: str) -> int:
        return self._tracks.get(trackType, 0)

    def AddTrack(self, trackType: str, subTrackType: str | None = None) -> bool:
        if trackType not in self._tracks:
            return False
        if trackType == "subtitle":
            return False  # 字幕轨道只能由 SRT 导入产生，与真实行为一致
        self._tracks[trackType] += 1
        return True

    def DeleteTrack(self, trackType: str, trackIndex: int) -> bool:
        if trackType not in self._tracks or not 1 <= trackIndex <= self._tracks[trackType]:
            return False
        self._tracks[trackType] -= 1
        self._items = [
            item
            for item in self._items
            if not (
                item._track_type == trackType
                and (item._track_index > trackIndex
                     or (item._track_index == trackIndex and False))
            )
        ]
        for item in self._items:
            if item._track_type == trackType and item._track_index > trackIndex:
                item._track_index -= 1
        return True

    def SetTrackLock(self, trackType: str, trackIndex: int, locked: bool) -> bool:
        self._track_locks[(trackType, trackIndex)] = bool(locked)
        return True

    def GetIsTrackLocked(self, trackType: str, trackIndex: int) -> bool:
        return self._track_locks.get((trackType, trackIndex), False)

    def SetTrackEnable(self, trackType: str, trackIndex: int, enabled: bool) -> bool:
        self._track_enabled[(trackType, trackIndex)] = bool(enabled)
        return True

    def GetIsTrackEnabled(self, trackType: str, trackIndex: int) -> bool:
        return self._track_enabled.get((trackType, trackIndex), True)

    def GetTrackName(self, trackType: str, trackIndex: int) -> str:
        return self._track_names.get((trackType, trackIndex), "")

    def SetTrackName(self, trackType: str, trackIndex: int, name: str) -> bool:
        self._track_names[(trackType, trackIndex)] = name
        return True

    def GetTrackSubType(self, trackType: str, trackIndex: int) -> str:
        return "stereo" if trackType == "audio" else ""

    # 片段 -----------------------------------------------------------------
    def GetItemListInTrack(self, trackType: str, index: int) -> list[Any]:
        items = [
            item for item in self._items
            if item._track_type == trackType and item._track_index == index
        ]
        items.sort(key=lambda i: i._start)
        return items

    def GetSelectedClips(self) -> list[Any]:
        return []

    def GetCurrentVideoItem(self) -> Any:
        for item in sorted(self._items, key=lambda i: i._start):
            if item._track_type == "video" and item._start <= self._playhead <= item._end:
                return item
        return None

    def GetCurrentClipThumbnailImage(self) -> dict[str, Any]:
        return {"width": 0, "height": 0, "format": "", "data": ""}

    def _place(
        self,
        *,
        item: Any,
        track_type: str,
        track_index: int,
        record_frame: float,
        mode: str,
        source_start: Any = None,
        source_end: Any = None,
    ) -> Any:
        if item is None:
            return None
        duration = item._duration()
        if source_start is not None and source_end is not None:
            duration = int(source_end) - int(source_start) + 1
        start = int(record_frame)
        if mode == "insert":
            for existing in self._items:
                if existing._track_type == track_type and existing._track_index == track_index and existing._start >= start:
                    existing._start += duration
                    existing._end += duration
        elif mode == "append":
            existing_ends = [
                e._end for e in self._items
                if e._track_type == track_type and e._track_index == track_index
            ]
            if existing_ends:
                start = max(existing_ends)
        created = _TimelineItem(
            name=item.GetName(),
            media=item,
            track_type=track_type,
            track_index=track_index,
            start=start,
            end=start + duration,
        )
        self._items.append(created)
        self._playhead = start
        return created

    def DeleteClips(self, timelineItems: list[Any], rippleDelete: bool = False) -> bool:
        if not isinstance(timelineItems, list):
            return False
        for item in timelineItems:
            if item in self._items:
                self._items.remove(item)
                if rippleDelete:
                    for other in self._items:
                        if (
                            other._track_type == item._track_type
                            and other._track_index == item._track_index
                            and other._start >= item._start
                        ):
                            other._start -= item._end - item._start
                            other._end -= item._end - item._start
        return True

    # 标记 -----------------------------------------------------------------
    def AddMarker(self, frameId: int, color: str, name: str, note: str, duration: int,
                  customData: str | None = None) -> bool:
        if color not in MARKER_COLORS:
            return False
        self._markers[int(frameId)] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": duration,
            "customData": customData or "",
        }
        return True

    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self._markers)

    def DeleteMarkerAtFrame(self, frameNum: int) -> bool:
        return self._markers.pop(int(frameNum), None) is not None

    def DeleteMarkersByColor(self, color: str) -> bool:
        if color == "All":
            self._markers.clear()
            return True
        for frame in [f for f, m in self._markers.items() if m["color"] == color]:
            del self._markers[frame]
        return True

    # 复制 -----------------------------------------------------------------
    def DuplicateTimeline(self, timelineName: str) -> Any:
        if any(t._name == timelineName for t in self._project._timelines):
            return None
        clone = _Timeline(timelineName, self._project)
        clone._tracks = dict(self._tracks)
        clone._markers = copy.deepcopy(self._markers)
        clone._track_locks = dict(self._track_locks)
        clone._track_enabled = dict(self._track_enabled)
        clone._track_names = dict(self._track_names)
        clone._items = [
            _TimelineItem(
                name=item._name,
                media=item._media,
                track_type=item._track_type,
                track_index=item._track_index,
                start=item._start,
                end=item._end,
                properties=copy.deepcopy(item._properties),
                color=item._color,
            )
            for item in self._items
        ]
        clone._grade = copy.deepcopy(getattr(self, "_grade", {}))
        self._project._timelines.append(clone)
        return clone

    # 其它 -----------------------------------------------------------------
    def GetNodeGraph(self) -> Any:
        if not hasattr(self, "_graph"):
            self._graph = _Graph("Timeline Graph")
        return self._graph

    def GetMediaPoolItem(self) -> Any:
        # 时间线条目若不对应素材池条目（如字幕/生成器），真实 API 返回 None。
        return None

    def GetSettings(self) -> dict[str, str]:
        return dict(self._project._settings)

    def SetSettings(self, settings: dict[str, Any]) -> bool:
        return self._project.SetSettings(settings)

    def CreateSubtitlesFromAudio(self, autoCaptionSettings: dict[str, Any] | None = None) -> bool:
        self._tracks["subtitle"] = max(1, self._tracks.get("subtitle", 0))
        return True

    def ImportIntoTimeline(self, filePath: str, importOptions: dict[str, Any] | None = None) -> bool:
        path = Path(filePath)
        if path.suffix.lower() not in {".srt", ".vtt", ".txt", ".aaf", ".edl", ".xml"}:
            return False
        if path.suffix.lower() == ".srt":
            return _import_srt_into(self, path)
        return path.exists()

    def Export(self, fileName: str, exportType: Any, exportSubtype: Any) -> bool:
        Path(fileName).write_text("simulated timeline export", encoding="utf-8")
        return True


class _TimelineItem:
    def __init__(
        self,
        *,
        name: str,
        media: Any,
        track_type: str,
        track_index: int,
        start: int,
        end: int,
        properties: dict[str, Any] | None = None,
        color: str = "",
    ) -> None:
        self._name = name
        self._media = media
        self._track_type = track_type
        self._track_index = track_index
        self._start = int(start)
        self._end = int(end)
        self._properties: dict[str, Any] = {
            "Pan": 0.0,
            "Tilt": 0.0,
            "ZoomX": 1.0,
            "ZoomY": 1.0,
            "RotationAngle": 0.0,
            "CropLeft": 0.0,
            "CropRight": 0.0,
            "CropTop": 0.0,
            "CropBottom": 0.0,
            "Opacity": 100.0,
            "TransformEnabled": False,
            "CroppingEnabled": False,
            "DynamicZoomEnabled": False,
            "CompositeEnabled": False,
        }
        if properties:
            self._properties.update(properties)
        self._color = color
        self._markers: dict[int, dict[str, Any]] = {}
        self._graph = _Graph(name)
        self._fusion_comps: list[Any] = []

    # 基础 -----------------------------------------------------------------
    def GetName(self) -> str:
        return self._name

    def SetName(self, name: str) -> bool:
        self._name = name
        return True

    def GetType(self) -> str:
        return "audio" if self._track_type == "audio" else "video"

    def GetStart(self, subframePrecision: bool = False) -> float:
        return float(self._start)

    def GetEnd(self, subframePrecision: bool = False) -> float:
        return float(self._end)

    def GetDuration(self, subframePrecision: bool = False) -> float:
        return float(self._end - self._start)

    def GetSourceStartFrame(self) -> int:
        return 0

    def GetSourceEndFrame(self) -> int:
        return self._end - self._start

    def GetLeftOffset(self, subframePrecision: bool = False) -> float:
        return 0.0

    def GetRightOffset(self, subframePrecision: bool = False) -> float:
        return 0.0

    def GetTrackTypeAndIndex(self) -> list[Any]:
        return [self._track_type, self._track_index]

    def GetMediaPoolItem(self) -> Any:
        return self._media

    # 属性 -----------------------------------------------------------------
    def GetProperties(self) -> dict[str, Any]:
        return dict(self._properties)

    def GetProperty(self, key: str) -> Any:
        return self._properties.get(key)

    def SetProperties(self, properties: dict[str, Any]) -> bool:
        if not isinstance(properties, dict) or not properties:
            return False
        # 全部键先校验，任何一个非法 -> 整体失败（与真实 API 一致）
        for key, value in properties.items():
            if key in _PROPERTY_BOOLS:
                if not isinstance(value, (bool, int)):
                    return False
                continue
            if key in _PROPERTY_RANGES:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return False
                bounds = _PROPERTY_RANGES[key]
                if bounds is not None and not (bounds[0] <= number <= bounds[1]):
                    return False
                continue
            return False
        self._properties.update(properties)
        return True

    def SetProperty(self, key: str, value: Any) -> bool:
        return self.SetProperties({key: value})

    def GetClipColor(self) -> str:
        return self._color

    def SetClipColor(self, colorName: str) -> bool:
        self._color = colorName
        return True

    def ClearClipColor(self) -> bool:
        self._color = ""
        return True

    def GetClipEnabled(self) -> bool:
        return True

    def SetClipEnabled(self, enabled: bool) -> bool:
        return True

    # 标记 -----------------------------------------------------------------
    def AddMarker(self, frameId: int, color: str, name: str, note: str, duration: int,
                  customData: str | None = None) -> bool:
        if color not in MARKER_COLORS:
            return False
        self._markers[int(frameId)] = {
            "color": color, "name": name, "note": note,
            "duration": duration, "customData": customData or "",
        }
        return True

    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self._markers)

    # 调色 -----------------------------------------------------------------
    def GetNodeGraph(self, layerIdx: int | None = None) -> Any:
        return self._graph

    def CopyGrades(self, tgtTimelineItems: list[Any]) -> bool:
        if not isinstance(tgtTimelineItems, list):
            return False
        for target in tgtTimelineItems:
            if hasattr(target, "_graph"):
                target._graph = copy.deepcopy(self._graph)
        return True

    # Fusion ---------------------------------------------------------------
    def GetFusionCompCount(self) -> int:
        return len(self._fusion_comps)

    def GetFusionCompNameList(self) -> list[str]:
        return [comp._name for comp in self._fusion_comps]

    def GetFusionCompByIndex(self, compIndex: int) -> Any:
        if 1 <= compIndex <= len(self._fusion_comps):
            return self._fusion_comps[compIndex - 1]
        return None

    def GetFusionCompByName(self, compName: str) -> Any:
        for comp in self._fusion_comps:
            if comp._name == compName:
                return comp
        return None

    def AddFusionComp(self) -> Any:
        comp = _FusionComp(f"Comp{len(self._fusion_comps) + 1}")
        self._fusion_comps.append(comp)
        return comp


# ---------------------------------------------------------------------------
# 调色节点图
# ---------------------------------------------------------------------------


class _Graph:
    def __init__(self, name: str) -> None:
        self._nodes: list[dict[str, Any]] = [
            {"label": "Serial 1", "lut": "", "enabled": True, "tools": ["PrimaryIn"]},
            {"label": "Serial 2", "lut": "", "enabled": True, "tools": []},
        ]
        self._name = name

    def GetNumNodes(self) -> int:
        return len(self._nodes)

    def GetNodeLabel(self, nodeIndex: int) -> str:
        if 1 <= nodeIndex <= len(self._nodes):
            return self._nodes[nodeIndex - 1]["label"]
        return ""

    def SetNodeLabel(self, nodeIndex: int, label: str) -> bool:
        if 1 <= nodeIndex <= len(self._nodes):
            self._nodes[nodeIndex - 1]["label"] = label
            return True
        return False

    def GetToolsInNode(self, nodeIndex: int) -> list[str]:
        if 1 <= nodeIndex <= len(self._nodes):
            return list(self._nodes[nodeIndex - 1]["tools"])
        return []

    def SetLUT(self, nodeIndex: int, lutPath: str) -> bool:
        if not 1 <= nodeIndex <= len(self._nodes):
            return False
        if not Path(lutPath).exists():
            return False
        self._nodes[nodeIndex - 1]["lut"] = str(lutPath)
        return True

    def GetLUT(self, nodeIndex: int) -> str:
        if 1 <= nodeIndex <= len(self._nodes):
            return self._nodes[nodeIndex - 1]["lut"]
        return ""

    def SetNodeEnabled(self, nodeIndex: int, isEnabled: bool) -> bool:
        if not 1 <= nodeIndex <= len(self._nodes):
            return False
        self._nodes[nodeIndex - 1]["enabled"] = bool(isEnabled)
        return True

    def GetNodeEnabled(self, nodeIndex: int) -> bool:
        if 1 <= nodeIndex <= len(self._nodes):
            return bool(self._nodes[nodeIndex - 1]["enabled"])
        return False

    def ApplyGradeFromDRX(self, path: str, gradeMode: int) -> bool:
        if not Path(path).exists() or gradeMode not in (0, 1, 2):
            return False
        self._nodes.append(
            {"label": f"DRX {Path(path).stem}", "lut": "", "enabled": True, "tools": ["SerialNode"]}
        )
        return True

    def ResetAllGrades(self) -> bool:
        self._nodes = [{"label": "Serial 1", "lut": "", "enabled": True, "tools": ["PrimaryIn"]}]
        return True


# ---------------------------------------------------------------------------
# Fusion 合成
# ---------------------------------------------------------------------------


class _FusionComp:
    def __init__(self, name: str) -> None:
        self._name = name
        self._tools: dict[str, _FusionTool] = {
            "MediaIn1": _FusionTool("MediaIn1", "MediaIn"),
            "MediaOut1": _FusionTool("MediaOut1", "MediaOut"),
        }

    def GetName(self) -> str:
        return self._name

    def GetAttrs(self) -> dict[str, Any]:
        return {"COMPS_FileName": "", "COMPS_Name": self._name}

    def AddTool(self, toolType: str, x: int = -32768, y: int = -32768) -> Any:
        index = 1
        while f"{toolType}{index}" in self._tools:
            index += 1
        tool = _FusionTool(f"{toolType}{index}", toolType)
        self._tools[tool._tool_name] = tool
        return tool

    def FindTool(self, name: str) -> Any:
        return self._tools.get(name)

    def GetToolList(self, selected: bool = False) -> dict[str, Any]:
        return dict(self._tools)

    def Lock(self) -> bool:
        return True

    def Unlock(self) -> bool:
        return True


class _FusionTool:
    def __init__(self, tool_name: str, tool_type: str) -> None:
        self._tool_name = tool_name
        self._tool_type = tool_type
        self._inputs: dict[str, Any] = {
            "StyledText": "Text",
            "Size": 0.05,
            "Center": {"1": 0.5, "2": 0.5},
            "Gain": 1.0,
            "Lift": 0.0,
        }

    def GetAttrs(self) -> dict[str, Any]:
        return {"TOOLS_Name": self._tool_name, "TOOLS_RegID": self._tool_type}

    def SetInput(self, name: str, value: Any, time: int | None = None) -> bool:
        if name not in self._inputs and not name.startswith(("Center", "Size", "Text", "Font")):
            return False
        self._inputs[name] = value
        return True

    def GetInput(self, name: str, time: int | None = None) -> Any:
        return self._inputs.get(name)

    def GetInputList(self) -> dict[str, Any]:
        return {name: {"Name": name} for name in self._inputs}


# ---------------------------------------------------------------------------
# 画廊
# ---------------------------------------------------------------------------


class _Gallery:
    def GetGalleryStillAlbums(self) -> list[Any]:
        return []

    def CreateGalleryStillAlbum(self) -> Any:
        # 模拟器不实现静帧相册：真实 API 在无相册时也可能返回 None。
        return None

    def GetCurrentStillAlbum(self) -> Any:
        # 模拟器没有当前静帧相册；真实 API 同样可能返回 None。
        return None


# ---------------------------------------------------------------------------
# SRT 解析（模拟 Timeline.ImportIntoTimeline 的字幕行为）
# ---------------------------------------------------------------------------


def _fps(project: _Project) -> float:
    try:
        return float(str(project._settings.get("timelineFrameRate", "24")).split()[0])
    except ValueError:
        return 24.0


def _import_srt_into(timeline: _Timeline, path: Path) -> bool:
    try:
        from .subtitle_srt import parse_srt

        cues = parse_srt(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:  # noqa: BLE001
        return False
    timeline._tracks["subtitle"] = max(1, timeline._tracks.get("subtitle", 0))
    fps = _fps(timeline._project)
    for cue in cues:
        start = int(round(cue.start_seconds * fps))
        end = int(round(cue.end_seconds * fps))
        item = _TimelineItem(
            name=cue.text.splitlines()[0] if cue.text else "Subtitle",
            media=None,
            track_type="subtitle",
            track_index=1,
            start=start,
            end=end,
        )
        timeline._items.append(item)
    return True


# ---------------------------------------------------------------------------
# 模块入口（与 DaVinciResolveScript 的对外形状一致）
# ---------------------------------------------------------------------------


def scriptapp(name: str = "Resolve") -> Any:
    """等价于真实模块的 ``scriptapp``。"""

    if name != "Resolve":
        return None
    return _Resolve()


def GetResolve() -> Any:
    return scriptapp("Resolve")


def __getattr__(name: str) -> Any:  # pragma: no cover - 仅为可读性
    raise AttributeError(f"模拟器不提供 {name}")
