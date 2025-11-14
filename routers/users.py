# users.py

"""
Endpoints that allow authenticated users to manage their profile images.
Other authenticated users can only read those images.
"""

from __future__ import annotations

import logging
import mimetypes
import os

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from routers.auth import User, current_user
from database.singleton import Database
from database.users import (
    fetch_user_by_id,
    fetch_user_by_username,
    update_user_profile_image,
)
from static.protected.fileManager import ProfileImage
import redis


router_users = APIRouter(prefix="/users", tags=["Users"])


logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CONNECTION_TTL_SECONDS = int(os.getenv("WEBSOCKET_CONNECTION_TTL", "120"))
presence_redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)

profile_image_manager = ProfileImage()
PROFILE_IMAGE_MAX_BYTES = int(os.getenv("PROFILE_IMAGE_MAX_BYTES", 5 * 1024 * 1024))


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


def _ensure_user(db: Database, user_id: int) -> dict:
    record = fetch_user_by_id(db, user_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado.",
        )
    return record


def _ensure_user_by_username(db: Database, username: str) -> dict:
    record = fetch_user_by_username(db, username)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado.",
        )
    return record


def _normalize_username(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar un nombre de usuario válido.",
        )
    return normalized


def _serve_profile_image(relative_path: str) -> FileResponse:
    if not relative_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Este usuario no tiene una imagen de perfil configurada.",
        )
    try:
        image_path = profile_image_manager.resolve_relative_path(relative_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ruta de imagen almacenada inválida.",
        ) from exc

    if not image_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="La imagen de perfil ya no está disponible en el servidor.",
        )

    media_type, _ = mimetypes.guess_type(image_path.name)
    return FileResponse(
        path=image_path,
        media_type=media_type or "image/png",
        filename=image_path.name,
    )


def _presence_key(user_id: int) -> str:
    return f"connection:{user_id}"


def _read_presence(user_id: int) -> dict:
    key = _presence_key(user_id)
    try:
        record = presence_redis.hgetall(key)
    except Exception as exc:
        logger.debug("No fue posible leer el estado de usuario %s: %s", user_id, exc)
        record = {}

    status_value = record.get("status") if record else None
    status = status_value or "disconnected"
    last_seen = record.get("last_seen") if record else None

    connection_raw = record.get("connection_count") if record else None
    try:
        connection_count = int(connection_raw) if connection_raw is not None else 0
    except (TypeError, ValueError):
        connection_count = 0

    ttl_seconds: int | None = None
    try:
        ttl_raw = presence_redis.ttl(key)
    except Exception as exc:
        logger.debug("No fue posible obtener el TTL de la presencia %s: %s", user_id, exc)
    else:
        if isinstance(ttl_raw, int) and ttl_raw >= 0:
            ttl_seconds = ttl_raw

    return {
        "status": status,
        "last_seen": last_seen,
        "connection_count": connection_count,
        "ttl_seconds": ttl_seconds,
    }


@router_users.get("/status")
async def get_users_status(
    ids: str = Query(..., description="Lista separada por comas de identificadores de usuario."),
    user: User = Depends(current_user),
):
    db = Database()
    _ensure_user(db, _extract_user_id(user))

    tokens = [segment.strip() for segment in (ids or "").split(",") if segment.strip()]
    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar al menos un identificador válido en 'ids'.",
        )

    target_ids: set[int] = set()
    invalid_tokens: list[str] = []
    for token in tokens:
        try:
            target_ids.add(int(token))
        except ValueError:
            invalid_tokens.append(token)

    if invalid_tokens:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Identificadores inválidos: {', '.join(invalid_tokens)}.",
        )

    statuses = {target_id: _read_presence(target_id) for target_id in sorted(target_ids)}
    return {"users": statuses}


@router_users.get("/{target_user_id}/status")
async def get_user_status(
    target_user_id: int,
    user: User = Depends(current_user),
):
    db = Database()
    _ensure_user(db, _extract_user_id(user))
    # Retorna 404 si el usuario objetivo no existe.
    _ensure_user(db, target_user_id)
    return _read_presence(target_user_id)


@router_users.get("/me/avatar")
async def get_my_profile_image(user: User = Depends(current_user)):
    db = Database()
    user_id = _extract_user_id(user)
    record = _ensure_user(db, user_id)
    return _serve_profile_image(record.get("profile_image"))


@router_users.put("/me/avatar", status_code=status.HTTP_200_OK)
async def update_my_profile_image(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
):
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar una imagen válida.",
        )

    payload = await file.read(PROFILE_IMAGE_MAX_BYTES + 1)
    if len(payload) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo está vacío.",
        )
    if len(payload) > PROFILE_IMAGE_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="La imagen excede el tamaño máximo permitido.",
        )

    db = Database()
    user_id = _extract_user_id(user)
    record = _ensure_user(db, user_id)
    previous_path = record.get("profile_image")

    try:
        stored = profile_image_manager.createProfileImage(
            payload,
            user_id=user_id,
            filename=file.filename,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No fue posible procesar la imagen de perfil.",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        update_user_profile_image(db, user_id, stored.relative_path)
    except Exception as exc:
        profile_image_manager.delete_profile_image(stored.relative_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No fue posible actualizar la imagen de perfil.",
        ) from exc

    if previous_path and previous_path != stored.relative_path:
        profile_image_manager.delete_profile_image(previous_path)

    return {
        "profile_image": stored.relative_path,
        "download_url": f"/users/{user_id}/avatar",
    }


@router_users.delete("/me/avatar", status_code=status.HTTP_200_OK)
async def delete_my_profile_image(user: User = Depends(current_user)):
    db = Database()
    user_id = _extract_user_id(user)
    record = _ensure_user(db, user_id)
    relative_path = record.get("profile_image")
    if not relative_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No tienes una imagen de perfil configurada.",
        )

    try:
        update_user_profile_image(db, user_id, None)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No fue posible eliminar la imagen de perfil.",
        ) from exc

    profile_image_manager.delete_profile_image(relative_path)
    return {"detail": "Imagen de perfil eliminada correctamente."}


@router_users.get("/{target_user_id}/avatar")
async def get_user_profile_image(
    target_user_id: int,
    user: User = Depends(current_user),
):
    db = Database()
    _ensure_user(db, _extract_user_id(user))
    record = _ensure_user(db, target_user_id)
    return _serve_profile_image(record.get("profile_image"))


@router_users.get("/by-username/{target_username}/avatar")
async def get_user_profile_image_by_username(
    target_username: str,
    user: User = Depends(current_user),
):
    db = Database()
    _ensure_user(db, _extract_user_id(user))
    normalized_username = _normalize_username(target_username)
    record = _ensure_user_by_username(db, normalized_username)
    return _serve_profile_image(record.get("profile_image"))
