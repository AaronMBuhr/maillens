"""
Email parser: reads mbox and Maildir formats, extracts structured message data.
"""

import email
import email.policy
import hashlib
import mailbox
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Generator, Optional


@dataclass
class ParsedAttachment:
    filename: Optional[str]
    content_type: str
    size_bytes: int
    content: bytes  # raw bytes for text extraction later


@dataclass
class ParsedMessage:
    message_id: str
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    subject: Optional[str] = None
    sender: Optional[str] = None
    recipients_to: Optional[str] = None
    recipients_cc: Optional[str] = None
    date: Optional[datetime] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    folder: Optional[str] = None
    source_file: Optional[str] = None
    account: Optional[str] = None
    attachments: list[ParsedAttachment] = field(default_factory=list)
    has_attachments: bool = False


def _decode_header(value: Optional[str]) -> Optional[str]:
    """Decode an email header value, handling encoded words."""
    if value is None:
        return None
    decoded_parts = email.header.decode_header(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)


def _sanitize(value: Optional[str]) -> Optional[str]:
    """Strip null bytes that PostgreSQL rejects in UTF-8 text columns."""
    if value is None:
        return None
    return value.replace("\x00", "")


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    """Parse email date header into a timezone-aware UTC datetime.

    Some email Date headers include timezone info and some don't;
    asyncpg rejects mixing the two in a batch insert, so we normalise
    everything to UTC here.
    """
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _safe_charset(raw: Optional[str]) -> str:
    """Validate a charset string, falling back to utf-8 for garbage values."""
    if not raw:
        return "utf-8"
    import codecs
    try:
        codecs.lookup(raw)
        return raw
    except LookupError:
        return "utf-8"


def _extract_body_and_attachments(
    msg: email.message.Message,
) -> tuple[Optional[str], Optional[str], list[ParsedAttachment]]:
    """
    Walk MIME parts to extract plain text body, HTML body, and attachments.
    """
    body_text = None
    body_html = None
    attachments = []

    if not msg.is_multipart():
        content_type = msg.get_content_type()
        disposition = msg.get_content_disposition()

        if disposition == "attachment":
            payload = msg.get_payload(decode=True) or b""
            attachments.append(ParsedAttachment(
                filename=msg.get_filename(),
                content_type=content_type,
                size_bytes=len(payload),
                content=payload,
            ))
        elif content_type == "text/plain":
            charset = _safe_charset(msg.get_content_charset())
            payload = msg.get_payload(decode=True)
            if payload:
                body_text = payload.decode(charset, errors="replace")
        elif content_type == "text/html":
            charset = _safe_charset(msg.get_content_charset())
            payload = msg.get_payload(decode=True)
            if payload:
                body_html = payload.decode(charset, errors="replace")
        return body_text, body_html, attachments

    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()

        if disposition == "attachment":
            payload = part.get_payload(decode=True) or b""
            attachments.append(ParsedAttachment(
                filename=part.get_filename(),
                content_type=content_type,
                size_bytes=len(payload),
                content=payload,
            ))
            continue

        if content_type == "text/plain" and body_text is None:
            charset = _safe_charset(part.get_content_charset())
            payload = part.get_payload(decode=True)
            if payload:
                body_text = payload.decode(charset, errors="replace")
        elif content_type == "text/html" and body_html is None:
            charset = _safe_charset(part.get_content_charset())
            payload = part.get_payload(decode=True)
            if payload:
                body_html = payload.decode(charset, errors="replace")

    return body_text, body_html, attachments


def _parse_single_message(
    msg: email.message.Message,
    folder: Optional[str] = None,
    source_file: Optional[str] = None,
    account: Optional[str] = None,
) -> Optional[ParsedMessage]:
    """Parse a single email.message.Message into a ParsedMessage."""
    message_id = msg.get("Message-ID", "").strip()
    if not message_id:
        subject = msg.get("Subject", "") or ""
        date = msg.get("Date", "") or ""
        sender = msg.get("From", "") or ""
        raw = msg.as_string()[:1024]
        digest = hashlib.sha256(f"{subject}{date}{sender}{source_file}{raw}".encode()).hexdigest()[:24]
        message_id = f"<generated-{digest}@maillens>"

    body_text, body_html, attachments = _extract_body_and_attachments(msg)

    return ParsedMessage(
        message_id=message_id,
        in_reply_to=msg.get("In-Reply-To", "").strip() or None,
        references=msg.get("References", "").strip() or None,
        subject=_sanitize(_decode_header(msg.get("Subject"))),
        sender=_sanitize(_decode_header(msg.get("From"))),
        recipients_to=_sanitize(_decode_header(msg.get("To"))),
        recipients_cc=_sanitize(_decode_header(msg.get("Cc"))),
        date=_parse_date(msg.get("Date")),
        body_text=_sanitize(body_text),
        body_html=_sanitize(body_html),
        folder=folder,
        source_file=source_file,
        account=account,
        attachments=attachments,
        has_attachments=len(attachments) > 0,
    )


def parse_mbox(
    path: str, folder_name: Optional[str] = None, account: Optional[str] = None,
) -> Generator[ParsedMessage, None, None]:
    """Parse all messages from an mbox file."""
    mbox = mailbox.mbox(path)
    folder = folder_name or Path(path).stem
    for key in mbox.iterkeys():
        try:
            msg = mbox[key]
            parsed = _parse_single_message(msg, folder=folder, source_file=path, account=account)
            if parsed:
                yield parsed
        except Exception as e:
            print(f"Warning: Failed to parse message {key} in {path}: {e}")
            continue


def parse_maildir(
    path: str, folder_name: Optional[str] = None, account: Optional[str] = None,
) -> Generator[ParsedMessage, None, None]:
    """Parse all messages from a Maildir directory."""
    md = mailbox.Maildir(path)
    folder = folder_name or Path(path).name
    for key in md.iterkeys():
        try:
            msg = md[key]
            parsed = _parse_single_message(msg, folder=folder, source_file=path, account=account)
            if parsed:
                yield parsed
        except Exception as e:
            print(f"Warning: Failed to parse message {key} in {path}: {e}")
            continue


def detect_format(path: str) -> str:
    """Detect if a path is an mbox file or Maildir directory."""
    p = Path(path)
    if p.is_file():
        return "mbox"
    if p.is_dir():
        # Maildir has cur/, new/, tmp/ subdirectories
        if (p / "cur").is_dir() or (p / "new").is_dir():
            return "maildir"
        return "directory"  # container of mbox files or subdirectories
    return "unknown"


def _extract_account(rel_parts: tuple[str, ...]) -> str:
    """
    Derive an account name from the relative path components within a
    Thunderbird profile directory.

    Thunderbird layout:
        ImapMail/<server>/...      → account = <server>
        Mail/<account name>/...    → account = <account name>  (e.g. "Local Folders")

    Falls back to the first path component, or "default".
    """
    if len(rel_parts) < 2:
        return rel_parts[0] if rel_parts else "default"

    top = rel_parts[0]
    if top.lower() in ("imapmail", "mail"):
        return rel_parts[1]
    return top


def discover_mail_sources(root_path: str) -> list[tuple[str, str, str, str]]:
    """
    Walk a Thunderbird mail directory and discover all mbox files and Maildir dirs.

    Returns list of (path, format, folder_name, account) tuples.
    """
    root = Path(root_path)
    sources: list[tuple[str, str, str, str]] = []

    if not root.exists():
        return sources

    fmt = detect_format(root_path)
    if fmt == "mbox":
        sources.append((root_path, "mbox", root.stem, "default"))
        return sources
    if fmt == "maildir":
        sources.append((root_path, "maildir", root.name, "default"))
        return sources

    for item in sorted(root.rglob("*")):
        if item.name.startswith("."):
            continue
        if item.is_file() and not item.suffix:
            if not item.name.endswith(".msf") and item.stat().st_size > 0:
                rel = item.relative_to(root)
                folder_name = str(rel).replace("\\", ".").replace("/", ".")
                account = _extract_account(rel.parts)
                sources.append((str(item), "mbox", folder_name, account))
        elif item.is_dir():
            if (item / "cur").is_dir() or (item / "new").is_dir():
                rel = item.relative_to(root)
                folder_name = str(rel).replace("\\", ".").replace("/", ".")
                account = _extract_account(rel.parts)
                sources.append((str(item), "maildir", folder_name, account))

    return sources
