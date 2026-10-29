"""启动时自动更新（git pull，拉到新代码就重启）和关掉还开着的旧网页服务。"""
import os
import socket
import subprocess
import time

import pytest

from djstudio import __version__, selfupdate

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV)


def commit(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", name)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    up = tmp_path / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    commit(up, "pyproject.toml", "[project]\nname = 'x'\n")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(up), str(clone))
    monkeypatch.setattr(selfupdate, "ROOT", clone)
    monkeypatch.delenv("DJSTUDIO_NO_UPDATE")
    calls = {"exec": [], "pip": []}
    monkeypatch.setattr(selfupdate.os, "execv", lambda exe, args: calls["exec"].append(args[1:]))
    real_run = subprocess.run

    def run(cmd, *a, **k):
        if "pip" in cmd:
            calls["pip"].append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(selfupdate.subprocess, "run", run)
    return up, clone, calls


def test_up_to_date_starts_normally(repo):
    up, clone, calls = repo
    selfupdate.self_update(["serve"])
    assert calls == {"exec": [], "pip": []}


def test_new_commit_pulls_and_restarts(repo):
    up, clone, calls = repo
    commit(up, "app.txt", "新界面")
    selfupdate.self_update(["serve", "--no-browser"])
    assert (clone / "app.txt").read_text() == "新界面"
    assert calls["exec"] == [["-m", "djstudio", "serve", "--no-browser"]] and calls["pip"] == []
    assert os.environ["DJSTUDIO_NO_UPDATE"] == "1"  # 重启后的那次不再拉


def test_dependency_change_reinstalls(repo):
    up, clone, calls = repo
    commit(up, "pyproject.toml", "[project]\nname = 'x'\ndependencies = ['pypdf']\n")
    selfupdate.self_update([])
    assert len(calls["pip"]) == 1 and str(clone) in calls["pip"][0] and calls["exec"] == [["-m", "djstudio"]]


def test_pull_failure_keeps_current_version(repo, capsys):
    up, clone, calls = repo
    git(clone, "remote", "set-url", "origin", str(up.parent / "nowhere"))
    selfupdate.self_update([])
    assert calls["exec"] == [] and "没能自动更新" in capsys.readouterr().out


def test_disabled_or_not_a_checkout(repo, monkeypatch, tmp_path):
    up, clone, calls = repo
    commit(up, "app.txt", "x")
    monkeypatch.setenv("DJSTUDIO_NO_UPDATE", "1")
    selfupdate.self_update([])
    monkeypatch.delenv("DJSTUDIO_NO_UPDATE")
    monkeypatch.setattr(selfupdate, "ROOT", tmp_path / "pip-installed")
    selfupdate.self_update([])
    assert calls["exec"] == [] and not (clone / "app.txt").exists()


def test_version_text():
    assert selfupdate.version_text().startswith(__version__)


def test_free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert selfupdate.free_port("127.0.0.1", port)  # 没人占
    with socket.socket() as s:  # 被别的程序（这里是测试进程自己）占着：不动它，报错
        s.bind(("127.0.0.1", port))
        s.listen()
        assert not selfupdate.free_port("127.0.0.1", port)


def test_free_port_stops_old_djstudio(tmp_path):
    """之前开着的 DJ Studio（命令行里有 djstudio）占着端口：关掉它再启动。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    code = f"import socket,time; s=socket.socket(); s.bind(('127.0.0.1',{port})); s.listen(); time.sleep(60)"
    old = subprocess.Popen([os.sys.executable, "-c", code, "djstudio-old-version"])
    try:
        for _ in range(50):
            if selfupdate._listening("127.0.0.1", port):
                break
            time.sleep(0.1)
        assert selfupdate.free_port("127.0.0.1", port)
        assert old.wait(5) is not None
    finally:
        old.kill()
