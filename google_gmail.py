"""Gmail send/draft wrapper. Always tries draft-only by default."""
from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Optional

from googleapiclient.discovery import build

from google_oauth import get_credentials


def _service():
    creds = get_credentials(allow_browser=False)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _build_message(
    to: str,
    subject: str,
    body_html: str,
    body_text: Optional[str] = None,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    reply_to_thread: Optional[str] = None,
    in_reply_to_header: Optional[str] = None,
) -> dict:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if in_reply_to_header:
        msg["In-Reply-To"] = in_reply_to_header
        msg["References"] = in_reply_to_header
    if body_text:
        msg.set_content(body_text)
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_html, subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if reply_to_thread:
        payload["threadId"] = reply_to_thread
    return payload


def draft(
    to: str,
    subject: str,
    body_html: str,
    *,
    body_text: Optional[str] = None,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    reply_to_thread: Optional[str] = None,
    in_reply_to_header: Optional[str] = None,
) -> dict:
    """Create a Gmail draft. Returns {kind: 'draft', id, message_id, thread_id, web_url}."""
    svc = _service()
    msg = _build_message(
        to, subject, body_html, body_text=body_text, cc=cc, bcc=bcc,
        reply_to_thread=reply_to_thread, in_reply_to_header=in_reply_to_header,
    )
    resp = svc.users().drafts().create(userId="me", body={"message": msg}).execute()
    msg_id = resp.get("message", {}).get("id")
    thread_id = resp.get("message", {}).get("threadId")
    return {
        "kind": "draft",
        "id": resp.get("id"),
        "message_id": msg_id,
        "thread_id": thread_id,
        "web_url": f"https://mail.google.com/mail/u/0/#drafts?compose={resp.get('id')}",
    }


def send(
    to: str,
    subject: str,
    body_html: str,
    *,
    body_text: Optional[str] = None,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    reply_to_thread: Optional[str] = None,
    in_reply_to_header: Optional[str] = None,
) -> dict:
    """Send immediately. Returns {kind: 'sent', id, thread_id}."""
    svc = _service()
    msg = _build_message(
        to, subject, body_html, body_text=body_text, cc=cc, bcc=bcc,
        reply_to_thread=reply_to_thread, in_reply_to_header=in_reply_to_header,
    )
    resp = svc.users().messages().send(userId="me", body=msg).execute()
    return {
        "kind": "sent",
        "id": resp.get("id"),
        "thread_id": resp.get("threadId"),
        "web_url": f"https://mail.google.com/mail/u/0/#sent/{resp.get('id')}",
    }


def send_or_draft(*, draft_only: bool, **kwargs) -> dict:
    return draft(**kwargs) if draft_only else send(**kwargs)


def my_email() -> Optional[str]:
    """Return the authenticated user's email address."""
    try:
        svc = _service()
        prof = svc.users().getProfile(userId="me").execute()
        return prof.get("emailAddress")
    except Exception:
        return None
