# websocket.py

"""
TO HABDLE LIVE CONNECTION THROUGH TCP.
SAVES LIST OF CONNECTIONS
"""

# Import libraries
from fastapi import APIRouter, HTTPException, Depends, status, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
import os
import redis
from jose import JWTError, jwt

from typing import Any, Dict

# Import modules
from routers.auth import User, current_user, SECRET, ALGORITHM, search_user

router_websockets = APIRouter(prefix="/websockets")
r = redis.Redis(host='localhost', port=6379, db=0)

load_dotenv(override= True)

# In-memory registry to keep websocket instances around the current process.
active_connections: Dict[str, Dict[str, Any]] = {}

@router_websockets.get("/ison")
async def ison():
    return {"message": "Yeah! I'm on!"}

@router_websockets.post("/connection")
async def create_or_confirm_connection(user: User = Depends(current_user)):
    try:
        user_id = user.user_id
        key = f"connection:{user_id}"

        if not r.exists(key):
            connection_info = {
                "status": "connected",
                "username": user.username,
                "name": user.name or "",
                "email": user.email or "",
                "websocket": "",
            }
            r.hset(key, mapping={k: v for k, v in connection_info.items() if k != "websocket"})
            active_connections[key] = connection_info
            message = "Connection created"
        else:
            if key not in active_connections:
                stored = r.hgetall(key)
                active_connections[key] = {
                    "status": stored.get(b"status", b"").decode("utf-8"),
                    "username": stored.get(b"username", b"").decode("utf-8"),
                    "name": stored.get(b"name", b"").decode("utf-8"),
                    "email": stored.get(b"email", b"").decode("utf-8"),
                    "websocket": "",
                }
            message = "Connection already registered"

        in_memory_ws = active_connections[key].get("websocket")
        print(f"Usuario agregado: {user.username}")
        return {
            "status": "ok",
            "message": message,
            "user_id": user_id,
            "has_websocket": bool(in_memory_ws),
        }
    except HTTPException:
        raise
    except Exception as e:
        # Generic error creating/checking the connection
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {e}"
        )


@router_websockets.websocket("/connection")
async def websocket_connection(websocket: WebSocket, token: str | None = None):
    if not token:
        await websocket.close(code=4401)
        return

    try:
        payload = jwt.decode(token, SECRET, algorithms=[ALGORITHM])
        user_id_str = payload.get("sub")
        if user_id_str is None:
            await websocket.close(code=4401)
            return
        user_id = int(user_id_str)
    except (JWTError, ValueError, TypeError):
        await websocket.close(code=4401)
        return

    user = search_user(user_id)
    if not user:
        await websocket.close(code=4401)
        return

    key = f"connection:{user_id}"

    if not r.exists(key):
        # Reuse the REST helper to create the redis record.
        connection_info = {
            "status": "connected",
            "username": user.username,
            "name": user.name or "",
            "email": user.email or "",
            "websocket": "",
        }
        r.hset(key, mapping={k: v for k, v in connection_info.items() if k != "websocket"})
        active_connections[key] = connection_info

    await websocket.accept()

    connection_info = active_connections.setdefault(
        key,
        {
            "status": "connected",
            "username": user.username,
            "name": user.name or "",
            "email": user.email or "",
            "websocket": "",
        },
    )
    connection_info["websocket"] = websocket
    active_connections[key] = connection_info

    try:
        while True:
            data = await websocket.receive_text()
            # Echo back or handle message accordingly
            await websocket.send_text(data)
    except WebSocketDisconnect:
        connection_info["websocket"] = ""
        active_connections[key] = connection_info
        r.hset(key, "status", "disconnected")
