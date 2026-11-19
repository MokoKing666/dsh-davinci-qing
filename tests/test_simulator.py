#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟器端到端测试：不需要真实 DaVinci Resolve。

覆盖：环境自检、检查点备份、预检拦截、媒体池导入/分箱/元数据、
时间线落片/属性/标记/SRT、调色 LUT/DRX/节点/截图、Fusion 节点、交付队列。

运行::

    python -m pytest tests -q
    # 或
    python tests/test_simulator.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ["QING_DAVINCI_FAKE"] = "1"
os.environ.setdefault("QING_STATE_DIR", str(Path(__file__).parent / "_state"))
os.environ.setdefault("QING_TEMP_DIR", str(Path(__file__).parent / "_previews"))


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试用例一套干净的模拟器状态 + 临时目录。"""

    monkeypatch.setenv("QING_DAVINCI_FAKE", "1")
    monkeypatch.setenv("QING_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QING_TEMP_DIR", str(tmp_path / "previews"))

    import dsh_davinci_qing.fake_resolve as fake

    fake._reset_state()

    import dsh_davinci_qing.resolve_connector as connector

    connector._SESSION = None  # 丢弃进程级单例
    session = connector.get_session(force_new=True)

    return {"session": session, "tmp": tmp_path, "fake": fake}


def _call(tool_name: str, /, **kwargs):
    """构造服务器并同步调用某个工具。"""

    import asyncio

    from dsh_davinci_qing.server import build_server

    server = build_server()
    result = asyncio.run(server.call_tool(tool_name, kwargs))
    content = result.content
    if not content:
        return None
    text = content[0].text
    return json.loads(text)


# ---------------------------------------------------------------------------
# 环境层
# ---------------------------------------------------------------------------


def test_health_ok(fake_env):
    payload = _call("qing_resolve_health")
    assert payload["ok"] is True
    ids = {check["id"] for check in payload["checks"]}
    assert {"scripting_paths", "connection", "studio", "project", "timeline"} <= ids
    assert payload["project_name"] == "青青演示工程"


def test_version_and_overview(fake_env):
    version = _call("qing_resolve_version")
    assert version["ok"] is True
    assert "Studio" in version["product_name"]

    overview = _call("qing_project_overview", include_timeline_items=True)
    assert overview["ok"] is True
    assert overview["timeline_count"] >= 1
    assert overview["current_timeline"] == "V1_主时间线"


# ---------------------------------------------------------------------------
# 安全检查点与预检
# ---------------------------------------------------------------------------


def test_checkpoint_creates_backup(fake_env):
    from dsh_davinci_qing.safety_guard import resolve_checkpoint

    session = fake_env["session"]
    project = session.project()
    timeline = session.timeline()
    checkpoint = resolve_checkpoint(project, timeline, reason="unit_test")
    assert checkpoint.saved is True
    assert checkpoint.backup_name is not None
    # 规格要求的命名格式：[Backup_HHMMSS]_原时间线名
    assert checkpoint.backup_name.startswith("[Backup_")
    assert checkpoint.backup_name.endswith("]_V1_主时间线")
    # 备份时间线确实存在
    names = [
        project.GetTimelineByIndex(i).GetName()
        for i in range(1, project.GetTimelineCount() + 1)
    ]
    assert checkpoint.backup_name in names


def test_preflight_blocks_out_of_range_track(fake_env):
    payload = _call(
        "qing_timeline_add_markers",
        markers=[{"frame": 99999999, "color": "Red", "name": "越界"}],
    )
    assert payload["ok"] is False
    assert payload["error"] == "preflight_failed"
    assert payload["detail"]["violations"][0]["code"] == "frame_after_end"


def test_preflight_blocks_missing_lut(fake_env):
    payload = _call(
        "qing_color_set_lut",
        lut_path=str(fake_env["tmp"] / "nope.cube"),
    )
    assert payload["ok"] is False
    assert payload["error"] == "preflight_failed"
    assert payload["detail"]["violations"][0]["code"] == "file_not_found"


def test_preflight_blocks_relative_path(fake_env):
    payload = _call("qing_color_set_lut", lut_path="relative/lut.cube")
    assert payload["ok"] is False
    assert payload["detail"]["violations"][0]["code"] == "relative_path"


def test_delete_creates_checkpoint_then_deletes(fake_env):
    session = fake_env["session"]
    before = session.project().GetTimelineCount()
    payload = _call("qing_timeline_delete_clips", track_type="video", track_index=1, item_indexes=[0])
    assert payload["ok"] is True
    assert payload["deleted"] is True
    assert payload["checkpoint"]["backup_timeline"].startswith("[Backup_")
    after = session.project().GetTimelineCount()
    assert after == before + 1


# ---------------------------------------------------------------------------
# 媒体池
# ---------------------------------------------------------------------------


def _make_media(tmp_path: Path) -> Path:
    """造一批假素材文件（内容为空，只需路径与扩展名）。"""

    root = tmp_path / "media"
    (root / "A_Cam").mkdir(parents=True)
    (root / "B_Cam").mkdir(parents=True)
    names = [
        ("A_Cam", "A001C003_20240315_0001.mov"),
        ("A_Cam", "A001C003_20240315_0002.mov"),
        ("B_Cam", "CAM_B_20240316_0001.mp4"),
        ("B_Cam", "CAM_B_20240316_0002.wav"),
        ("B_Cam", "CAM_B_20240316_0003.png"),
    ]
    for folder, name in names:
        (root / folder / name).write_bytes(b"\x00" * 16)
    return root


def test_media_scan_and_import(fake_env):
    root = _make_media(fake_env["tmp"])
    scan = _call("qing_media_scan_folder", folder=str(root))
    assert scan["ok"] is True
    assert scan["file_count"] == 5
    assert scan["counts"].get("video", 0) >= 2

    imported = _call(
        "qing_media_import_folder",
        folder=str(root),
        bin_path="Master/Qing/Import",
        recursive=True,
    )
    assert imported["ok"] is True
    assert imported["imported_count"] == 5
    assert imported["bin_path"] == "Master/Qing/Import"

    bins = _call("qing_media_list_bins", max_depth=5)
    assert bins["ok"] is True
    assert "Qing" in json.dumps(bins, ensure_ascii=False)


def test_media_import_dry_run_does_not_write(fake_env):
    root = _make_media(fake_env["tmp"])
    payload = _call("qing_media_import_folder", folder=str(root), dry_run=True)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["would_import"] == 5


def test_media_auto_bin_by_camera(fake_env):
    root = _make_media(fake_env["tmp"])
    _call("qing_media_import_folder", folder=str(root), bin_path="Master/Take1")

    payload = _call("qing_media_auto_bin", rule="by_camera", source_bin="Master/Take1")
    assert payload["ok"] is True
    assert payload["total_moved"] == 5
    assert any("A" == key or key.startswith("A") for key in payload["bins"])


def test_media_metadata_and_search(fake_env):
    root = _make_media(fake_env["tmp"])
    _call("qing_media_import_folder", folder=str(root), bin_path="Master/Meta")

    updated = _call(
        "qing_media_set_metadata",
        metadata={"Scene": "12A", "Take": "3", "Camera #": "A", "Keywords": "海滩,日落"},
        bin_path="Master/Meta",
    )
    assert updated["ok"] is True
    assert updated["updated"] == 5

    found = _call("qing_media_search", query="海滩")
    assert found["ok"] is True
    assert found["match_count"] == 5


# ---------------------------------------------------------------------------
# 时间线
# ---------------------------------------------------------------------------


def test_timeline_create_and_add_clips(fake_env):
    root = _make_media(fake_env["tmp"])
    _call("qing_media_import_folder", folder=str(root), bin_path="Master/Edit")

    created = _call(
        "qing_timeline_create",
        name="竖屏_V2",
        width=1080,
        height=1920,
        fps="25",
    )
    assert created["ok"] is True
    assert created["timeline_name"] == "竖屏_V2"

    placed = _call(
        "qing_timeline_add_clips",
        placements=[
            {"clip_name": "A001C003_20240315_0001", "track_type": "video", "track_index": 1},
            {
                "clip_name": "A001C003_20240315_0002",
                "track_type": "video",
                "track_index": 1,
                "record_frame": 12,
            },
        ],
        timeline_name="竖屏_V2",
    )
    assert placed["ok"] is True
    assert placed["placed_count"] == 2

    inspect = _call("qing_timeline_inspect", timeline_name="竖屏_V2")
    assert inspect["ok"] is True
    assert inspect["fps"] == 25.0
    assert inspect["tracks"]["video"][0]["item_count"] == 2


def test_clip_properties_roundtrip(fake_env):
    payload = _call(
        "qing_timeline_set_clip_properties",
        properties={"ZoomX": 1.2, "ZoomY": 1.2, "Pan": 40.0, "RotationAngle": 5.0, "CropLeft": 10.0},
        item_indexes=[0],
    )
    assert payload["ok"] is True
    assert payload["updated"] == 1
    assert payload["checkpoint"]["backup_timeline"]

    read = _call("qing_timeline_get_clip_properties", item_index=0)
    assert read["ok"] is True
    assert read["properties"]["ZoomX"] == 1.2
    assert read["properties"]["TransformEnabled"] is True  # 自动打开变换区块
    assert read["properties"]["CroppingEnabled"] is True


def test_clip_properties_rejects_invalid_value(fake_env):
    payload = _call(
        "qing_timeline_set_clip_properties",
        properties={"ZoomX": 999.0},  # 超出 0..100
        item_indexes=[0],
    )
    assert payload["ok"] is True
    assert payload["updated"] == 0
    assert payload["failures"]


def test_markers_batch(fake_env):
    payload = _call(
        "qing_timeline_add_markers",
        markers=[
            {"frame": 10, "color": "Red", "name": "卡点1", "note": "重拍", "duration": 4},
            {"frame": 40, "color": "Red", "name": "卡点2", "note": "保留", "duration": 2},
            {"frame": 80, "color": "Blue", "name": "参考"},
        ],
        clear_color_first="Red",
    )
    assert payload["ok"] is True
    assert payload["created"] == 3

    read = _call("qing_timeline_read_markers")
    assert read["ok"] is True
    assert read["count"] == 3
    assert read["markers"][0]["timecode"]


def test_track_state_and_locked_write_blocked(fake_env):
    locked = _call(
        "qing_timeline_set_track_state",
        track_type="video",
        track_index=1,
        locked=True,
        name="V1_主",
    )
    assert locked["ok"] is True
    assert locked["current"]["locked"] is True

    blocked = _call(
        "qing_timeline_set_clip_properties",
        properties={"ZoomX": 1.1},
        item_indexes=[0],
    )
    assert blocked["ok"] is False
    assert blocked["error"] == "preflight_failed"
    assert blocked["detail"]["violations"][0]["code"] == "track_locked"


def test_srt_import(fake_env):
    srt = fake_env["tmp"] / "sample.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n第一句\n\n"
        "2\n00:00:03,500 --> 00:00:05,000\n第二句\n换行内容\n",
        encoding="utf-8",
    )
    payload = _call("qing_timeline_import_srt", srt_path=str(srt))
    assert payload["ok"] is True
    assert payload["imported"] is True
    assert payload["cue_count"] == 2
    assert payload["cues"][0]["start_frame"] == 24


def test_srt_from_inline_cues(fake_env):
    payload = _call(
        "qing_timeline_import_srt",
        cues=[
            {"text": "青青", "start_seconds": 0.0, "end_seconds": 1.5},
            {"text": "住进你的dsh", "start_seconds": 1.5, "end_seconds": 3.0},
        ],
        dry_run=True,
    )
    assert payload["ok"] is True
    assert payload["cue_count"] == 2


# ---------------------------------------------------------------------------
# Color
# ---------------------------------------------------------------------------


@pytest.fixture()
def cube_file(tmp_path: Path) -> Path:
    path = tmp_path / "qing_test.cube"
    path.write_text(
        "TITLE \"qing test\"\nLUT_3D_SIZE 2\n"
        "0.0 0.0 0.0\n1.0 0.0 0.0\n0.0 1.0 0.0\n1.0 1.0 0.0\n"
        "0.0 0.0 1.0\n1.0 0.0 1.0\n0.0 1.0 1.0\n1.0 1.0 1.0\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def drx_file(tmp_path: Path) -> Path:
    path = tmp_path / "qing_look.drx"
    path.write_text("<xml>simulated drx</xml>", encoding="utf-8")
    return path


def test_color_inspect_nodes(fake_env):
    payload = _call("qing_color_inspect_nodes")
    assert payload["ok"] is True
    assert payload["node_count"] == 2
    assert payload["nodes"][0]["index"] == 1


def test_color_set_lut_and_read(fake_env, cube_file):
    applied = _call("qing_color_set_lut", lut_path=str(cube_file), node_index=1)
    assert applied["ok"] is True
    assert applied["success"] == 1
    assert applied["checkpoint"]["backup_timeline"]

    inspected = _call("qing_color_inspect_nodes")
    assert inspected["nodes"][0]["lut"] == str(cube_file)


def test_color_set_lut_node_out_of_range(fake_env, cube_file):
    payload = _call("qing_color_set_lut", lut_path=str(cube_file), node_index=9)
    assert payload["ok"] is False
    assert payload["detail"]["violations"][0]["code"] == "node_index_out_of_range"


def test_color_node_enable(fake_env):
    payload = _call("qing_color_set_node_enabled", node_index=2, enabled=False)
    assert payload["ok"] is True
    assert payload["applied"] is True
    assert payload["nodes"][1]["enabled"] is False


def test_color_copy_grade(fake_env):
    # 需要至少两个片段：先导入素材并放第二条到轨道上
    root = _make_media(fake_env["tmp"])
    _call("qing_media_import_folder", folder=str(root), bin_path="Master/Grade")
    _call(
        "qing_timeline_add_clips",
        placements=[{"clip_name": "A001C003_20240315_0001", "track_type": "video", "track_index": 1}],
    )
    payload = _call("qing_color_copy_grade", source_item_index=0, whole_timeline=True)
    assert payload["ok"] is True
    assert payload["copied"] is True
    assert payload["target_count"] >= 1


def test_color_apply_drx_scopes(fake_env, drx_file):
    payload = _call("qing_color_apply_drx", drx_path=str(drx_file), scope="whole_timeline")
    assert payload["ok"] is True
    assert payload["success"] >= 1
    assert payload["grade_mode"].startswith("No keyframes")


def test_color_grab_frame_returns_image(fake_env):
    import asyncio

    from dsh_davinci_qing.server import build_server

    server = build_server()
    result = asyncio.run(server.call_tool("qing_color_grab_frame", {"tag": "unit_test"}))
    types = [block.type for block in result.content]
    assert "image" in types, types
    text_block = next(block for block in result.content if block.type == "text")
    payload = json.loads(text_block.text)
    assert payload["ok"] is True
    assert Path(payload["preview"]["file_path"]).exists()
    assert payload["review_checklist"]


# ---------------------------------------------------------------------------
# Fusion 与 Deliver
# ---------------------------------------------------------------------------


def test_fusion_add_text_plus(fake_env):
    payload = _call(
        "qing_fusion_add_tool",
        tool_type="TextPlus",
        inputs=[{"name": "StyledText", "value": "青青"}],
    )
    assert payload["ok"] is True
    assert payload["tool"]["type"] == "TextPlus"
    assert payload["inputs_applied"][0]["value"] == "青青"

    inspected = _call("qing_fusion_inspect")
    assert inspected["ok"] is True
    assert inspected["comps"][0]["tool_count"] >= 3


def test_fusion_rejects_unknown_tool(fake_env):
    payload = _call("qing_fusion_add_tool", tool_type="NotARealTool")
    assert payload["ok"] is False
    assert payload["error"] == "api_failure"


def test_fusion_set_input(fake_env):
    _call("qing_fusion_add_tool", tool_type="TextPlus", inputs=[{"name": "StyledText", "value": "旧文案"}])
    payload = _call(
        "qing_fusion_set_input",
        tool_name="TextPlus1",
        inputs=[{"name": "StyledText", "value": "新文案"}, {"name": "Size", "value": 0.08}],
    )
    assert payload["ok"] is True
    assert len(payload["applied"]) == 2


def test_deliver_presets_and_job_queue(fake_env):
    presets = _call("qing_deliver_list_presets")
    assert presets["ok"] is True
    assert "H.264 Master" in presets["presets"]

    out_dir = fake_env["tmp"] / "render"
    queued = _call(
        "qing_deliver_setup_job",
        jobs=[
            {
                "target_dir": str(out_dir),
                "filename": "qing_master",
                "preset": "H.264 Master",
            }
        ],
    )
    assert queued["ok"] is True
    assert queued["queued_count"] == 1

    status = _call("qing_deliver_queue_status")
    assert status["ok"] is True
    assert status["job_count"] == 1

    started = _call("qing_deliver_start", wait_seconds=0)
    assert started["ok"] is True
    assert started["status"][0]["JobStatus"] in {"Ready", "Complete", "Rendering"}

    deleted = _call("qing_deliver_delete_jobs")
    assert deleted["ok"] is True
    assert deleted["cleared_all"] is True


def test_deliver_rejects_unknown_preset(fake_env):
    payload = _call(
        "qing_deliver_setup_job",
        jobs=[{"target_dir": str(fake_env["tmp"] / "r2"), "preset": "不存在的预设"}],
    )
    assert payload["ok"] is False
    assert payload["detail"]["violations"][0]["code"] == "unknown_preset"


# ---------------------------------------------------------------------------
# 单元级：SRT 解析容错
# ---------------------------------------------------------------------------


def test_srt_parser_tolerates_messy_input():
    from dsh_davinci_qing.subtitle_srt import parse_srt_document

    messy = (
        "\ufeff1\r\n00:00:01.000 --> 00:00:02.500\r\n第一句\r\n\r\n"
        "00:00:02.000 --> 00:00:03.000\r\n缺序号且重叠\r\n\r\n"
        "3\r\n00:00:05,000 --> 00:00:04,000\r\n反向区间\r\n"
    )
    document = parse_srt_document(messy)
    assert len(document.cues) == 3
    assert any("重叠" in warning for warning in document.warnings)
    assert any("结束时间" in warning for warning in document.warnings)
    assert document.cues[2].end_seconds > document.cues[2].start_seconds


def test_srt_roundtrip(tmp_path: Path):
    from dsh_davinci_qing.subtitle_srt import Cue, dump_srt, parse_srt

    cues = [Cue(index=1, start_seconds=1.0, end_seconds=2.5, text="青青")]
    text = dump_srt(cues)
    assert "00:00:01,000 --> 00:00:02,500" in text
    assert parse_srt(text)[0].text == "青青"


# ---------------------------------------------------------------------------
# 独立运行入口
# ---------------------------------------------------------------------------


def main() -> int:
    raise SystemExit(pytest.main([__file__, "-v", "--tb=short"]))


if __name__ == "__main__":
    main()
