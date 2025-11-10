# users.py

"""
Endpoints that allow authenticated users to manage their profile images.
Other authenticated users can only read those images.
"""

from __future__ import annotations

import mimetypes
import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from routers.auth import User, current_user
from database.singleton import Database
from database.users import fetch_user_by_id, update_user_profile_image
from static.protected.fileManager import ProfileImage


router_users = APIRouter(prefix="/users", tags=["Users"])


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
