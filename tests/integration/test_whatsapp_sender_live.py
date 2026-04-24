"""C10.4 — Live WhatsAppCloudSender test against the real Meta Graph API.

Skipped by default. Opt-in via ``WHATSBOT_LIVE_META=1`` so ``make test``
and ``make smoke`` never reach out to the network.

Manual invocation:

    WHATSBOT_LIVE_META=1 \
    WHATSBOT_LIVE_META_TOKEN=EAAxxxxxxxxxx \
    WHATSBOT_LIVE_META_PHONE_NUMBER_ID=1234567890 \
    WHATSBOT_LIVE_META_TO=491716598519 \
    venv/bin/pytest tests/integration/test_whatsapp_sender_live.py -v -s

The test sends **one** text message to the configured recipient. The
assertion is limited to "no exception raised" — the operator manually
verifies that the message actually arrived on the phone.

Env vars:

- ``WHATSBOT_LIVE_META``: must be truthy (``1``, ``true``, ``yes``) for
  the test to run. Any other value → skip.
- ``WHATSBOT_LIVE_META_TOKEN``: permanent Meta access token (System User).
- ``WHATSBOT_LIVE_META_PHONE_NUMBER_ID``: Meta phone-number-id from the
  WhatsApp Business app.
- ``WHATSBOT_LIVE_META_TO``: recipient phone number in E.164 digits
  (no ``+`` — the adapter strips it either way).
"""

from __future__ import annotations

import datetime as _dt
import os

import pytest

from whatsbot.adapters.resilience import _reset_registry_for_tests
from whatsbot.adapters.whatsapp_sender import WhatsAppCloudSender

_LIVE_FLAG = os.environ.get("WHATSBOT_LIVE_META", "").strip().lower()
_LIVE_ENABLED = _LIVE_FLAG in {"1", "true", "yes"}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _LIVE_ENABLED,
        reason="WHATSBOT_LIVE_META not set — live Meta test skipped by default",
    ),
]


def test_live_send_to_configured_recipient() -> None:
    token = os.environ.get("WHATSBOT_LIVE_META_TOKEN", "").strip()
    phone_number_id = os.environ.get("WHATSBOT_LIVE_META_PHONE_NUMBER_ID", "").strip()
    to = os.environ.get("WHATSBOT_LIVE_META_TO", "").strip()

    if not token or not phone_number_id or not to:
        pytest.fail(
            "Live test requires WHATSBOT_LIVE_META_TOKEN, "
            "WHATSBOT_LIVE_META_PHONE_NUMBER_ID, WHATSBOT_LIVE_META_TO"
        )

    # Avoid picking up a tripped breaker from an earlier session.
    _reset_registry_for_tests()

    sender = WhatsAppCloudSender(
        access_token=token,
        phone_number_id=phone_number_id,
    )
    now = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    sender.send_text(
        to=to,
        body=f"whatsbot phase-10 live test · {now}",
    )
    # Manual check: message arrives on the phone within a few seconds.
