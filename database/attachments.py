"""Utilidades para gestionar adjuntos almacenados en la base de datos."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from database.singleton import Database


ATTACHMENT_COLUMNS = """
    SELECT
        id,
        chat_id,
        message_id,
        sender_id,
        file_name,
        mime_type,
        size_bytes,
        created_at,
        original_name
    FROM attachments
"""


def create_attachment_record(
    db: Database,
    *,
    chat_id: int,
    message_id: int,
    sender_id: int,
    file_name: str,
    mime_type: str,
    size_bytes: int | None,
    original_name: str,
) -> dict:
    insert_query = """
        INSERT INTO attachments (
            chat_id,
            message_id,
            sender_id,
            file_name,
            mime_type,
            size_bytes,
            original_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    db.execute_query(
        insert_query,
        (
            chat_id,
            message_id,
            sender_id,
            file_name,
            mime_type,
            size_bytes,
            original_name,
        ),
    )
    rows = db.fetch_query(
        ATTACHMENT_COLUMNS
        + """
        WHERE id = last_insert_rowid()
        """
    )
    if not rows:
        raise RuntimeError("No se pudo registrar el adjunto en la base de datos.")
    return rows[0]


def fetch_attachment_by_id(db: Database, attachment_id: int) -> Optional[dict]:
    rows = db.fetch_query(
        ATTACHMENT_COLUMNS
        + """
        WHERE id = ?
        """,
        (attachment_id,),
    )
    if not rows:
        return None
    return rows[0]


def fetch_chat_attachments(db: Database, chat_id: int) -> List[dict]:
    rows = db.fetch_query(
        ATTACHMENT_COLUMNS
        + """
        WHERE chat_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (chat_id,),
    )
    return rows or []


def fetch_attachments_by_message_ids(
    db: Database, message_ids: Sequence[int]
) -> Dict[int, List[dict]]:
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    rows = db.fetch_query(
        ATTACHMENT_COLUMNS
        + f"""
        WHERE message_id IN ({placeholders})
        ORDER BY created_at ASC, id ASC
        """,
        tuple(message_ids),
    )
    grouped: Dict[int, List[dict]] = {}
    for row in rows or []:
        grouped.setdefault(row["message_id"], []).append(row)
    return grouped


def serialize_attachment(row: dict) -> dict:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "message_id": row["message_id"],
        "sender_id": row["sender_id"],
        "file_name": row["file_name"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"],
        "original_name": row.get("original_name"),
        "download_url": f"/files/attachments/{row['id']}/download",
    }
