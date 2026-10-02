"""Deploying a conversation fix must invalidate a cached browser script."""

import os

from src.dashboard.assets import asset_version


def test_script_change_with_preserved_timestamps_invalidates_assets(tmp_path):
    script = tmp_path / "conversation.js"
    script.write_text("old logic", encoding="utf-8")
    stamp = script.stat().st_mtime
    old = asset_version(tmp_path)
    script.write_text("new logic", encoding="utf-8")
    os.utime(script, (stamp, stamp))
    assert asset_version(tmp_path) != old
    assert asset_version(tmp_path) == asset_version(tmp_path)


def test_style_addition_and_removal_invalidates_assets(tmp_path):
    old = asset_version(tmp_path)
    style = tmp_path / "workspace.css"
    style.write_text("body {color: black}", encoding="utf-8")
    assert asset_version(tmp_path) != old
    style.unlink()
    assert asset_version(tmp_path) == old
