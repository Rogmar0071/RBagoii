"""
Shared test utilities for backend tests.
"""

from __future__ import annotations

import uuid


def _chat_payload(message: str = "test", **overrides) -> dict:
    """Build a valid POST /api/chat request body.

    CONVERSATION_LIFECYCLE_ENFORCEMENT_LOCK: conversation_id is required on
    every request.  If not supplied via ``overrides``, a fresh UUID4 is
    generated so tests that don't care about conversation identity still pass
    validation without needing to wire up a real conversation first.

    Usage::

        # Minimal — auto-generates conversation_id
        body = _chat_payload("Hello")

        # Specific conversation
        body = _chat_payload("Hello", conversation_id=cid)

        # With extra fields
        body = _chat_payload("Hello", conversation_id=cid, force_new_session=True)
    """
    cid = overrides.pop("conversation_id", None) or str(uuid.uuid4())
    return {"message": message, "conversation_id": cid, **overrides}
