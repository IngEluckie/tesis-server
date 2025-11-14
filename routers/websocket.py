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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Set
import uuid

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

PROCESS_ID = f"{os.getpid()}-{uuid.uuid4().hex}"
CONNECTION_TTL_SECONDS = int(os.getenv("WEBSOCKET_CONNECTION_TTL", "120"))
HEARTBEAT_INTERVAL = int(os.getenv("WEBSOCKET_HEARTBEAT_INTERVAL", "30"))
IDLE_TIMEOUT = int(os.getenv("WEBSOCKET_IDLE_TIMEOUT", "90"))
PRESENCE_TOUCH_INTERVAL = int(os.getenv("WEBSOCKET_PRESENCE_TOUCH_INTERVAL", "15"))


def _membership_key(user_id: int) -> str:
    return f"connection:{user_id}"

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _presence_touch(user_id: int) -> None:
    key = _membership_key(user_id)
    timestamp = _utcnow_iso()
    try:
        pipe = redis_async.pipeline()
        pipe.hset(key, mapping={"last_seen": timestamp})
        pipe.expire(key, CONNECTION_TTL_SECONDS)
        await pipe.execute()
    except Exception as exc:
        logger.debug("No se pudo refrescar la presencia del usuario %s: %s", user_id, exc)


async def _presence_increment(user: User) -> Dict[str, Any]:
    key = _membership_key(user.user_id)
    timestamp = _utcnow_iso()
    payload = {
        "status": "connected",
        "username": user.username,
        "name": user.name or "",
        "email": user.email or "",
        "last_seen": timestamp,
    }
    connection_count: int | None = None
    try:
        connection_count = await redis_async.hincrby(key, "connection_count", 1)
        await redis_async.hset(key, mapping=payload)
        await redis_async.expire(key, CONNECTION_TTL_SECONDS)
    except Exception as exc:
        logger.warning("No se pudo registrar la conexión WebSocket para %s: %s", user.user_id, exc)
        connection_count = None
    return {
        "status": payload["status"],
        "last_seen": timestamp,
        "connection_count": connection_count,
    }


async def _presence_decrement(user_id: int, *, fallback_status: str) -> Dict[str, Any]:
    key = _membership_key(user_id)
    timestamp = _utcnow_iso()
    status = fallback_status
    connection_count: int | None = None
    try:
        connection_count = await redis_async.hincrby(key, "connection_count", -1)
        if connection_count < 0:
            connection_count = 0
            await redis_async.hset(key, "connection_count", 0)
        if connection_count > 0:
            status = "connected"
        await redis_async.hset(
            key,
            mapping={
                "status": status,
                "last_seen": timestamp,
            },
        )
        await redis_async.expire(key, CONNECTION_TTL_SECONDS)
    except Exception as exc:
        logger.warning("No se pudo registrar la desconexión de %s: %s", user_id, exc)
        connection_count = None
    return {
        "status": status,
        "last_seen": timestamp,
        "connection_count": connection_count,
    }


def _presence_event(user_id: int, presence: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "user.status",
        "user_id": user_id,
        "status": presence.get("status"),
        "last_seen": presence.get("last_seen"),
        "connection_count": presence.get("connection_count"),
    }


@dataclass
class ConnectionState:
    websocket: WebSocket
    last_activity: float = field(default_factory=time.monotonic)
    last_presence_refresh: float = field(default_factory=lambda: 0.0)
    pending_ping_id: str | None = None
    heartbeat_task: asyncio.Task | None = None
    disconnect_status: str = "disconnected"


class ConnectionManager:
    """
    Keeps track of active websocket connections per user,
    the chat subscriptions for each user and a single Redis
    pub/sub listener per process.
    """

    def __init__(self) -> None:
        self._connections: Dict[int, Dict[int, ConnectionState]] = {}
        self._subscriptions: Dict[int, Set[int]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._listener_task: asyncio.Task | None = None

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            user_connections = self._connections.setdefault(user_id, {})
            state = ConnectionState(websocket=websocket)
            user_connections[id(websocket)] = state
            self._subscriptions.setdefault(user_id, set())
        state.heartbeat_task = asyncio.create_task(self._heartbeat_loop(user_id, id(websocket)))
        await self._ensure_listener()

    async def disconnect(self, user_id: int, websocket: WebSocket | None = None) -> ConnectionState | None:
        removed_state: ConnectionState | None = None
        async with self._lock:
            if websocket is None:
                states = self._connections.pop(user_id, {})
                self._subscriptions.pop(user_id, None)
                for state in states.values():
                    if state.heartbeat_task:
                        state.heartbeat_task.cancel()
                return None

            connections = self._connections.get(user_id)
            if not connections:
                return None

            removed_state = connections.pop(id(websocket), None)

            if removed_state and removed_state.heartbeat_task:
                removed_state.heartbeat_task.cancel()

            if not connections:
                self._connections.pop(user_id, None)
                self._subscriptions.pop(user_id, None)
        return removed_state

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
        enriched_event = dict(event)
        enriched_event.setdefault("origin", PROCESS_ID)
        payload = json.dumps(enriched_event, ensure_ascii=False)

        redis_error: Exception | None = None
        try:
            await redis_async.publish(PUBSUB_CHANNEL, payload)
        except Exception as exc:  # Redis debe ser opcional para el broadcast
            redis_error = exc
            logger.warning("Fallo al publicar evento en Redis: %s", exc)

        event_type = enriched_event.get("type")
        if event_type == "chat.message":
            chat_id = enriched_event.get("chat_id")
            if isinstance(chat_id, int):
                await self.broadcast_event(chat_id, enriched_event)
        elif event_type == "user.status":
            await self.broadcast_all(enriched_event)

        if redis_error:
            logger.debug("Redis no disponible; se usó broadcast local para evento %s", event_type)

    async def broadcast_event(self, chat_id: int, event: Dict[str, Any]) -> None:
        async with self._lock:
            recipients: list[WebSocket] = []
            for uid, connections in self._connections.items():
                if chat_id in self._subscriptions.get(uid, set()):
                    recipients.extend(state.websocket for state in connections.values())

        for ws in recipients:
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.send_json(event)

    async def broadcast_all(self, event: Dict[str, Any]) -> None:
        async with self._lock:
            recipients = [state.websocket for connections in self._connections.values() for state in connections.values()]

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

        if event.get("origin") == PROCESS_ID:
            return

        if event.get("type") == "chat.message":
            chat_id = event.get("chat_id")
            if isinstance(chat_id, int):
                await self.broadcast_event(chat_id, event)
        elif event.get("type") == "user.status":
            await self.broadcast_all(event)

    async def mark_activity(self, user_id: int, websocket: WebSocket, *, ping_id: str | None = None) -> None:
        should_refresh_presence = False
        async with self._lock:
            connections = self._connections.get(user_id, {})
            state = connections.get(id(websocket))
            if not state:
                return
            if ping_id is not None:
                expected = state.pending_ping_id
                if expected and expected != ping_id:
                    logger.debug(
                        "Ping ID no coincide para el usuario %s: esperado %s, recibido %s",
                        user_id,
                        expected,
                        ping_id,
                    )
                state.pending_ping_id = None
            now = time.monotonic()
            state.last_activity = now
            if now - state.last_presence_refresh >= PRESENCE_TOUCH_INTERVAL:
                should_refresh_presence = True
                state.last_presence_refresh = now

        if should_refresh_presence:
            await _presence_touch(user_id)

    async def _heartbeat_loop(self, user_id: int, connection_key: int) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                async with self._lock:
                    connections = self._connections.get(user_id)
                    if not connections:
                        return
                    state = connections.get(connection_key)
                    if not state:
                        return
                    websocket = state.websocket
                    last_activity = state.last_activity
                now = time.monotonic()
                if now - last_activity > IDLE_TIMEOUT:
                    state.disconnect_status = "inactive"
                    with suppress(RuntimeError, WebSocketDisconnect):
                        await websocket.close(code=4408)
                    return

                ping_id = uuid.uuid4().hex
                async with self._lock:
                    connections = self._connections.get(user_id)
                    if not connections:
                        return
                    state = connections.get(connection_key)
                    if not state:
                        return
                    state.pending_ping_id = ping_id
                with suppress(RuntimeError, WebSocketDisconnect):
                    await websocket.send_json(
                        {
                            "type": "system.ping",
                            "ping_id": ping_id,
                            "server_timestamp": _utcnow_iso(),
                        }
                    )
        except asyncio.CancelledError:
            raise


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

        is_new = not redis_sync.exists(key)
        connection_info = {
            "status": "connecting",
            "username": user.username,
            "name": user.name or "",
            "email": user.email or "",
            "last_seen": _utcnow_iso(),
        }
        pipe = redis_sync.pipeline()
        pipe.hset(key, mapping=connection_info)
        pipe.hsetnx(key, "connection_count", 0)
        pipe.expire(key, CONNECTION_TTL_SECONDS)
        pipe.execute()
        message = "Connection created" if is_new else "Connection refreshed"

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

    presence = await _presence_increment(user)
    if presence.get("status"):
        await manager.publish_event(_presence_event(user_id, presence))

    try:
        while True:
            message = await websocket.receive_text()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "chat.error", "error": "Formato inválido."})
                continue
            action_value = payload.get("type") or payload.get("action")
            if not action_value or not isinstance(action_value, str):
                await websocket.send_json({"type": "chat.error", "error": "Acción no especificada."})
                continue
            action = action_value.lower()

            if action in {"system.pong", "pong"}:
                await manager.mark_activity(user_id, websocket, ping_id=payload.get("ping_id"))
                await websocket.send_json(
                    {
                        "type": "system.pong",
                        "ping_id": payload.get("ping_id"),
                        "server_timestamp": _utcnow_iso(),
                        "connection_ttl": CONNECTION_TTL_SECONDS,
                    }
                )
                continue

            if action in {"ping", "heartbeat", "system.ping"}:
                await manager.mark_activity(user_id, websocket)
                await websocket.send_json(
                    {
                        "type": "system.pong",
                        "ping_id": payload.get("ping_id") or uuid.uuid4().hex,
                        "server_timestamp": _utcnow_iso(),
                        "connection_ttl": CONNECTION_TTL_SECONDS,
                    }
                )
                continue
            await manager.mark_activity(user_id, websocket)

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
        state = await manager.disconnect(user_id, websocket)
        disconnect_status = state.disconnect_status if state else "disconnected"
        presence = await _presence_decrement(user_id, fallback_status=disconnect_status)
        if presence.get("status"):
            await manager.publish_event(_presence_event(user_id, presence))


async def notify_new_message(message: Dict[str, Any]) -> None:
    """
    Permite que otras rutas (REST) publiquen un evento de mensaje nuevo.
    """
    event = _build_chat_message_event(message)
    await manager.publish_event(event)


__all__ = ["router_websockets", "notify_new_message"]
