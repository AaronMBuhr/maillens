"""
Extract text content from email attachments for indexing.
"""

from typing import Optional

from backend.ingestion.parser import ParsedAttachment


def extract_text_from_attachment(attachment: ParsedAttachment) -> Optional[str]:
    """
    Attempt to extract searchable text from an attachment.
    Returns None if the attachment type is not supported.
    """
    content_type = (attachment.content_type or "").lower()

    if content_type in ("text/plain", "text/csv", "text/tab-separated-values"):
        return _extract_plain_text(attachment)

    if content_type == "application/pdf":
        return _extract_pdf_text(attachment)

    # Add more extractors here as needed:
    # - text/html
    # - application/vnd.openxmlformats-officedocument.* (docx, xlsx)
    # - application/rtf

    return None


def _extract_plain_text(attachment: ParsedAttachment) -> Optional[str]:
    """Extract text from plain text attachments."""
    try:
        return attachment.content.decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_pdf_text(attachment: ParsedAttachment) -> Optional[str]:
    """Extract text from PDF attachments using pymupdf."""
    try:
        import pymupdf

        doc = pymupdf.open(stream=attachment.content, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()

        text = "\n".join(text_parts).strip()
        return text if text else None
    except ImportError:
        print("Warning: pymupdf not installed, skipping PDF extraction")
        return None
    except Exception as e:
        print(f"Warning: Failed to extract PDF text: {e}")
        return None
