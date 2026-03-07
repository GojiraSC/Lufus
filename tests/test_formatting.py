from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rufus_py.drives import formatting
from rufus_py.drives import states as st


def _setup_common_monkeypatch(monkeypatch) -> None:
    monkeypatch.setattr(formatting.fu, "find_usb", lambda: {"/media/testuser/USB": "USB"})
    monkeypatch.setattr(formatting.fu, "find_DN", lambda: "/dev/sdb1")
    monkeypatch.setattr(formatting.states, "DN", "/dev/sdb1")


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
    monkeypatch.setattr(formatting.states, "cluster_size", 0)
    monkeypatch.setattr(formatting.states, "partition_scheme", 0)

    calls = []

    def fake_run(cmd, check=True, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr(formatting.subprocess, "run", fake_run)

    formatting.dskformat()

    # Find the mkfs call (partition scheme parted calls come first)
    mkfs_calls = [c for c in calls if c and c[0].startswith("mkfs")]
    assert len(mkfs_calls) == 1, f"Expected 1 mkfs call, got: {calls}"
    assert mkfs_calls[0][0] == expected_tool


def test_dskformat_calls_unexpected_for_unknown_fs(monkeypatch) -> None:
    _setup_common_monkeypatch(monkeypatch)
    monkeypatch.setattr(formatting.states, "currentFS", 99)
    monkeypatch.setattr(formatting.states, "cluster_size", 0)
    monkeypatch.setattr(formatting.states, "partition_scheme", 0)

    called = {"unexpected": False}

    def fake_unexpected():
        called["unexpected"] = True

    monkeypatch.setattr(formatting, "unexpected", fake_unexpected)
    monkeypatch.setattr(formatting.subprocess, "run", lambda *args, **kwargs: None)

    formatting.dskformat()

    assert called["unexpected"] is True


def test_cluster_returns_tuple_even_without_usb(monkeypatch) -> None:
    """cluster() must never crash — it must always return a valid 3-tuple."""
    monkeypatch.setattr(formatting.fu, "find_usb", lambda: {})
    monkeypatch.setattr(formatting.fu, "find_DN", lambda: None)
    monkeypatch.setattr(formatting.states, "DN", "")

    result = formatting.cluster()
    assert isinstance(result, tuple)
    assert len(result) == 3
    cluster1, cluster2, sector = result
    assert cluster1 > 0
    assert cluster2 > 0
    assert sector == cluster1 // cluster2


def test_cluster_respects_cluster_size_state(monkeypatch) -> None:
    monkeypatch.setattr(formatting.fu, "find_usb", lambda: {"/media/testuser/USB": "USB"})
    monkeypatch.setattr(formatting.fu, "find_DN", lambda: "/dev/sdb1")
    monkeypatch.setattr(formatting.states, "DN", "/dev/sdb1")

    monkeypatch.setattr(formatting.states, "cluster_size", 0)
    c1, _, _ = formatting.cluster()
    assert c1 == 4096

    monkeypatch.setattr(formatting.states, "cluster_size", 1)
    c1, _, _ = formatting.cluster()
    assert c1 == 8192


def test_apply_partition_scheme_gpt(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(formatting.subprocess, "run", lambda cmd, check=True, **kw: calls.append(cmd))
    monkeypatch.setattr(formatting.states, "partition_scheme", 0)

    formatting._apply_partition_scheme("/dev/sdb1")

    assert any("gpt" in c for c in calls)


def test_apply_partition_scheme_mbr(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(formatting.subprocess, "run", lambda cmd, check=True, **kw: calls.append(cmd))
    monkeypatch.setattr(formatting.states, "partition_scheme", 1)

    formatting._apply_partition_scheme("/dev/sdb1")

    assert any("msdos" in c for c in calls)


def test_checkdevicebadblock_returns_false_when_no_drive(monkeypatch) -> None:
    monkeypatch.setattr(formatting.fu, "find_usb", lambda: {})
    monkeypatch.setattr(formatting.fu, "find_DN", lambda: None)
    monkeypatch.setattr(formatting.states, "DN", "")

    result = formatting.checkdevicebadblock()
    assert result is False


def test_volumecustomlabel_no_drive_does_not_crash(monkeypatch) -> None:
    """volumecustomlabel() should gracefully handle missing drive node."""
    monkeypatch.setattr(formatting.fu, "find_usb", lambda: {})
    monkeypatch.setattr(formatting.fu, "find_DN", lambda: None)
    monkeypatch.setattr(formatting.states, "DN", "")
    monkeypatch.setattr(formatting.states, "currentFS", 0)
    monkeypatch.setattr(formatting.states, "new_label", "TESTLABEL")

    # Should not raise
    formatting.volumecustomlabel()
