# files.py

"""
Router that manages requests, routing, and managing files, images, videos, etc..
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse

from routers.auth import User, current_user
from routers.chats import create_message, _user_is_member as ensure_chat_membership
from routers.websocket import notify_new_message
from database.singleton import Database
from database.attachments import (
    create_attachment_record,
    fetch_attachment_by_id,
    fetch_chat_attachments,
    serialize_attachment,
)
from static.protected.fileManager import ChatAttachmentManager

router_files: APIRouter = APIRouter(prefix="/files", tags=["Files"])


attachment_manager = ChatAttachmentManager()
ATTACHMENT_MAX_BYTES = int(os.getenv("ATTACHMENT_MAX_BYTES", 20 * 1024 * 1024))


def _extract_user_id(user: User) -> int:
    user_id = getattr(user, "user_id", None)
    if user_id is None:
        user_id = getattr(user, "iD", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No fue posible determinar el identificador del usuario autenticado.",
        )
    return user_id


def _build_attachment_payload(row: dict) -> dict:
    return serialize_attachment(row)


@router_files.get("/ison")
async def ison():
    return {"message": "Yeah! I'm on!"}


@router_files.post(
    "/chats/{chat_id}/attachments",
    status_code=status.HTTP_201_CREATED,
)
async def upload_chat_attachment(
    chat_id: int,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
):
    db = Database()
    user_id = _extract_user_id(user)

    if not ensure_chat_membership(db, chat_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para enviar archivos a este chat.",
        )

    if file is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar un archivo.",
        )

    payload = await file.read(ATTACHMENT_MAX_BYTES + 1)
    if len(payload) > ATTACHMENT_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="El archivo excede el tamaño máximo permitido.",
        )
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo está vacío.",
        )

    try:
        stored = attachment_manager.store_attachment(
            chat_id=chat_id,
            sender_id=user_id,
            filename=file.filename or "archivo",
            payload=payload,
            content_type=file.content_type,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # pragma: no cover - errores inesperados
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No fue posible almacenar el archivo.",
        ) from exc

    message_content = "[Archivo adjunto]"
    if file.filename:
        message_content = f"[Archivo adjunto] {file.filename}"

    message = create_message(db, chat_id, user_id, message_content)

    try:
        attachment_row = create_attachment_record(
            db,
            chat_id=chat_id,
            message_id=message["message_id"],
            sender_id=user_id,
            file_name=stored.relative_path,
            mime_type=stored.mime_type,
            size_bytes=stored.size_bytes,
            original_name=stored.original_filename,
        )
    except Exception as exc:  # pragma: no cover - errores inesperados
        attachment_manager.delete_attachment(stored.relative_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo registrar el adjunto en la base de datos.",
        ) from exc

    attachment_payload = _build_attachment_payload(attachment_row)
    message.setdefault("attachments", []).append(attachment_payload)

    try:
        await notify_new_message(message)
    except Exception:
        # Fallos en la notificación no deben revertir el envío del archivo
        pass

    return {
        "message": message,
        "attachment": attachment_payload,
    }


@router_files.get("/chats/{chat_id}/attachments")
async def list_chat_attachments(
    chat_id: int,
    user: User = Depends(current_user),
) -> Dict[str, List[dict]]:
    db = Database()
    user_id = _extract_user_id(user)

    if not ensure_chat_membership(db, chat_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para consultar los archivos de este chat.",
        )

    rows = fetch_chat_attachments(db, chat_id)
    payload = [_build_attachment_payload(row) for row in rows]
    return {"attachments": payload}


@router_files.get("/attachments/{attachment_id}")
async def get_attachment_metadata(
    attachment_id: int,
    user: User = Depends(current_user),
) -> dict:
    db = Database()
    user_id = _extract_user_id(user)

    row = fetch_attachment_by_id(db, attachment_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Adjunto no encontrado.",
        )

    if not ensure_chat_membership(db, row["chat_id"], user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para acceder a este archivo.",
        )

    return _build_attachment_payload(row)


@router_files.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: int,
    user: User = Depends(current_user),
):
    db = Database()
    user_id = _extract_user_id(user)

    row = fetch_attachment_by_id(db, attachment_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Adjunto no encontrado.",
        )

    if not ensure_chat_membership(db, row["chat_id"], user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para descargar este archivo.",
        )

    try:
        file_path = attachment_manager.resolve_relative_path(row["file_name"])
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ruta de archivo inválida.",
        ) from exc

    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El archivo ya no está disponible en el servidor.",
        )

    download_name = row.get("original_name") or Path(row["file_name"]).name

    return FileResponse(
        path=file_path,
        media_type=row["mime_type"],
        filename=download_name,
    )
