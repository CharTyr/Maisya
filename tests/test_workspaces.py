import os

import pytest
from conftest import safe_symlink

from maisya.abilities import Profile
from maisya.threads import ThreadStore
from maisya.workspaces import Workspaces


def build(tmp_path, **limits):
    store = ThreadStore(tmp_path / "state.db")
    files = Workspaces(tmp_path / "workspaces", store, **limits)
    row = store.create(
        Profile(), "first", chat_id="chat", platform="qq", user_id="", workspace="trip"
    )
    return store, files, row


def test_named_workspace_reuses_context_and_is_scoped(tmp_path):
    store, files, row = build(tmp_path)
    same = store.create(
        Profile(), "next", chat_id="chat", platform="qq", user_id="", workspace="trip"
    )
    other = store.create(
        Profile(), "other", chat_id="other", platform="qq", user_id="", workspace="trip"
    )
    assert same["thread_id"] == row["thread_id"]
    assert other["workspace_id"] != row["workspace_id"]
    with files.activity(row) as work:
        work.write("project/journey.txt", "第一站")
        work.commit()
    with files.activity(same) as work:
        assert work.read("project/journey.txt")["text"] == "第一站"
    with files.activity(other) as work, pytest.raises(FileNotFoundError):
        work.read("project/journey.txt")


def test_private_workspace_cannot_be_republished_by_profile_access_edit(tmp_path):
    store = ThreadStore(tmp_path / "state.db")
    private = store.create(
        Profile(access="admins"),
        "private",
        chat_id="chat",
        platform="qq",
        user_id="Alice",
        workspace="private",
    )
    store.stop(private["thread_id"], "chat")
    with pytest.raises(PermissionError, match="工作区"):
        store.create(
            Profile(),
            "public",
            chat_id="chat",
            platform="qq",
            user_id="",
            workspace="private",
        )


def test_paths_links_special_files_and_oversize_are_rejected(tmp_path):
    _store, files, row = build(tmp_path, max_bytes=64, max_files=4)
    secret = tmp_path / "outside"
    secret.write_text("outside", encoding="utf-8")
    with files.activity(row) as work:
        for path in (
            "../outside",
            "/etc/passwd",
            "project/../../outside",
            "in/write.txt",
        ):
            with pytest.raises((ValueError, PermissionError)):
                work.write(path, "bad")
        safe_symlink(work.path / "project/link", secret)
        with pytest.raises((ValueError, OSError)):
            work.read("project/link")
        with pytest.raises(ValueError):
            work.commit()
        (work.path / "project/link").unlink()
        work.write("project/a", "x" * 32)
        with pytest.raises(ValueError):
            work.write("project/b", "x" * 64)
        work.commit()
        os.link(work.path / "project/a", work.path / "project/hard")
        with pytest.raises(ValueError):
            work.commit()
    assert secret.read_text(encoding="utf-8") == "outside"


def test_checkpoint_rollback_and_immutable_attachments(tmp_path):
    store, files, row = build(tmp_path)
    with files.activity(row) as work:
        work.write("project/state", "saved")
        work.write("out/report.txt", "original")
        work.commit()
        attachment = files.attach(row, work, ["out/report.txt"])[0]
        work.write("out/report.txt", "modified")
        assert files.resolve(attachment["handle"], "chat").read_text(encoding="utf-8") == "original"
        with pytest.raises(PermissionError):
            files.resolve(attachment["handle"], "other")
        work.write("project/state", "interrupted")
    with files.activity(row) as work:
        assert work.read("project/state")["text"] == "saved"
    store.stop(row["thread_id"], "chat")
    next_row = store.create(
        Profile(),
        "continue",
        chat_id="chat",
        platform="qq",
        user_id="",
        workspace="trip",
    )
    assert next_row["thread_id"] != row["thread_id"]
    assert next_row["workspace_id"] == row["workspace_id"]


def test_held_workspace_does_not_admit_another_writer(tmp_path):
    _store, files, row = build(tmp_path)
    with files.activity(row), pytest.raises(RuntimeError), files.activity(row):
        pass


def test_attachment_rejects_known_credentials_before_persistence(tmp_path):
    _store, files, row = build(
        tmp_path, contains_secret=lambda data: b"credential-test-value" in data
    )
    with files.activity(row) as work:
        work.write("out/report.txt", "credential-test-value")
        with pytest.raises(ValueError, match="凭据"):
            files.attach(row, work, ["out/report.txt"])
    assert not list(files.artifacts.iterdir())


def test_crashed_checkpoint_staging_is_reclaimed_on_next_activity(tmp_path):
    _store, files, row = build(tmp_path)
    with files.activity(row) as work:
        work.write("project/value", "committed")
        work.commit()
        durable = work.durable
    abandoned = durable / ("v_" + "0" * 32)
    abandoned.mkdir()
    (abandoned / "partial").write_text("interrupted copy", encoding="utf-8")
    with files.activity(row) as work:
        assert work.read("project/value")["text"] == "committed"
        assert not abandoned.exists()
