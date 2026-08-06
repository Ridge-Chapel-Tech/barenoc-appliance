"""Tech-to-tech internal chat (AIM-style buddy messaging).

Powering the buddy list in the desktop chat client: users message each
other directly, threads render like instant messages, and unread counts
drive the badges. Read state is marked when a thread is fetched.
"""

import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, and_, func
from sqlalchemy.orm import Session
from database import get_db
from models import User, ChatMessage
from auth import get_current_user

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


def chat_client_enabled() -> bool:
    """Desktop chat client feature flag (CHAT_CLIENT_ENABLED, default true).

    Hot-reads the env file so the Settings toggle applies without a container
    restart. When off, the chat API + Downloads are gated (403).
    """
    try:
        from llm_providers import read_env_file
        raw = (read_env_file().get("CHAT_CLIENT_ENABLED") or "").strip().lower()
        if raw:
            return raw in ("1", "true", "yes", "on")
    except Exception:
        pass
    return True


def require_chat_enabled(user: User = Depends(get_current_user)):
    """Dependency: logged in AND the desktop chat feature is enabled."""
    if not chat_client_enabled():
        raise HTTPException(
            status_code=403,
            detail="Desktop chat client is disabled (enable it in Settings → General)",
        )
    return user


class ChatSend(BaseModel):
    to_username: str = Field(..., min_length=1, max_length=64)
    body: str = Field(..., min_length=1, max_length=4000)


def _user_brief(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role,
        "last_login": u.last_login.isoformat() if u.last_login else None,
    }


def _msg_brief(m: ChatMessage) -> dict:
    return {
        "id": m.id,
        "from_username": m.sender.username if m.sender else "?",
        "to_username": m.recipient.username if m.recipient else "?",
        "body": m.body,
        "read": m.read_at is not None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/users")
def chat_users(db: Session = Depends(get_db), user: User = Depends(require_chat_enabled)):
    """Buddy list: active users I can message (excludes myself) + the bot names
    so the client can label the Queue Manager and AI assistant."""
    users = (
        db.query(User)
        .filter(User.is_active == True, User.id != user.id)  # noqa: E712
        .order_by(User.username)
        .all()
    )
    from routes.settings import _read_env_file
    env = _read_env_file()
    return {
        "users": [_user_brief(u) for u in users],
        "names": {
            "queue_manager": (env.get("BOT_QUEUE_MANAGER_NAME") or "Juniper").strip() or "Juniper",
            "assistant": (env.get("BOT_ASSISTANT_NAME") or "Lily").strip() or "Lily",
        },
    }


@router.get("/conversations")
def chat_conversations(db: Session = Depends(get_db), user: User = Depends(require_chat_enabled)):
    """Threads involving me: other user, last message, unread count."""
    rows = (
        db.query(ChatMessage)
        .filter(or_(ChatMessage.from_user_id == user.id, ChatMessage.to_user_id == user.id))
        .order_by(ChatMessage.created_at.desc())
        .limit(1000)
        .all()
    )
    seen = set()
    convs = []
    for m in rows:
        other_id = m.from_user_id if m.to_user_id == user.id else m.to_user_id
        if other_id in seen or other_id == user.id:
            continue
        other = db.query(User).get(other_id)
        if other is None:
            continue
        seen.add(other_id)
        unread = (
            db.query(func.count(ChatMessage.id))
            .filter(
                ChatMessage.to_user_id == user.id,
                ChatMessage.from_user_id == other_id,
                ChatMessage.read_at.is_(None),
            )
            .scalar()
            or 0
        )
        convs.append({"other": _user_brief(other), "last_message": _msg_brief(m), "unread": unread})
    return {"conversations": convs}


@router.get("/messages")
def chat_messages(
    with_username: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_chat_enabled),
):
    """Full thread with another user; marks incoming messages as read."""
    other = db.query(User).filter(User.username == with_username).first()
    if not other:
        raise HTTPException(status_code=404, detail="User not found")

    msgs = (
        db.query(ChatMessage)
        .filter(
            or_(
                and_(ChatMessage.from_user_id == user.id, ChatMessage.to_user_id == other.id),
                and_(ChatMessage.from_user_id == other.id, ChatMessage.to_user_id == user.id),
            )
        )
        .order_by(ChatMessage.created_at.asc())
        .limit(500)
        .all()
    )
    now = datetime.datetime.utcnow()
    changed = False
    for m in msgs:
        if m.to_user_id == user.id and m.read_at is None:
            m.read_at = now
            changed = True
    if changed:
        db.commit()

    return {"messages": [_msg_brief(m) for m in msgs], "other": _user_brief(other)}


@router.post("/messages", status_code=201)
def chat_send(data: ChatSend, db: Session = Depends(get_db), user: User = Depends(require_chat_enabled)):
    other = db.query(User).filter(User.username == data.to_username).first()
    if not other:
        raise HTTPException(status_code=404, detail="User not found")
    if other.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot message yourself")
    if other.is_active is False:
        raise HTTPException(status_code=400, detail="Recipient is disabled")

    body = data.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    m = ChatMessage(from_user_id=user.id, to_user_id=other.id, body=body)
    db.add(m)
    db.commit()
    db.refresh(m)
    return {"status": "sent", "message": _msg_brief(m)}
