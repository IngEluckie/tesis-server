# websocket.py
"""
Handle live messaging through WebSocket connections.
Maintains chat subscriptions, publishes events via Redis and
broadcasts updates to connected clients.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from contextlib import suppress
import logging
from typing import Any, Dict, Set

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from dotenv import load_dotenv
import redis
from redis.asyncio import Redis as AsyncRedis
from jose import JWTError, jwt

from routers.auth import (
    ALGORITHM,
    SECRET,
    User,
    current_user,
    search_user,
)
from database.singleton import Database

load_dotenv(override=True)

router_websockets = APIRouter(prefix="/websockets")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PUBSUB_CHANNEL = os.getenv("CHAT_PUBSUB_CHANNEL", "chat_events")

redis_sync = redis.Redis.from_url(REDIS_URL, decode_responses=True)
redis_async = AsyncRedis.from_url(REDIS_URL, decode_responses=True)


def _membership_key(user_id: int) -> str:
    return f"connection:{user_id}"

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Keeps track of active websocket connections per user,
    the chat subscriptions for each user and a single Redis
    pub/sub listener per process.
    """

    def __init__(self) -> None:
        self._connections: Dict[int, Dict[int, WebSocket]] = {}
        self._subscriptions: Dict[int, Set[int]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._listener_task: asyncio.Task | None = None

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            user_connections = self._connections.setdefault(user_id, {})
            user_connections[id(websocket)] = websocket
            self._subscriptions.setdefault(user_id, set())
        await self._ensure_listener()

    async def disconnect(self, user_id: int, websocket: WebSocket | None = None) -> None:
        async with self._lock:
            if websocket is None:
                self._connections.pop(user_id, None)
                self._subscriptions.pop(user_id, None)
                return

            connections = self._connections.get(user_id)
            if connections:
                connections.pop(id(websocket), None)
                if not connections:
                    self._connections.pop(user_id, None)
                    self._subscriptions.pop(user_id, None)

    async def subscribe(self, user_id: int, chat_id: int) -> None:
        async with self._lock:
            chats = self._subscriptions.setdefault(user_id, set())
            chats.add(chat_id)

    async def unsubscribe(self, user_id: int, chat_id: int) -> None:
        async with self._lock:
            chats = self._subscriptions.get(user_id)
            if chats and chat_id in chats:
                chats.remove(chat_id)
                if not chats:
                    self._subscriptions.pop(user_id, None)

    async def has_connection(self, user_id: int) -> bool:
        async with self._lock:
            return bool(self._connections.get(user_id))

    async def publish_event(self, event: Dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        await redis_async.publish(PUBSUB_CHANNEL, payload)

    async def broadcast_event(self, chat_id: int, event: Dict[str, Any]) -> None:
        async with self._lock:
            recipients: list[WebSocket] = []
            for uid, connections in self._connections.items():
                if chat_id in self._subscriptions.get(uid, set()):
                    recipients.extend(connections.values())

        for ws in recipients:
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.send_json(event)

    async def _ensure_listener(self) -> None:
        if self._listener_task and not self._listener_task.done():
            return
        self._listener_task = asyncio.create_task(self._run_pubsub_listener())

    async def _run_pubsub_listener(self) -> None:
        pubsub = redis_async.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(PUBSUB_CHANNEL)
        try:
            async for message in pubsub.listen():
                if not message:
                    continue
                data = message.get("data")
                if not data:
                    continue
                await self._handle_pubsub_message(data)
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.close()

    async def _handle_pubsub_message(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return

        if event.get("type") == "chat.message":
            chat_id = event.get("chat_id")
            if isinstance(chat_id, int):
                await self.broadcast_event(chat_id, event)


manager = ConnectionManager()


async def _user_is_member(chat_id: int, user_id: int) -> bool:
    db = Database()
    rows = db.fetch_query(
        """
        SELECT 1
        FROM chat_members
        WHERE chat_id = ? AND user_id = ?
        LIMIT 1
        """,
        (chat_id, user_id),
    )
    return bool(rows)


async def _handle_join_chat(user_id: int, chat_id: int, websocket: WebSocket) -> None:
    if not await _user_is_member(chat_id, user_id):
        await websocket.send_json(
            {
                "type": "chat.error",
                "chat_id": chat_id,
                "error": "No tienes permiso para unirte a este chat.",
            }
        )
        return
    await manager.subscribe(user_id, chat_id)
    await websocket.send_json({"type": "chat.joined", "chat_id": chat_id})


async def _handle_leave_chat(user_id: int, chat_id: int, websocket: WebSocket) -> None:
    await manager.unsubscribe(user_id, chat_id)
    await websocket.send_json({"type": "chat.left", "chat_id": chat_id})


async def _handle_send_message(
    user_id: int,
    chat_id: int,
    content: str,
    websocket: WebSocket,
) -> None:
    if not await _user_is_member(chat_id, user_id):
        await websocket.send_json(
            {
                "type": "chat.error",
                "chat_id": chat_id,
                "error": "No tienes permiso para enviar mensajes en este chat.",
            }
        )
        return

    clean_content = content.strip()
    if not clean_content:
        await websocket.send_json(
            {
                "type": "chat.error",
                "chat_id": chat_id,
                "error": "El mensaje no puede estar vacío.",
            }
        )
        return

    db = Database()
    from routers.chats import create_message  # Lazy import to avoid circular dependency

    message = create_message(db, chat_id, user_id, clean_content)
    event = _build_chat_message_event(message)
    await manager.publish_event(event)
    await websocket.send_json({"type": "chat.sent", "chat_id": chat_id, "message_id": message["message_id"]})


def _build_chat_message_event(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "chat.message",
        "chat_id": message["chat_id"],
        "message": message,
    }


@router_websockets.get("/ison")
async def ison():
    return {"message": "Yeah! I'm on!"}


@router_websockets.post("/connection")
async def create_or_confirm_connection(user: User = Depends(current_user)):
    try:
        user_id = user.user_id
        key = _membership_key(user_id)

        if not redis_sync.exists(key):
            connection_info = {
                "status": "connected",
                "username": user.username,
                "name": user.name or "",
                "email": user.email or "",
            }
            redis_sync.hset(key, mapping=connection_info)
            message = "Connection created"
        else:
            message = "Connection already registered"

        has_websocket = await manager.has_connection(user_id)

        return {
            "status": "ok",
            "message": message,
            "user_id": user_id,
            "has_websocket": has_websocket,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {exc}",
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

    await websocket.accept()
    await manager.connect(user_id, websocket)

    redis_sync.hset(_membership_key(user_id), "status", "connected")

    try:
        while True:
            message = await websocket.receive_text()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "chat.error", "error": "Formato inválido."})
                continue

            action = payload.get("type") or payload.get("action")
            if not action:
                await websocket.send_json({"type": "chat.error", "error": "Acción no especificada."})
                continue

            if action in {"ping", "heartbeat"}:
                await websocket.send_json({"type": "pong"})
                continue

            chat_id = payload.get("chat_id")
            if action in {"join", "join_chat"}:
                if isinstance(chat_id, int):
                    await _handle_join_chat(user_id, chat_id, websocket)
                else:
                    await websocket.send_json({"type": "chat.error", "error": "chat_id inválido."})
            elif action in {"leave", "leave_chat"}:
                if isinstance(chat_id, int):
                    await _handle_leave_chat(user_id, chat_id, websocket)
                else:
                    await websocket.send_json({"type": "chat.error", "error": "chat_id inválido."})
            elif action in {"send", "send_message"}:
                if not isinstance(chat_id, int):
                    await websocket.send_json({"type": "chat.error", "error": "chat_id inválido."})
                    continue
                await _handle_send_message(user_id, chat_id, payload.get("content", ""), websocket)
            else:
                await websocket.send_json({"type": "chat.error", "error": f"Acción desconocida: {action}"})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(user_id, websocket)
        redis_sync.hset(_membership_key(user_id), "status", "disconnected")


async def notify_new_message(message: Dict[str, Any]) -> None:
    """
    Permite que otras rutas (REST) publiquen un evento de mensaje nuevo.
    """
    event = _build_chat_message_event(message)
    await manager.publish_event(event)


__all__ = ["router_websockets", "notify_new_message"]
