"""C7.1 — iter_media_messages parsing of Meta webhook payloads."""

from __future__ import annotations

from whatsbot.domain.media import MediaKind
from whatsbot.http.meta_webhook import MediaMessage, iter_media_messages


def _wrap(message: dict[str, object]) -> dict[str, object]:
    """Build the standard Meta webhook envelope around a single message."""
    return {
        "entry": [
            {
                "changes": [
                    {"value": {"messages": [message]}}
                ]
            }
        ]
    }


def test_iter_media_messages_skips_text() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "abc",
            "type": "text",
            "text": {"body": "hi"},
        }
    )
    assert list(iter_media_messages(payload)) == []


def test_iter_media_messages_image() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "msg1",
            "type": "image",
            "image": {
                "id": "mid-1",
                "mime_type": "image/jpeg",
                "caption": "look at this",
                "sha256": "deadbeef",
            },
        }
    )
    messages = list(iter_media_messages(payload))
    assert len(messages) == 1
    assert messages[0] == MediaMessage(
        sender="+491",
        kind=MediaKind.IMAGE,
        msg_id="msg1",
        media_id="mid-1",
        mime="image/jpeg",
        caption="look at this",
    )


def test_iter_media_messages_image_without_caption() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "msg1",
            "type": "image",
            "image": {"id": "mid-1", "mime_type": "image/png"},
        }
    )
    [msg] = list(iter_media_messages(payload))
    assert msg.caption is None
    assert msg.media_id == "mid-1"
    assert msg.mime == "image/png"


def test_iter_media_messages_document_pdf() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "msg2",
            "type": "document",
            "document": {
                "id": "mid-2",
                "mime_type": "application/pdf",
                "filename": "report.pdf",
            },
        }
    )
    [msg] = list(iter_media_messages(payload))
    assert msg.kind is MediaKind.DOCUMENT
    assert msg.media_id == "mid-2"
    assert msg.mime == "application/pdf"


def test_iter_media_messages_audio_voice_note() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "msg3",
            "type": "audio",
            "audio": {
                "id": "mid-3",
                "mime_type": "audio/ogg; codecs=opus",
                "voice": True,
            },
        }
    )
    [msg] = list(iter_media_messages(payload))
    assert msg.kind is MediaKind.AUDIO
    assert msg.media_id == "mid-3"
    assert msg.mime and msg.mime.startswith("audio/ogg")


def test_iter_media_messages_video() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "msg4",
            "type": "video",
            "video": {"id": "mid-4", "mime_type": "video/mp4"},
        }
    )
    [msg] = list(iter_media_messages(payload))
    assert msg.kind is MediaKind.VIDEO
    assert msg.media_id == "mid-4"


def test_iter_media_messages_sticker() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "msg5",
            "type": "sticker",
            "sticker": {"id": "mid-5", "mime_type": "image/webp"},
        }
    )
    [msg] = list(iter_media_messages(payload))
    assert msg.kind is MediaKind.STICKER


def test_iter_media_messages_location_has_no_media_id() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "msg6",
            "type": "location",
            "location": {"latitude": 52.5, "longitude": 13.4},
        }
    )
    [msg] = list(iter_media_messages(payload))
    assert msg.kind is MediaKind.LOCATION
    assert msg.media_id is None


def test_iter_media_messages_contact_has_no_media_id() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "msg7",
            "type": "contacts",
            "contacts": [{"name": {"formatted_name": "Alice"}}],
        }
    )
    [msg] = list(iter_media_messages(payload))
    assert msg.kind is MediaKind.CONTACT
    assert msg.media_id is None


def test_iter_media_messages_unknown_type() -> None:
    payload = _wrap(
        {
            "from": "+491",
            "id": "msg8",
            "type": "reaction",
            "reaction": {"message_id": "abc", "emoji": "🔥"},
        }
    )
    [msg] = list(iter_media_messages(payload))
    assert msg.kind is MediaKind.UNKNOWN


def test_iter_media_messages_malformed_entry_skipped() -> None:
    payload: dict[str, object] = {"entry": "not-a-list"}
    assert list(iter_media_messages(payload)) == []


def test_iter_media_messages_missing_image_body() -> None:
    payload = _wrap(
        {"from": "+491", "id": "msg1", "type": "image"}  # no "image" sub-object
    )
    [msg] = list(iter_media_messages(payload))
    assert msg.media_id is None
    assert msg.mime is None


def test_iter_media_messages_handles_multiple_messages_in_change() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "+491",
                                    "id": "a",
                                    "type": "image",
                                    "image": {"id": "id-a", "mime_type": "image/jpeg"},
                                },
                                {
                                    "from": "+491",
                                    "id": "b",
                                    "type": "text",
                                    "text": {"body": "hi"},
                                },
                                {
                                    "from": "+491",
                                    "id": "c",
                                    "type": "sticker",
                                    "sticker": {"id": "id-c", "mime_type": "image/webp"},
                                },
                            ]
                        }
                    }
                ]
            }
        ]
    }
    kinds = [m.kind for m in iter_media_messages(payload)]
    assert kinds == [MediaKind.IMAGE, MediaKind.STICKER]


def test_iter_media_messages_empty_payload() -> None:
    assert list(iter_media_messages({})) == []
