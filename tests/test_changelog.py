"""更新记录（docs/CHANGELOG.md）：当前版本必须有一段（CI 发布时把这一段写进 Release 说明），版本从新到旧排。"""
import re
from pathlib import Path

from djstudio import __version__

TEXT = (Path(__file__).resolve().parents[1] / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")


def test_current_version_has_an_entry():
    m = re.search(r"^## " + re.escape(__version__) + r" — \d{4}-\d{2}-\d{2}", TEXT, re.M)
    assert m, f"docs/CHANGELOG.md 里没有 {__version__} 这一版：发布前写一段（中文，写用户能感觉到的变化）"
    body = TEXT[m.end():].split("\n## ", 1)[0]
    assert body.strip().startswith("-") or "\n- " in body, "这一版的说明是空的"


def test_versions_newest_first():
    vers = [tuple(int(x) for x in v.split(".")) for v in re.findall(r"^## (\d+\.\d+\.\d+) — ", TEXT, re.M)]
    assert vers == sorted(vers, reverse=True) and len(vers) == len(set(vers)), vers
