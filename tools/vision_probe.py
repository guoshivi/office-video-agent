"""一次性验证：EmbeddingGemma2 的视觉塔是否真的把画面编码进向量。

用工作台自带的私有 python 跑：
    python tools/vision_probe.py <model_dir> <workbench_root> <clip1> <clip2> ...
"""

import os
import sys
from pathlib import Path

import numpy as np

model_dir, root = sys.argv[1], sys.argv[2]
clips = [Path(p) for p in sys.argv[3:]]
sys.path.insert(0, root)

# torchcodec 解码视频要 ffmpeg 的共享库（avcodec-62.dll 等），
# 工作台自带的就在 tools_dir 里，先塞进 PATH 和 DLL 搜索目录。
tools_dir = Path(model_dir).parents[1] / "tools"
if tools_dir.is_dir():
    os.environ["PATH"] = f"{tools_dir}{os.pathsep}" + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(tools_dir))

from reference.core.videopipe.embedder import Embedder  # noqa: E402

LABELS = [
    "一个人在镜头前展示手里的产品",
    "有人在讲解护肤品的使用方法",
    "办公室前台、logo 墙和空置工位",
    "城市街道和建筑物外观",
]

embedder = Embedder(model_dir, mode="text_vision", preset="fast", device="cuda")
media = embedder.encode_media_many([{"video": [str(clip)]} for clip in clips])
texts = embedder.encode_texts(LABELS)

media = np.asarray(media, dtype=np.float32)
texts = np.asarray(texts, dtype=np.float32)
media /= np.linalg.norm(media, axis=1, keepdims=True)
texts /= np.linalg.norm(texts, axis=1, keepdims=True)
scores = media @ texts.T

print(f"向量维度 {media.shape[1]}　片段 {media.shape[0]} 个　描述 {texts.shape[0]} 句")
for i, clip in enumerate(clips):
    print(f"\n{clip.name}")
    for j, label in enumerate(LABELS):
        print(f"  {scores[i][j]:+.4f}  {label}")
    best = int(np.argmax(scores[i]))
    print(f"  → 最像：{LABELS[best]}")

print("\n片段之间相似度（越低说明模型能区分画面）：")
for i in range(len(clips)):
    for j in range(i + 1, len(clips)):
        print(f"  {clips[i].name} vs {clips[j].name}: {float(media[i] @ media[j]):+.4f}")
