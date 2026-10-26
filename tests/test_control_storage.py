import json

import pytest

from revive import runtime


@pytest.mark.parametrize("persistent", [False, True])
def test_windows_reader_sharing_conflict_has_bounded_retry(
    tmp_path, monkeypatch, persistent
):
    path = tmp_path / "monitor.json"
    runtime.write_json(tmp_path, path, {"version": 1})
    replace = runtime.os.replace
    calls = []
    sleeps = []

    def busy(source, destination):
        calls.append(destination)
        if persistent or len(calls) < 3:
            error = PermissionError("Windows reader holds destination")
            error.winerror = 5
            raise error
        return replace(source, destination)

    monkeypatch.setattr(runtime.os, "replace", busy)
    monkeypatch.setattr(runtime.time, "sleep", sleeps.append)
    if persistent:
        with pytest.raises(PermissionError):
            runtime.write_json(tmp_path, path, {"version": 2})
        assert len(calls) == 6 and len(sleeps) == 5
        assert json.loads(path.read_text()) == {"version": 1}
    else:
        runtime.write_json(tmp_path, path, {"version": 2})
        assert len(calls) == 3 and len(sleeps) == 2
        assert json.loads(path.read_text()) == {"version": 2}
    assert list(tmp_path.iterdir()) == [path]
