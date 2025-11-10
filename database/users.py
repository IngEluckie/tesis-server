"""Utilities for working with user records."""

from __future__ import annotations

from typing import Optional

from database.singleton import Database


def fetch_user_by_id(db: Database, user_id: int) -> Optional[dict]:
    query = """
        SELECT
            Id_Usuarios AS user_id,
            username,
            NombreCompleto AS full_name,
            email,
            Foto_perfil AS profile_image
        FROM Usuarios
        WHERE Id_Usuarios = ?
        LIMIT 1
    """
    rows = db.fetch_query(query, (user_id,))
    if not rows:
        return None
    return rows[0]


def update_user_profile_image(db: Database, user_id: int, relative_path: str | None) -> None:
    query = """
        UPDATE Usuarios
        SET Foto_perfil = ?
        WHERE Id_Usuarios = ?
    """
    db.execute_query(query, (relative_path, user_id))
