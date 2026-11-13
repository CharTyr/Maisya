import base64
import json

import pytest
from test_foundation import loaded

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.mark.asyncio
async def test_outputs_and_artifact_read_cover_attachments_workspace_out_conclusions_and_redaction(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    try:
        plugin.vault.set("api_secret", "super-secret-key-999")
        profile = plugin.profiles.get("scout")
        row = plugin.threads.create(
            profile,
            "整理今日简报并生成卡片",
            chat_id="chat",
            platform="qq",
            user_id="qq:Alice",
            workspace="daily_digest",
        )
        ws_id = row["workspace_id"]
        with plugin.runtime.files.activity(row) as work:
            work.write(
                "out/report.html",
                "<h1>今日简报</h1><p>凭证：super-secret-key-999</p><script>window.bad=1</script>",
            )
            (work.path / "out/card.png").write_bytes(PNG_1X1)
            (work.path / "out/leaked.bin").write_bytes(b"\x00\x01super-secret-key-999\x02")
            work.write("project/build.py", 'print("ok")\n')
            work.commit()
            attached = plugin.runtime.files.attach(row, work, ["out/card.png"])

        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='waiting', result=?, completed_revision=1 WHERE thread_id=?",
                (
                    json.dumps(
                        {
                            "text": "已完成简报整理，详见 https://example.org/digest",
                            "attachments": attached,
                        },
                        ensure_ascii=False,
                    ),
                    row["thread_id"],
                ),
            )

        res = await plugin.manage_read("outputs", "{}")
        assert res["success"] is True
        items = res["items"]
        assert any(
            i["kind"] == "attachment" and i["name"] == "card.png" for i in items
        )
        assert any(
            i["kind"] == "workspace_out" and i.get("path") == "out/report.html"
            for i in items
        )
        assert any(
            i["kind"] == "workspace_out" and i.get("path") == "out/leaked.bin"
            for i in items
        )
        assert not any(
            i["kind"] == "workspace_out" and i.get("path") == "out/card.png"
            for i in items
        )
        assert any(
            i["kind"] == "conclusion" and i.get("links_count") == 1 for i in items
        )

        ws = next(w for w in res["workspaces"] if w["name"] == "daily_digest")
        assert ws["file_count"] == 4
        assert ws["out_count"] == 3
        assert res["stats"]["total"] >= 4
        assert "chat_sources" in res

        only_att = await plugin.manage_read(
            "outputs", json.dumps({"kind": "attachment"})
        )
        assert only_att["items"] and all(
            i["kind"] == "attachment" for i in only_att["items"]
        )

        by_query = await plugin.manage_read("outputs", json.dumps({"query": "report"}))
        assert len(by_query["items"]) == 1
        assert by_query["items"][0]["path"] == "out/report.html"

        empty_chat = await plugin.manage_read(
            "outputs", json.dumps({"chat_id": "nonexistent"})
        )
        assert empty_chat["items"] == []

        art_img = await plugin.manage_read(
            "artifact",
            json.dumps({"kind": "attachment", "handle": attached[0]["handle"]}),
        )
        assert art_img["success"] is True
        assert art_img["media_kind"] == "image"
        assert art_img["mime"] == "image/png"
        assert base64.b64decode(art_img["base64"]) == PNG_1X1
        assert art_img["next_offset"] is None

        art_html = await plugin.manage_read(
            "artifact",
            json.dumps(
                {
                    "kind": "workspace_file",
                    "workspace_id": ws_id,
                    "path": "out/report.html",
                }
            ),
        )
        assert art_html["success"] is True
        assert art_html["media_kind"] == "html"
        assert "super-secret-key-999" not in art_html["text"]
        assert "REDACTED" in art_html["text"]

        art_bin = await plugin.manage_read(
            "artifact",
            json.dumps(
                {
                    "kind": "workspace_file",
                    "workspace_id": ws_id,
                    "path": "out/leaked.bin",
                }
            ),
        )
        assert art_bin["success"] is False

        art_traversal = await plugin.manage_read(
            "artifact",
            json.dumps(
                {
                    "kind": "workspace_file",
                    "workspace_id": ws_id,
                    "path": "../current.json",
                }
            ),
        )
        assert art_traversal["success"] is False

        art_con = await plugin.manage_read(
            "artifact",
            json.dumps({"kind": "conclusion", "thread_id": row["thread_id"]}),
        )
        assert art_con["success"] is True
        assert art_con["links"] == ["https://example.org/digest"]
        assert len(art_con["attachments"]) == 1
    finally:
        await plugin.on_unload()
