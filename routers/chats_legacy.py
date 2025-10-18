# chats.py

# Importamos librerías
from fastapi import APIRouter, HTTPException, Depends, status
#from dotenv import load_dotenv
#import os
from pydantic import BaseModel
#from fastapi.security import OAuth2PasswordBearer
from icecream import ic
from typing import Optional, List

# Importamos módulos
from routers.authentication import current_user, User
from database.functions import searchNavbarUser, DatabaseSingleton

router = APIRouter(prefix="/chats")

"""
PARA EL ENVÍO DE MENSAJES
"""

# MODELO Pydantic para la entrada
class MessageCreate(BaseModel):
    content: str

# Función para crear un mensaje en la BD
def create_message(db: DatabaseSingleton, chat_id: int, user_id: int, content: str) -> dict:
    """
    Inserta un nuevo mensaje en la tabla 'messages' y retorna el mensaje recién creado.
    """
    # Insertar el mensaje
    insert_query = """
        INSERT INTO messages (chat_id, user_id, content, created_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    """
    db.execute_query(insert_query, (chat_id, user_id, content))

    # Obtener el ID del mensaje recién insertado
    row = db.fetch_query("SELECT last_insert_rowid() as message_id")
    new_id = row[0]["message_id"]

    # Recuperar el registro completo
    fetch_query = """
        SELECT message_id, chat_id, user_id, content, created_at
        FROM messages
        WHERE message_id = ?
    """
    msg_row = db.fetch_query(fetch_query, (new_id,))

    return msg_row[0] if msg_row else {}

@router.post("/{chat_id}/send_message")
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
    db = DatabaseSingleton()

    # (OPCIONAL) Validar si el usuario logueado forma parte de ese chat
    # por ejemplo:
    member_check = db.fetch_query(
        """
        SELECT COUNT(*) as count 
        FROM chat_members 
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat_id, user.iD)
    )
    if member_check[0]["count"] == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para enviar mensajes a este chat."
        )

    # Crear mensaje
    new_message = create_message(db, chat_id, user.iD, message.content)
    return new_message


"""
AQUI PARA:
- Crear o buscar chat
- Querys de mensajes
"""

def find_user_by_username(db: DatabaseSingleton, username: str) -> Optional[int]:
    """
    Retorna el 'Id_Usuarios' (entero) de un usuario dado su username,
    y lo mapea a 'id' en el resultado.
    """
    query = """
        SELECT Id_Usuarios AS id
        FROM Usuarios
        WHERE username = ?
        LIMIT 1
    """
    resultado = db.fetch_query(query, (username,))
    if resultado and len(resultado) > 0:
        return resultado[0]["id"]  # 'id' proviene de AS id
    return None

def find_single_chat(db: DatabaseSingleton, user_a_id: int, user_b_id: int) -> Optional[int]:
    """
    Retorna el 'chat_id' de un chat (no grupal) donde estén exactamente
    ambos usuarios user_a_id y user_b_id.
    Si no existe, retorna None.
    """
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
    resultado = db.fetch_query(query, (user_a_id, user_b_id))

    if resultado and len(resultado) > 0:
        return resultado[0]["chat_id"]
    else:
        return None

def create_single_chat(db: DatabaseSingleton, creator_id: int, other_user_id: int) -> int:
    """
    Crea un nuevo chat no grupal en la tabla 'chats' con is_group=0,
    y agrega las filas en 'chat_members' para ambos usuarios.
    Retorna el 'chat_id' recién creado.
    """
    insert_chats = """
        INSERT INTO chats (is_group, created_by, created_at)
        VALUES (0, ?, CURRENT_TIMESTAMP)
    """
    db.execute_query(insert_chats, (creator_id,))
    
    last_chat_id_query = "SELECT last_insert_rowid() as chat_id"
    row = db.fetch_query(last_chat_id_query)
    chat_id = row[0]["chat_id"]

    insert_members = """
        INSERT INTO chat_members (chat_id, user_id, joined_at, role)
        VALUES (?, ?, CURRENT_TIMESTAMP, ?)
    """
    db.execute_query(insert_members, (chat_id, creator_id, "admin"))
    db.execute_query(insert_members, (chat_id, other_user_id, "member"))

    return chat_id

def fetch_chat_messages(db: DatabaseSingleton, chat_id: int, limit: int = 20, offset: int = 0) -> List[dict]:
    """
    Retorna una lista de los mensajes en el chat dado,
    ordenados por fecha descendente.
    Aplica LIMIT y OFFSET para paginación.
    """
    query = """
        SELECT m.message_id, m.chat_id, m.user_id, m.content, m.created_at
        FROM messages m
        WHERE m.chat_id = ?
        ORDER BY m.created_at DESC
        LIMIT ? OFFSET ?
    """
    resultados = db.fetch_query(query, (chat_id, limit, offset))
    return resultados if resultados else []

### AQUI LA RUTA PRINCIPAL QUE RECIBE UN STRING (target_username)
@router.get("/open_single_chat/{target_username}")
async def open_single_chat(
    target_username: str,
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(current_user),  # <--- 'user' viene de tu modelo 'User' con iD, username, ...
):
    db = DatabaseSingleton()

    # 1) Convertir target_username => ID entero en la BD
    user2_id = find_user_by_username(db, target_username)
    if not user2_id:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró usuario con username '{target_username}'"
        )

    # 2) Buscar si existe un chat 1:1 entre user.iD (actual) y user2_id (otro)
    existing_chat_id = find_single_chat(db, user.iD, user2_id)
    
    if existing_chat_id is None:
        # No existe chat => crear
        existing_chat_id = create_single_chat(db, user.iD, user2_id)
    
    # 3) Cargar últimos mensajes
    mensajes = fetch_chat_messages(db, existing_chat_id, limit, offset)

    return {
        "chat_id": existing_chat_id,
        "messages": mensajes
    }



@router.get("/ison")
async def is_active():
    return "Yes, I'm working ma'a faka"

"""
A continuación, los módulos para buscar usuarios
desde el navbar.
"""

@router.get("/me")
async def me(user: User = Depends(current_user)):
    # Ya lo chequé con el thunderClient y sí funcionó.
    # Aún así me interesa hacer un script de JS para adaptarme
    return user



@router.get("/search_user_navbar/{terminoBusqueda}")
async def search_user(user: User = Depends(current_user), terminoBusqueda: str = None):
    db = DatabaseSingleton()  # Obtén la instancia del Singleton
    like_pattern = f"%{terminoBusqueda}%"
    
    query = """
        SELECT username 
        FROM Usuarios 
        WHERE username LIKE ?
    """
    try:
        resultados = db.fetch_query(query, (like_pattern,))
        # resultados es probable que sea una lista de diccionarios [{ "username": "..." }, ...]
        # o lista de tuplas [("user1",), ("user2",), ...] dependiendo de tu row_factory.
        
        # 1) Verifica si es lista de diccionarios:
        #    usernames_list = [ row["username"] for row in resultados ]
        #
        # 2) Si en cambio es lista de tuplas (porque no configuraste row_factory a dict),
        #    haz:  usernames_list = [ row[0] for row in resultados ]
        
        # Ajusta esto según tu caso real:
        usernames_list = [row["username"] for row in resultados]  
        
        return usernames_list
    except Exception as e:
        return {"error": str(e)}

@router.get("/my_chats", tags=["Chats"])
async def get_my_chats(limit: int = 10, offset: int = 0, user: User = Depends(current_user)):
    """
    Retorna los chats en los que el usuario actual es miembro, ordenados por la última actividad.
    - Para chats individuales (is_group = 0): retorna el username del otro participante.
    - Para chats grupales (is_group = 1): retorna 'Grupo Chat' (puedes ajustar si agregas nombre de grupo).
    La paginación se maneja con LIMIT y OFFSET.
    """
    db = DatabaseSingleton()
    query = """
    SELECT c.chat_id, c.is_group, c.last_activity,
      CASE 
        WHEN c.is_group = 0 THEN (
            SELECT username FROM Usuarios 
            WHERE Id_Usuarios = (
                SELECT user_id FROM chat_members 
                WHERE chat_id = c.chat_id AND user_id != ?
                LIMIT 1
            )
        )
        ELSE 'Grupo Chat'
      END AS chat_name
    FROM chats c
    INNER JOIN chat_members cm ON c.chat_id = cm.chat_id
    WHERE cm.user_id = ?
    ORDER BY c.last_activity DESC
    LIMIT ? OFFSET ?
    """
    results = db.fetch_query(query, (user.iD, user.iD, limit, offset))
    return {"chats": results}

@router.get("/get_chat/{chat_id}", tags=["Chats"])
async def get_chat(
    chat_id: int,
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(current_user)
):
    """
    Retorna los mensajes del chat especificado por chat_id con paginación.
    Primero verifica que el usuario autenticado sea miembro del chat.
    """
    db = DatabaseSingleton()
    
    # Verificar que el usuario pertenezca al chat
    member_check = db.fetch_query(
        """
        SELECT COUNT(*) as count 
        FROM chat_members 
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat_id, user.iD)
    )
    
    if member_check is None or member_check[0]["count"] == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permiso para acceder a este chat."
        )
    
    # Obtener los mensajes usando la función fetch_chat_messages
    messages = fetch_chat_messages(db, chat_id, limit, offset)
    
    return {"chat_id": chat_id, "messages": messages}


# Función antes de implementar Singleton
@router.get("/search_user_navbar2/{terminoBusqueda}")
async def search_user2(user: User = Depends(current_user), terminoBusqueda: str = None):
    # Aquí la lógica para buscar usuarios en la base de datos
    # con respecto al nombre de usuario o código.
    # Aunque la parte del código va después.

    """
    ¿Qué input necesito?
    - Por el momento solo el nombre de usuario (username)
      del contacto a buscar.

    OUTPUT:
    - Arrojar los usuarios
    """
    
    try:
        query = searchNavbarUser(terminoBusqueda)
    except:
        return "Error de memoria, intente nuevamente"
    finally:
         return query
