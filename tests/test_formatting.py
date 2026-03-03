from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rufus_py.drives import formatting


def _setup_common_monkeypatch(monkeypatch) -> None:
    monkeypatch.setattr(formatting, "cluster", lambda: ("4096", "512", "8"))
    monkeypatch.setattr(formatting.fu, "find_usb", lambda: {"/media/testuser/USB": "USB"})
    monkeypatch.setattr(formatting.fu, "find_DN", lambda: "/dev/sdb1")


@pytest.mark.parametrize(
    ("fs_type", "expected_tool"),
    [
        (0, "mkfs.ntfs"),
        (1, "mkfs.vfat"),
        (2, "mkfs.exfat"),
        (3, "mkfs.ext4"),
    ],
)
def test_dskformat_runs_expected_mkfs_command(monkeypatch, fs_type: int, expected_tool: str) -> None:
    _setup_common_monkeypatch(monkeypatch)
    monkeypatch.setattr(formatting.states, "currentFS", fs_type)

    calls = []

    def fake_run(cmd, check=True):
        calls.append(cmd)

    monkeypatch.setattr(formatting.subprocess, "run", fake_run)

    formatting.dskformat()

    assert len(calls) == 1
    assert calls[0][0] == "pkexec"
    assert calls[0][1] == expected_tool


def test_dskformat_calls_unexpected_for_unknown_fs(monkeypatch) -> None:
    _setup_common_monkeypatch(monkeypatch)
    monkeypatch.setattr(formatting.states, "currentFS", 99)

    called = {"unexpected": False}

    def fake_unexpected():
        called["unexpected"] = True

    monkeypatch.setattr(formatting, "unexpected", fake_unexpected)
    monkeypatch.setattr(formatting.subprocess, "run", lambda *args, **kwargs: None)

    formatting.dskformat()

    assert called["unexpected"] is True
