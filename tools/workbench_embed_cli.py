"""One-shot EmbeddingGemma2 bridge for the local video workbench.

Run this file with the workbench's private Python, not the application venv.
It loads the model, embeds the requested texts, releases CUDA memory and exits.
The same GPU byte lock used by the workbench is acquired first, so this bridge
cannot load alongside a render or index job.
"""

import gc
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def gpu_guard(lock_path: str):
    if not lock_path:
        yield
        return
    path = Path(lock_path)
    if not path.is_file() or path.stat().st_size != 1:
        raise RuntimeError("GPU 锁文件不存在或大小不是 1 字节")
    handle = path.open("r+b")
    try:
        if os.name == "nt":
            import msvcrt

            for attempt in range(20):
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if attempt == 19:
                        raise RuntimeError("显卡正忙：视频工作台正在索引或出片，请稍后再试")
                    time.sleep(3)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: workbench_embed_cli.py <input.json> <output.json>", file=sys.stderr)
        return 2
    input_path, output_path = Path(sys.argv[1]), Path(sys.argv[2])
    request = json.loads(input_path.read_text(encoding="utf-8"))
    texts = request.get("texts") or []
    if not texts:
        raise ValueError("texts 不能为空")
    workbench_root = Path(request["workbench_root"]).resolve()
    model_dir = Path(request["model_dir"]).resolve()
    sys.path.insert(0, str(workbench_root / "reference" / "core"))

    result = {"vectors": [], "model": request.get("model") or model_dir.name}
    embedder = None
    torch = None
    try:
        with gpu_guard(request.get("gpu_lock_path", "")):
            from videopipe.embedder import Embedder

            embedder = Embedder(str(model_dir), mode="text", preset="fast", device="cuda")
            vectors = embedder.encode_texts(
                texts,
                prompt_name=request.get("prompt_name") or "SearchQuery",
                batch_size=int(request.get("batch_size") or 16),
            )
            result["vectors"] = vectors.tolist()
    finally:
        try:
            import torch as torch_module

            torch = torch_module
        except ImportError:
            torch = None
        embedder = None
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()

    temporary = output_path.with_suffix(output_path.suffix + ".part")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(output_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        output_arg = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("embed-error.json")
        output_arg.write_text(
            json.dumps({"error": str(error)}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(str(error), file=sys.stderr)
        raise
