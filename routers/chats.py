# chats.py

# Importamos librerías
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel

# Importamos módulos
from routers.auth import current_user, User
from database.singleton import Database
from routers.websocket import notify_new_message

router_chats: APIRouter = APIRouter(prefix="/chats")

logger = logging.getLogger(__name__)


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


@router_chats.get("/ison")
async def is_active():
    return "Yes, I'm working ma'a faka"

"""
PARA EL ENVÍO DE MENSAJES
"""

# MODELO Pydantic para la entrada
class MessageCreate(BaseModel):
    content: str


class PaginationMeta(BaseModel):
    older_cursor: str | None = None
    has_more_older: bool = False

# Función para crear un mensaje en la BD
def create_message(db: Database, chat_id: int, user_id: int, content: str) -> dict:
    insert_query = """
        INSERT INTO messages (chat_id, user_id, content, created_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    """
    db.execute_query(insert_query, (chat_id, user_id, content))

    row = db.fetch_query("SELECT last_insert_rowid() AS message_id")
    if not row:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo obtener el identificador del mensaje recién creado."
        )

    message_id = row[0]["message_id"]
    db.execute_query(
        """
        UPDATE chats
        SET last_activity = CURRENT_TIMESTAMP
        WHERE chat_id = ?
        """,
        (chat_id,),
    )
    message_rows = db.fetch_query(
        """
        SELECT
            m.message_id,
            m.chat_id,
            m.user_id,
            u.username AS sender_username,
            m.content,
            m.created_at
        FROM messages AS m
        INNER JOIN Usuarios AS u ON u.Id_Usuarios = m.user_id
        WHERE m.message_id = ?
        """,
        (message_id,),
    )
    if not message_rows:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se encontró el mensaje recién creado."
        )

    return message_rows[0]

@router_chats.post("/{chat_id}/send_message")
async def send_message_to_chat(
    chat_id: int,
    message: MessageCreate,
    user: User = Depends(current_user)
):
    """
    Envía (crea) un mensaje en el chat con id = chat_id,
    usando el usuario logueado (user.iD) como remitente.
    Retorna el mensaje recién creado.
    """
    db = Database()
    user_id = _extract_user_id(user)

    if not _user_is_member(db, chat_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para enviar mensajes a este chat.",
        )

    content = message.content.strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El contenido del mensaje no puede estar vacío.",
        )

    message_created = create_message(db, chat_id, user_id, content)

    try:
        await notify_new_message(message_created)
    except Exception:
        logger.exception(
            "Fallo al notificar un mensaje nuevo para el chat %s", chat_id
        )

    return message_created


"""
Para la búsqueda de usuarios en la navbar:
- Crear o buscar chat
- Querys de mensajes
"""

def find_user_by_username(db: Database, username: str) -> Optional[int]:
    query = """
        SELECT Id_Usuarios AS user_id
        FROM Usuarios
        WHERE username = ?
        LIMIT 1
    """
    result = db.fetch_query(query, (username,))
    if not result:
        return None
    return result[0]["user_id"]

def find_single_chat(db: Database, user_a_id: int, user_b_id: int) -> Optional[int]:
    query = """
        SELECT c.chat_id
        FROM chats c
        WHERE c.is_group = 0
          AND c.chat_id IN (
              SELECT chat_id FROM chat_members WHERE user_id = ?
          )
          AND c.chat_id IN (
              SELECT chat_id FROM chat_members WHERE user_id = ?
          )
        LIMIT 1
    """
    rows = db.fetch_query(query, (user_a_id, user_b_id))
    if not rows:
        return None
    return rows[0]["chat_id"]

def create_single_chat(db: Database, creator_id: int, other_user_id: int) -> int:
    insert_chat = """
        INSERT INTO chats (is_group, created_by, created_at, last_activity)
        VALUES (0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
    """
    db.execute_query(insert_chat, (creator_id,))

    new_chat_row = db.fetch_query("SELECT last_insert_rowid() AS chat_id")
    if not new_chat_row:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo crear el chat individual."
        )
    chat_id = new_chat_row[0]["chat_id"]

    insert_members = """
        INSERT OR IGNORE INTO chat_members (chat_id, user_id, joined_at, role)
        VALUES (?, ?, CURRENT_TIMESTAMP, ?)
    """
    db.executemany(
        insert_members,
        [
            (chat_id, creator_id, "admin"),
            (chat_id, other_user_id, "member"),
        ],
    )

    return chat_id

def fetch_chat_messages(
    db: Database,
    chat_id: int,
    limit: int = 20,
    older_cursor: str | None = None,
) -> tuple[List[dict], PaginationMeta]:
    """
    Obtiene los mensajes más recientes de un chat en orden cronológico ascendente.
    Usa older_cursor (timestamp ISO) para paginar hacia mensajes más antiguos.
    """
    base_limit = max(limit, 1)
    query = """
        SELECT
            m.message_id,
            m.chat_id,
            m.user_id,
            u.username AS sender_username,
            m.content,
            m.created_at
        FROM messages AS m
        INNER JOIN Usuarios AS u ON u.Id_Usuarios = m.user_id
        WHERE m.chat_id = ?
    """
    params: list = [chat_id]
    if older_cursor:
        query += " AND m.created_at < ?"
        params.append(older_cursor)

    query += " ORDER BY m.created_at DESC LIMIT ?"
    params.append(base_limit + 1)  # +1 para detectar si hay más mensajes antiguos

    rows = db.fetch_query(query, tuple(params)) or []

    has_more = len(rows) > base_limit
    trimmed = rows[:base_limit]
    trimmed.reverse()  # Cronológico ascendente

    meta = PaginationMeta()
    if trimmed:
        meta.older_cursor = trimmed[0]["created_at"]
    meta.has_more_older = has_more

    return trimmed, meta

### AQUI LA RUTA PRINCIPAL QUE RECIBE UN STRING (target_username)
@router_chats.get("/open_single_chat/{target_username}")
async def open_single_chat(
    target_username: str,
    limit: int = 20,
    older_cursor: str | None = None,
    user: User = Depends(current_user),  # <--- 'user' viene de tu modelo 'User' con iD, username, ...
):
    db = Database()
    requester_id = _extract_user_id(user)

    if target_username == getattr(user, "username", None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No puedes abrir un chat contigo mismo.",
        )

    other_user_id = find_user_by_username(db, target_username)
    if other_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró usuario con username '{target_username}'.",
        )

    existing_chat_id = find_single_chat(db, requester_id, other_user_id)
    if existing_chat_id is None:
        existing_chat_id = create_single_chat(db, requester_id, other_user_id)

    messages, meta = fetch_chat_messages(db, existing_chat_id, limit, older_cursor)

    return {
        "chat_id": existing_chat_id,
        "messages": messages,
        "pagination": meta.model_dump(),
    }

@router_chats.get("/me")
async def me(user: User = Depends(current_user)):
    return user

@router_chats.get("/search_user_navbar/{terminoBusqueda}")
async def search_user(user: User = Depends(current_user), terminoBusqueda: str = None):
    db = Database()  # Obtén la instancia del Singleton
    if terminoBusqueda is None or terminoBusqueda.strip() == "":
        return []

    user_id = _extract_user_id(user)
    like_pattern = f"%{terminoBusqueda}%"

    query = """
        SELECT username
        FROM Usuarios
        WHERE username LIKE ?
          AND Id_Usuarios != ?
        ORDER BY username ASC
        LIMIT 20
    """
    rows = db.fetch_query(query, (like_pattern, user_id))
    if not rows:
        return []
    return [row["username"] for row in rows]

@router_chats.get("/my_chats", tags=["Chats"])
async def get_my_chats(limit: int = 10, offset: int = 0, user: User = Depends(current_user)):
    """
    Retorna los chats en los que el usuario actual es miembro, ordenados por la última actividad.
    - Para chats individuales (is_group = 0): retorna el username del otro participante.
    - Para chats grupales (is_group = 1): retorna 'Grupo Chat' (puedes ajustar si agregas nombre de grupo).
    La paginación se maneja con LIMIT y OFFSET.
    """
    db = Database()
    user_id = _extract_user_id(user)
    limit = max(limit, 1)
    offset = max(offset, 0)

    query = """
        SELECT
            c.chat_id,
            c.is_group,
            COALESCE(c.last_activity, c.created_at) AS last_activity,
            CASE
                WHEN c.is_group = 0 THEN (
                    SELECT username
                    FROM Usuarios
                    WHERE Id_Usuarios = (
                        SELECT cm2.user_id
                        FROM chat_members AS cm2
                        WHERE cm2.chat_id = c.chat_id AND cm2.user_id != ?
                        LIMIT 1
                    )
                )
                ELSE COALESCE(g.nombre, 'Grupo Chat')
            END AS chat_name
        FROM chats AS c
        INNER JOIN chat_members AS cm ON cm.chat_id = c.chat_id
        LEFT JOIN info_grupos AS g ON g.chat_id = c.chat_id
        WHERE cm.user_id = ?
        ORDER BY last_activity DESC
        LIMIT ? OFFSET ?
    """

    rows = db.fetch_query(query, (user_id, user_id, limit, offset))
    return {"chats": rows or []}

@router_chats.get("/get_chat/{chat_id}", tags=["Chats"])
async def get_chat(
    chat_id: int,
    limit: int = 20,
    older_cursor: str | None = None,
    user: User = Depends(current_user)
):
    """
    Retorna los mensajes del chat especificado por chat_id con paginación.
    Primero verifica que el usuario autenticado sea miembro del chat.
    """
    db = Database()
    user_id = _extract_user_id(user)

    if not _user_is_member(db, chat_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para acceder a este chat.",
        )

    messages, meta = fetch_chat_messages(db, chat_id, limit, older_cursor)
    return {
        "chat_id": chat_id,
        "messages": messages,
        "pagination": meta.model_dump(),
    }


def _user_is_member(db: Database, chat_id: int, user_id: int) -> bool:
    membership_rows = db.fetch_query(
        """
        SELECT 1
        FROM chat_members
        WHERE chat_id = ? AND user_id = ?
        LIMIT 1
        """,
        (chat_id, user_id),
    )
    return bool(membership_rows)
