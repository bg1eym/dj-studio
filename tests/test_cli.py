"""命令行入口：不带子命令 / serve / demo 的参数都能解析（直接运行 `djstudio` 曾经报 AttributeError）。"""
import pytest

from djstudio import cli


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("DJSTUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DJSTUDIO_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("DJSTUDIO_MIXXX_DIR", str(tmp_path / "mixxx"))
    calls = []
    monkeypatch.setattr(cli, "_serve", lambda studio, host, port, browser: calls.append((host, port, browser)))
    return calls


def test_bare_command_serves_with_defaults(served):
    assert cli.main([]) == 0
    assert served == [("127.0.0.1", 8430, True)]


def test_serve_options(served):
    assert cli.main(["serve", "--port", "9001", "--no-browser"]) == 0
    assert served == [("127.0.0.1", 9001, False)]
