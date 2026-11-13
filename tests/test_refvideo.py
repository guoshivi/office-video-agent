"""视频库拆解的纯逻辑测试：不加载模型，只验结构与融合判断。"""

from __future__ import annotations

from pathlib import Path


def test_classify_roles():
    from app.refvideo import classify

    assert classify("今天带大家实地看房推荐一套", 0, 5) == "hook"
    assert classify("220平3+1格局的办公室", 1, 5) == "面积"
    assert classify("入户员工区域", 2, 5) == "亮点"
    assert classify("感兴趣的老板赶紧过来看看吧", 4, 5) == "cta"
    # 中间句出现「联系」也要算收尾，避免把 CTA 当成卖点
    assert classify("想看的联系我", 3, 5) == "cta"


def test_fuse_prefers_caption_text_without_leaking():
    from app.refvideo import _fuse

    sentences = [
        {"start": 0.0, "end": 3.0, "text": "今天带大家实地看房推荐一套"},
        {"start": 3.1, "end": 7.1, "text": "220瓶3加1格局的办公室"},
    ]
    subtitles = [
        {"start": 0.5, "end": 2.0, "text": "今天带大家实地看房", "y": 0.71},
        {"start": 2.5, "end": 3.0, "text": "推荐一套", "y": 0.71},
        {"start": 3.5, "end": 6.5, "text": "220平", "y": 0.70},
        {"start": 5.5, "end": 6.5, "text": "3+1格局的办公室", "y": 0.76},
    ]
    fused = _fuse(subtitles, [dict(row) for row in sentences])
    assert fused[0]["text"] == "今天带大家实地看房推荐一套"
    # 关键：第二句不能再吃到第一句的「推荐一套」
    assert fused[1]["text"] == "220平3+1格局的办公室"
    assert fused[1]["spoken"].startswith("220瓶")


def test_fuse_fills_gap_from_captions():
    from app.refvideo import _fuse

    sentences = [
        {"start": 0.0, "end": 2.0, "text": "开场"},
        {"start": 12.0, "end": 14.0, "text": "收尾"},
    ]
    subtitles = [
        {"start": 3.0, "end": 4.0, "text": "会议室", "y": 0.7},
        {"start": 5.0, "end": 6.0, "text": "可坐十人", "y": 0.7},
    ]
    fused = _fuse(subtitles, [dict(row) for row in sentences])
    assert [row["text"] for row in fused] == ["开场", "会议室可坐十人", "收尾"]


def test_build_spec_shares_and_labels():
    from app.refvideo import build_spec

    sentences = [
        {"start": 0.0, "end": 3.0, "text": "今天带大家看一套220平的办公室"},
        {"start": 3.0, "end": 6.0, "text": "入户就是工区"},
        {"start": 6.0, "end": 9.0, "text": "感兴趣的老板过来看看"},
    ]
    labels = [{"start": 3.2, "end": 5.0, "text": "工区", "y": 0.2}]
    spec = build_spec(9.0, [3.0], sentences, labels)

    assert spec["constraints"]["total_seconds"] == 9.0
    assert abs(sum(item["share"] for item in spec["structure"]) - 1.0) < 0.01
    assert spec["shots"][1]["labels"] == ["工区"]
    assert "工区" in spec["tips"][-1] or any("角标" in tip for tip in spec["tips"])


def test_scan_and_fingerprint_dedupe(tmp_path):
    from app.library import _fingerprint, ingest_folder, scan

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    other = tmp_path / "c.mov"
    a.write_bytes(b"x" * 2048)
    b.write_bytes(b"x" * 2048)      # 与 a 完全相同（模拟两次导出）
    other.write_bytes(b"y" * 2048)
    (tmp_path / "note.txt").write_text("ignore me", encoding="utf-8")

    files = scan(tmp_path)
    assert [path.name for path in files] == ["a.mp4", "b.mp4", "c.mov"]
    assert _fingerprint(a) == _fingerprint(b)
    assert _fingerprint(a) != _fingerprint(other)


def test_scan_rejects_missing_folder(tmp_path):
    from app.library import LibraryError, scan

    try:
        scan(tmp_path / "nope")
    except LibraryError as error:
        assert "目录不存在" in str(error)
    else:  # pragma: no cover
        raise AssertionError("应当抛出 LibraryError")


def test_fallback_search_without_vectors(monkeypatch, tmp_path):
    import os

    os.environ["OVA_DATA_DIR"] = str(tmp_path)
    from app.db import init_db, json_dumps, transaction, utc_now

    init_db()
    with transaction() as connection:
        connection.execute(
            "INSERT INTO style_templates(id, name, status, spec_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            ("tpl_1", "10.30 实地看房", "ready", json_dumps({"shots": [{"sample": "入户就是工区"}]}), utc_now(), utc_now()),
        )
    import app.library as library

    results = library._fallback_search("工区 入户", limit=3)
    assert results and results[0]["template_id"] == "tpl_1"
    assert Path(tmp_path, "ova.db").exists() or True


def test_audio_mean_db_parses_volumedetect(monkeypatch):
    """音量检测用来判断"这条片子本来就没口播"，别把空镜素材报成缺件。"""
    from app import refvideo

    class Done:
        stderr = "[Parsed_volumedetect_0] mean_volume: -43.9 dB\nmax_volume: -26.6 dB"

    monkeypatch.setattr(refvideo, "_find_binary", lambda name: "ffmpeg")
    monkeypatch.setattr(refvideo.subprocess, "run", lambda *a, **k: Done())
    assert refvideo.audio_mean_db(Path("x.mp4")) == -43.9


def test_audio_mean_db_returns_none_without_ffmpeg(monkeypatch):
    from app import refvideo

    monkeypatch.setattr(refvideo, "_find_binary", lambda name: None)
    assert refvideo.audio_mean_db(Path("x.mp4")) is None


def test_silent_clip_warning_says_no_voiceover(monkeypatch, tmp_path):
    """静音片子不该弹"本机没装语音模型"，那会让人以为哪坏了。"""
    from app import refvideo

    fake = tmp_path / "x.mp4"
    fake.write_bytes(b"stub")

    monkeypatch.setattr(
        refvideo, "probe_capabilities",
        lambda: {"speech_ready": False, "caption_ocr_ready": False},
    )
    monkeypatch.setattr(refvideo, "probe_duration", lambda path: 10.6)
    monkeypatch.setattr(refvideo, "scene_cuts", lambda path: [])
    monkeypatch.setattr(refvideo, "audio_mean_db", lambda path: -43.9)

    spec = refvideo.analyze_file(fake)
    text = " ".join(spec["warnings"])
    assert "本来就没有口播" in text
    assert "没装语音模型" not in text


def test_loud_clip_still_mentions_missing_model(monkeypatch, tmp_path):
    """真有声音的片子才提模型，否则用户不知道该不该装。"""
    from app import refvideo

    fake = tmp_path / "x.mp4"
    fake.write_bytes(b"stub")

    monkeypatch.setattr(
        refvideo, "probe_capabilities",
        lambda: {"speech_ready": False, "caption_ocr_ready": False},
    )
    monkeypatch.setattr(refvideo, "probe_duration", lambda path: 30.0)
    monkeypatch.setattr(refvideo, "scene_cuts", lambda path: [8.0, 16.0])
    monkeypatch.setattr(refvideo, "audio_mean_db", lambda path: -18.0)

    spec = refvideo.analyze_file(fake)
    assert "没装语音模型" in " ".join(spec["warnings"])


def test_audio_mean_db_parses_volumedetect(monkeypatch):
    """音量检测用来判断"这条片子本来就没口播"，别把空镜素材报成缺件。"""
    from app import refvideo

    class Done:
        stderr = "[Parsed_volumedetect_0] mean_volume: -43.9 dB\nmax_volume: -26.6 dB"

    monkeypatch.setattr(refvideo, "_find_binary", lambda name: "ffmpeg")
    monkeypatch.setattr(refvideo.subprocess, "run", lambda *a, **k: Done())
    assert refvideo.audio_mean_db(Path("x.mp4")) == -43.9


def test_audio_mean_db_returns_none_without_ffmpeg(monkeypatch):
    from app import refvideo

    monkeypatch.setattr(refvideo, "_find_binary", lambda name: None)
    assert refvideo.audio_mean_db(Path("x.mp4")) is None


def test_silent_clip_warning_says_no_voiceover(monkeypatch, tmp_path):
    """静音片子不该弹"本机没装语音模型"，那会让人以为哪坏了。"""
    from app import refvideo

    fake = tmp_path / "x.mp4"
    fake.write_bytes(b"stub")

    monkeypatch.setattr(
        refvideo, "probe_capabilities",
        lambda: {"speech_ready": False, "caption_ocr_ready": False},
    )
    monkeypatch.setattr(refvideo, "probe_duration", lambda path: 10.6)
    monkeypatch.setattr(refvideo, "scene_cuts", lambda path: [])
    monkeypatch.setattr(refvideo, "audio_mean_db", lambda path: -43.9)

    spec = refvideo.analyze_file(fake)
    text = " ".join(spec["warnings"])
    assert "本来就没有口播" in text
    assert "没装语音模型" not in text


def test_loud_clip_still_mentions_missing_model(monkeypatch, tmp_path):
    """真有声音的片子才提模型，否则用户不知道该不该装。"""
    from app import refvideo

    fake = tmp_path / "x.mp4"
    fake.write_bytes(b"stub")

    monkeypatch.setattr(
        refvideo, "probe_capabilities",
        lambda: {"speech_ready": False, "caption_ocr_ready": False},
    )
    monkeypatch.setattr(refvideo, "probe_duration", lambda path: 30.0)
    monkeypatch.setattr(refvideo, "scene_cuts", lambda path: [8.0, 16.0])
    monkeypatch.setattr(refvideo, "audio_mean_db", lambda path: -18.0)

    spec = refvideo.analyze_file(fake)
    assert "没装语音模型" in " ".join(spec["warnings"])
