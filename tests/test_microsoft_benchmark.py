"""Safety and failure handling for the explicit research benchmark, offline."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image


def script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def benchmark():
    return script("benchmark_microsoft.py")


def test_preview_keeps_composition_orientation_and_original_bytes(benchmark):
    image = Image.new("RGB", (480, 320), "gray")
    image.paste("red", (0, 0, 32, 32))
    exif = Image.Exif()
    exif[274] = 6
    stream = io.BytesIO()
    image.save(stream, format="PNG", exif=exif)
    data = stream.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    preview, original_size, profile = benchmark.make_preview(data)
    assert original_size == (320, 480)
    assert preview.size == (171, 256)
    assert preview.getpixel((preview.width - 3, 3))[0] > 220
    assert profile and hashlib.sha256(data).hexdigest() == digest
    preview.close()


def test_preview_rejects_transparency_and_animation(benchmark):
    image = Image.new("RGBA", (100, 100), (255, 0, 0, 0))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    with pytest.raises(ValueError, match="Transparent"):
        benchmark.make_preview(stream.getvalue())
    stream = io.BytesIO()
    image.convert("RGB").save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (100, 100), "blue")],
    )
    with pytest.raises(ValueError, match="single-frame"):
        benchmark.make_preview(stream.getvalue())


def test_memory_admission_does_not_spawn(benchmark, monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark, "worker_budget_mib", lambda: 256)
    monkeypatch.setattr(benchmark, "worker_commit_budget_mib", lambda: 1024)

    def forbidden(*args, **kwargs):
        pytest.fail("Low-memory benchmark must not spawn a model worker")

    monkeypatch.setattr(benchmark.subprocess, "Popen", forbidden)
    result = benchmark.bounded_worker(tmp_path, "detect", 10)
    assert not result["ok"] and result["error"] == "RAM_ADMISSION"
    assert result["commit_limit_mib"] == 1024


def test_worker_is_contained_before_inference_and_killed_on_timeout(
    benchmark, monkeypatch, tmp_path
):
    events = []
    monkeypatch.setattr(benchmark, "worker_budget_mib", lambda: 1024)
    monkeypatch.setattr(benchmark, "worker_commit_budget_mib", lambda: 2048)

    class Containment:
        def __init__(self, budget):
            assert budget == 2048

        def assign(self, pid):
            events.append("assign")

        def close(self):
            events.append("close")

    class Process:
        pid = 123

        def __init__(self, *args, **kwargs):
            assert kwargs.get("shell", False) is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, data=None, timeout=None):
            if data:
                assert events == ["assign"] and data == "GO\n"
                events.append("start")
                raise benchmark.subprocess.TimeoutExpired("worker", timeout)
            events.append("reap")
            return "", ""

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(benchmark, "WorkerJob", Containment)
    monkeypatch.setattr(benchmark.subprocess, "Popen", Process)
    result = benchmark.bounded_worker(tmp_path, "quality", 1)
    assert not result["ok"] and result["error"] == "TIMEOUT"
    assert events == ["assign", "start", "kill", "reap", "close"]


def test_changed_source_is_rejected_before_execution(benchmark, monkeypatch, tmp_path):
    source = tmp_path / "source" / "Global" / "models" / "networks.py"
    source.parent.mkdir(parents=True)
    source.write_text("raise RuntimeError('must not execute')")
    monkeypatch.setattr(benchmark, "ASSETS", tmp_path)
    manifest = {"sources": {"Global/models/networks.py": "0" * 64}}
    with pytest.raises(ValueError, match="Source digest mismatch"):
        benchmark.upstream(manifest)


def test_gallery_escapes_private_filenames(benchmark, tmp_path):
    benchmark.gallery(
        tmp_path, {"cases": [{"id": "000", "source": '<script>alert("x")</script>'}]}
    )
    page = (tmp_path / "review.html").read_text(encoding="utf-8")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "Unavailable" in page


def test_remote_zip_rejects_full_body_response(monkeypatch):
    prepare = script("prepare_microsoft_benchmark.py")

    class IgnoredRange:
        status = 200
        headers: ClassVar = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            pytest.fail("Must reject full 2 GB body without reading")

    monkeypatch.setattr(
        prepare.urllib.request, "urlopen", lambda *a, **kw: IgnoredRange()
    )
    reader = prepare.RemoteZip()
    with pytest.raises(ValueError, match="16 MiB"):
        reader.read(17 * 1024**2)
    with pytest.raises(ValueError, match="honor"):
        reader.read(22)


def test_remote_zip_bounds_and_cache(monkeypatch):
    prepare = script("prepare_microsoft_benchmark.py")
    monkeypatch.setattr(prepare, "ARCHIVE_BYTES", 100)
    calls = []

    class RangeResponse(io.BytesIO):
        status = 206
        headers: ClassVar = {"Content-Range": "bytes 50-99/100"}

    def request(req, **kwargs):
        calls.append(req.headers["Range"])
        return RangeResponse(bytes(range(50, 100)))

    monkeypatch.setattr(prepare.urllib.request, "urlopen", request)
    reader = prepare.RemoteZip()
    reader.seek(-50, 2)
    assert reader.read(10) == bytes(range(50, 60))
    assert reader.read(10) == bytes(range(60, 70))
    assert reader.read(100) == bytes(range(70, 100))
    assert reader.read() == b""
    assert calls == ["bytes=50-99"]
    with pytest.raises(ValueError, match="seek"):
        reader.seek(-1)


def test_checkpoint_tamper_rejected_before_deserialization(
    benchmark, monkeypatch, tmp_path
):
    torch = pytest.importorskip("torch")
    path = tmp_path / "weights" / "tiny.pt"
    path.parent.mkdir()
    path.write_bytes(b"tampered")
    monkeypatch.setattr(benchmark, "ASSETS", tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Tampered checkpoint must not reach torch.load")

    monkeypatch.setattr(torch, "load", forbidden)
    manifest = {
        "weights": {"tiny": {"file": path.name, "bytes": 8, "sha256": "0" * 64}}
    }
    with pytest.raises(ValueError, match="digest mismatch"):
        benchmark.checkpoint(manifest, "tiny")
    assert json.dumps(benchmark.PATCHES)
