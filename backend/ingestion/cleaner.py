"""
Email body cleaner: strips quoted reply chains, signatures, and noise.
"""

import re
from typing import Optional

from email_reply_parser import EmailReplyParser


def strip_quoted_replies(text: str) -> str:
    """
    Use email_reply_parser to extract only the 'visible' (non-quoted) text
    from an email body.
    """
    if not text:
        return ""

    reply = EmailReplyParser.read(text)
    # Get only the non-quoted, non-signature fragments
    visible_parts = []
    for fragment in reply.fragments:
        if not fragment.quoted and not fragment.hidden:
            visible_parts.append(fragment.content)

    result = "\n".join(visible_parts).strip()
    return result if result else text  # fallback to full text if parser strips everything


def strip_html_tags(html: str) -> str:
    """Basic HTML to plain text conversion."""
    if not html:
        return ""
    # Remove style and script blocks
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace br and p tags with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li)>", "\n", text, flags=re.IGNORECASE)
    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common entities
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&nbsp;", " ")
    text = text.replace("&quot;", '"')
    # Normalize whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def normalize_whitespace(text: str) -> str:
    """Collapse excessive whitespace and blank lines."""
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\t", " ", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_message_body(body_text: Optional[str], body_html: Optional[str]) -> str:
    """
    Produce a clean body text from available message content.

    Prefers plain text. Falls back to HTML->text conversion.
    Strips quoted replies and signatures.
    """
    # Start with plain text if available
    if body_text:
        text = body_text
    elif body_html:
        text = strip_html_tags(body_html)
    else:
        return ""

    # Strip quoted replies
    text = strip_quoted_replies(text)

    # Normalize
    text = normalize_whitespace(text)

    return text
