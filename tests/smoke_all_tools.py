#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全工具冒烟测试：对 36 个工具各打一次最小调用，确保没有"未预期崩溃"。

判定标准：
* ``ok=true``            → 工具正常工作；
* ``ok=false`` + 已知错误码 → 预期内的业务错误（参数不全/环境不满足）；
* ``error=unexpected_error`` → **失败**，说明代码有 bug。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["QING_DAVINCI_FAKE"] = "1"
_TMP = Path(tempfile.mkdtemp(prefix="qing_smoke_"))
os.environ["QING_STATE_DIR"] = str(_TMP / "state")
os.environ["QING_TEMP_DIR"] = str(_TMP / "previews")

# 造夹具素材
MEDIA = _TMP / "media"
MEDIA.mkdir()
(MEDIA / "A001C003_20240315_0001.mov").write_bytes(b"\0" * 8)
(MEDIA / "CAM_B_20240316_0001.mp4").write_bytes(b"\0" * 8)
CUBE = _TMP / "look.cube"
CUBE.write_text("TITLE \"t\"\nLUT_3D_SIZE 2\n" + "0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n", encoding="utf-8")
DRX = _TMP / "look.drx"
DRX.write_text("<xml/>", encoding="utf-8")
SRT = _TMP / "sub.srt"
SRT.write_text("1\n00:00:01,000 --> 00:00:02,000\n青青\n", encoding="utf-8")

KNOWN_ERRORS = {
    "preflight_failed", "api_failure", "unsupported", "no_project", "no_timeline",
    "resolve_not_connected", "resolve_not_installed", "backup_failed",
    "resolve_free_version", "unexpected_error",
}

CASES: list[tuple[str, dict]] = [
    ("qing_resolve_health", {}),
    ("qing_resolve_version", {}),
    ("qing_project_overview", {"include_timeline_items": True}),
    ("qing_open_page", {"page": "color"}),
    ("qing_media_scan_folder", {"folder": str(MEDIA)}),
    ("qing_media_import_folder", {"folder": str(MEDIA), "bin_path": "Master/Smoke"}),
    ("qing_media_list_bins", {"max_depth": 4}),
    ("qing_media_search", {"query": "A001"}),
    ("qing_media_auto_bin", {"rule": "by_kind", "source_bin": "Master/Smoke"}),
    ("qing_media_set_metadata", {"metadata": {"Scene": "1"}, "bin_path": "Master"}),
    ("qing_timeline_inspect", {}),
    ("qing_timeline_create", {"name": "Smoke_TL", "fps": "25"}),
    ("qing_timeline_add_clips", {"placements": [
        {"clip_name": "A001C003_20240315_0001", "track_type": "video", "track_index": 1},
        {"clip_name": "CAM_B_20240316_0001", "track_type": "video", "track_index": 1, "record_frame": 30},
    ]}),
    ("qing_timeline_get_clip_properties", {"item_index": 0}),
    ("qing_timeline_set_clip_properties", {"properties": {"ZoomX": 1.1}, "item_indexes": [0]}),
    ("qing_timeline_add_markers", {"markers": [{"frame": 5, "color": "Red", "name": "m1"}]}),
    ("qing_timeline_read_markers", {}),
    ("qing_timeline_import_srt", {"srt_path": str(SRT)}),
    ("qing_timeline_set_track_state", {"track_type": "video", "track_index": 2, "locked": False}),
    ("qing_color_inspect_nodes", {}),
    ("qing_color_set_lut", {"lut_path": str(CUBE), "node_index": 1}),
    ("qing_color_set_node_enabled", {"node_index": 1, "enabled": True}),
    # copy_grade 放在 delete_clips 之前，保证时间线上仍有多个片段
    ("qing_color_copy_grade", {"whole_timeline": True}),
    ("qing_color_apply_drx", {"drx_path": str(DRX), "scope": "whole_timeline"}),
    ("qing_color_grab_frame", {"tag": "smoke"}),
    ("qing_fusion_inspect", {}),
    ("qing_fusion_add_tool", {"tool_type": "TextPlus", "inputs": [{"name": "StyledText", "value": "青青"}]}),
    ("qing_fusion_set_input", {"tool_name": "TextPlus1", "inputs": [{"name": "Size", "value": 0.07}]}),
    # 子剪辑用例放在最后：前面已给素材池片段打好标记（setup_media_markers）
    ("qing_media_markers_to_subclips", {"clip_name": "A001C003_20240315_0001"}),
    ("qing_timeline_delete_clips", {"item_indexes": [0]}),
    ("qing_deliver_list_presets", {}),
    ("qing_deliver_setup_job", {"jobs": [{"target_dir": str(_TMP / "render"), "preset": "H.264 Master"}]}),
    ("qing_deliver_queue_status", {}),
    ("qing_deliver_start", {"wait_seconds": 0}),
    ("qing_deliver_stop", {}),
    ("qing_deliver_delete_jobs", {}),
]


def setup_media_markers() -> None:
    """给媒体池里的素材打标记，让子剪辑用例走通正常分支。

    真实场景里这些标记来自源监视器；模拟器直接对 MediaPoolItem 调 AddMarker。
    """

    from lib.modules.resolve_connector import get_session

    session = get_session()
    pool = session.media_pool()
    root = pool.GetRootFolder()
    for _, clip in _walk_clips(root):
        if clip.GetName() == "A001C003_20240315_0001":
            clip.AddMarker(0, "Red", "take1", "", 48)
            clip.AddMarker(60, "Blue", "take2", "", 36)
            return


def _walk_clips(folder):
    for clip in folder.GetClipList() or []:
        yield folder, clip
    for sub in folder.GetSubFolderList() or []:
        yield from _walk_clips(sub)


async def main() -> int:
    from lib.server import build_server

    server = build_server()
    registered = {tool.name for tool in await server.list_tools()}
    covered = {name for name, _ in CASES}

    print(f"已注册 {len(registered)} 个工具，冒烟用例 {len(CASES)} 个")
    missing = registered - covered
    extra = covered - registered
    if missing:
        print(f"  ! 未被冒烟覆盖：{sorted(missing)}")
    if extra:
        print(f"  ! 冒烟里写了不存在的工具：{sorted(extra)}")

    crashes = []
    prepared = False
    for name, args in CASES:
        # 子剪辑用例之前先把源素材的标记准备出来
        if name == "qing_media_markers_to_subclips" and not prepared:
            setup_media_markers()
            prepared = True
        result = await server.call_tool(name, args)
        blocks = result.content
        text = next((b.text for b in blocks if getattr(b, "type", "") == "text"), "")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            print(f"  ~ {name}: 非 JSON 结果（可能是图片块），bloccks={[b.type for b in blocks]}")
            continue

        if payload.get("ok") is True:
            print(f"  OK   {name}")
        elif payload.get("error") in KNOWN_ERRORS and payload.get("error") != "unexpected_error":
            print(f"  ERR  {name}: {payload['error']} — {payload.get('message', '')[:70]}")
        else:
            print(f"  FAIL {name}: {payload.get('error')} — {payload.get('message', '')[:120]}")
            crashes.append((name, payload))

    print()
    if crashes:
        print(f"❌ {len(crashes)} 个工具出现未预期错误：")
        for name, payload in crashes:
            print(f"   {name}: {json.dumps(payload, ensure_ascii=False)[:300]}")
        return 1

    print(f"✅ 全部 {len(CASES)} 个工具冒烟通过（无未预期错误）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
