"""Attachment batch validation migrated from the base server."""

from app.perception.attachments import Attachment

MAX_ATTACHMENTS_PER_TURN = 8
MAX_TOTAL_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _validate_attachment_batch(
    attachments: list[Attachment],
    *,
    max_count: int = MAX_ATTACHMENTS_PER_TURN,
    max_total_bytes: int = MAX_TOTAL_ATTACHMENT_BYTES,
) -> None:
    if len(attachments) > max_count:
        raise ValueError(f"A message may include at most {max_count} attachments")
    total_bytes = sum(attachment.size_bytes or 0 for attachment in attachments)
    if total_bytes > max_total_bytes:
        raise ValueError(
            f"Message attachments exceed the {max_total_bytes}-byte aggregate limit"
        )
