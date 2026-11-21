import json
import logging
import re
from urllib.parse import urlparse

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from .models import Channel, FriendRequest, Message, PrivateMessage, Server

logger = logging.getLogger(__name__)
User = get_user_model()

ALLOWED_CHAT_EVENT_TYPES = {"chat_message", "message", "ping", "delete_message"}
ALLOWED_DM_EVENT_TYPES = {
    "dm_message",
    "delete_dm",
    "call_signal",
    "call_hangup",
    "incoming_call",
    "ping",
}
ALLOWED_CALL_EVENT_TYPES = {"call_signal", "call_hangup", "incoming_call"}

MAX_MESSAGE_LENGTH = 4000
MAX_WS_PAYLOAD_BYTES = 65536


def sanitize_group_token(value: str, fallback: str = "default") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", (value or "").strip())
    return cleaned[:80] if cleaned else fallback


def build_channel_group_name(server_token: str, channel_token: str) -> str:
    return (
        f"kiwi_srv_{sanitize_group_token(server_token, 'home')}"
        f"_ch_{sanitize_group_token(channel_token, 'geral')}"
    )


def build_presence_group_name(server_token: str) -> str:
    return f"kiwi_presence_srv_{sanitize_group_token(server_token, 'home')}"


def build_dm_group_name(username_a: str, username_b: str) -> str:
    pair = sorted([(username_a or "").lower(), (username_b or "").lower()])
    return (
        f"kiwi_dm_{sanitize_group_token(pair[0], 'u1')}"
        f"_{sanitize_group_token(pair[1], 'u2')}"
    )


def build_user_personal_group_name(username: str) -> str:
    return f"kiwi_user_{sanitize_group_token((username or '').lower(), 'user')}"


def _is_valid_media_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    value = url.strip()
    if len(value) > 1000:
        return False
    if value.startswith("/media/") or value.startswith("media/"):
        return True
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _normalize_attachment_type(att_type: str, att_url: str) -> str:
    allowed = {"image", "audio", "file", "video", "document"}
    clean_type = (att_type or "").strip().lower()
    if clean_type in allowed:
        return "file" if clean_type == "document" else clean_type
    lower_url = (att_url or "").lower().split("?")[0]
    if any(lower_url.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if any(lower_url.endswith(ext) for ext in (".mp3", ".ogg", ".wav", ".webm", ".m4a")):
        return "audio"
    if any(lower_url.endswith(ext) for ext in (".mp4", ".mov", ".mkv")):
        return "video"
    return "file"


def _get_user_avatar(user) -> str:
    if not user:
        return ""
    profile = getattr(user, "profile", None)
    if profile and getattr(profile, "avatar", None):
        try:
            url = profile.avatar.url
            if _is_valid_media_url(url):
                return url
        except Exception:
            pass
    return ""


def _extract_model_attachment(msg_obj) -> tuple[str, str, str]:
    att_url = ""
    att_name = ""
    att_type = getattr(msg_obj, "attachment_type", "") or ""

    file_field = getattr(msg_obj, "attachment", None)
    if file_field:
        try:
            att_url = file_field.url or ""
            if file_field.name:
                att_name = file_field.name.split("/")[-1]
        except Exception:
            att_url = ""

    if not att_url:
        raw_url = getattr(msg_obj, "attachment_url", "") or ""
        if _is_valid_media_url(raw_url):
            att_url = raw_url.strip()

    if not att_name:
        att_name = (getattr(msg_obj, "attachment_name", "") or "").strip()[:255]

    if att_url and not att_type:
        att_type = _normalize_attachment_type("", att_url)

    return att_url, att_type, att_name


def _model_field_names(model_cls) -> set:
    return {f.name for f in model_cls._meta.get_fields()}


class ChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket para canais isolados por servidor + canal:
    Suporta tanto /ws/chat/<server_id>/<channel_name>/ quanto /ws/chat/<room_slug>/?server=<server_id>&channel=<channel_name>
    """

    HISTORY_LIMIT = 50

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.room_slug = "geral"
        self.server_id = "home"
        self.channel_name_slug = "geral"
        self.group_name = None
        self.presence_group = None
        self.channel_obj = None
        self.user_avatar = ""
        self._joined_groups = False

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            logger.warning("WSREJECT ChatConsumer: utilizador não autenticado.")
            await self.close(code=4001)
            return

        if not self.channel_layer:
            logger.error("WSREJECT ChatConsumer: Channel layer não configurado.")
            await self.close(code=4002)
            return

        self.user = user
        url_kwargs = self.scope.get("url_route", {}).get("kwargs", {})
        query_params = self._parse_query_string()

        raw_server = (
            url_kwargs.get("server_id")
            or query_params.get("server")
            or "home"
        )
        raw_slug = (
            url_kwargs.get("channel_name")
            or url_kwargs.get("room_slug")
            or url_kwargs.get("room_name")
            or query_params.get("channel")
            or "geral"
        )
        raw_channel = query_params.get("channel") or raw_slug

        self.room_slug = sanitize_group_token(raw_slug, fallback="geral")
        self.server_id = sanitize_group_token(str(raw_server), fallback="home")
        self.channel_name_slug = sanitize_group_token(str(raw_channel), fallback="geral")
        self.group_name = build_channel_group_name(self.server_id, self.channel_name_slug)
        self.presence_group = build_presence_group_name(self.server_id)

        channel_obj, allowed = await self._resolve_and_authorize_channel(
            raw_server=str(raw_server),
            server_token=self.server_id,
            raw_channel=str(raw_channel),
            channel_token=self.channel_name_slug,
        )
        if not allowed or channel_obj is None:
            logger.warning(
                "WSREJECT ChatConsumer: acesso negado para user=%s em server=%s channel=%s",
                user.username,
                raw_server,
                raw_channel,
            )
            await self.close(code=4003)
            return

        self.channel_obj = channel_obj
        self.user_avatar = await database_sync_to_async(_get_user_avatar)(self.user)

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.channel_layer.group_add(self.presence_group, self.channel_name)
        self._joined_groups = True

        await self.accept()

        history = await self._get_recent_messages(self.channel_obj, limit=self.HISTORY_LIMIT)
        if history:
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "chat_history",
                        "server_id": self.server_id,
                        "channel": self.channel_name_slug,
                        "messages": history,
                    }
                )
            )

        await self.channel_layer.group_send(
            self.presence_group,
            {
                "type": "presence_update",
                "username": self.user.username,
                "status": "online",
                "avatar_url": self.user_avatar,
                "server_id": self.server_id,
                "channel": self.channel_name_slug,
            },
        )

    async def disconnect(self, close_code):
        if not self.channel_layer or not self._joined_groups:
            return

        if self.group_name:
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                pass

        if self.presence_group:
            try:
                await self.channel_layer.group_discard(self.presence_group, self.channel_name)
            except Exception:
                pass

            if self.user and getattr(self.user, "username", None):
                try:
                    await self.channel_layer.group_send(
                        self.presence_group,
                        {
                            "type": "presence_update",
                            "username": self.user.username,
                            "status": "offline",
                            "avatar_url": self.user_avatar,
                            "server_id": self.server_id,
                            "channel": self.channel_name_slug,
                        },
                    )
                except Exception:
                    pass

        self._joined_groups = False

    def _parse_query_string(self) -> dict:
        raw_qs = self.scope.get("query_string", b"").decode("utf-8", errors="ignore")
        params = {}
        for part in raw_qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip()
        return params

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data or len(text_data.encode("utf-8", errors="ignore")) > MAX_WS_PAYLOAD_BYTES:
            return

        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return

        if not isinstance(payload, dict):
            return

        msg_type = payload.get("type") or "chat_message"
        if msg_type not in ALLOWED_CHAT_EVENT_TYPES:
            return

        if msg_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
            return

        if msg_type == "delete_message":
            msg_id = payload.get("id") or payload.get("message_id")
            if msg_id is not None and str(msg_id).strip():
                deleted = await self._delete_message(
                    msg_id,
                    self.user.id,
                    bool(self.user.is_staff or self.user.is_superuser),
                )
                if deleted and self.channel_layer and self.group_name:
                    await self.channel_layer.group_send(
                        self.group_name,
                        {
                            "type": "message_deleted",
                            "id": msg_id,
                            "server_id": self.server_id,
                            "channel": self.channel_name_slug,
                        },
                    )
            return

        raw_content = payload.get("message") if "message" in payload else payload.get("content")
        content = (raw_content if isinstance(raw_content, str) else "").strip()[:MAX_MESSAGE_LENGTH]

        raw_att_url = payload.get("attachment_url")
        attachment_url = (raw_att_url if isinstance(raw_att_url, str) else "").strip()

        raw_att_name = payload.get("attachment_name")
        attachment_name = (raw_att_name if isinstance(raw_att_name, str) else "").strip()[:255]

        raw_att_type = payload.get("attachment_type")
        attachment_type = _normalize_attachment_type(
            raw_att_type if isinstance(raw_att_type, str) else "",
            attachment_url,
        )

        if attachment_url and not _is_valid_media_url(attachment_url):
            attachment_url = ""
            attachment_type = ""
            attachment_name = ""

        if not content and not attachment_url:
            return

        current_avatar = await database_sync_to_async(_get_user_avatar)(self.user)
        self.user_avatar = current_avatar

        msg_id = payload.get("id") or f"msg_{int(timezone.now().timestamp() * 1000)}"
        time_str = str(payload.get("time") or timezone.localtime(timezone.now()).strftime("%H:%M"))[:16]
        client_id = str(payload.get("client_id") or msg_id)[:100]

        saved_msg = await self._save_message(
            user=self.user,
            channel=self.channel_obj,
            content=content,
            attachment_url=attachment_url,
            attachment_type=attachment_type if attachment_url else "",
            attachment_name=attachment_name if attachment_url else "",
            client_id=client_id,
        )

        final_id = saved_msg.id if saved_msg else msg_id

        if self.channel_layer and self.group_name:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "chat_message",
                    "id": final_id,
                    "client_id": client_id,
                    "sender": self.user.username,
                    "username": self.user.username,
                    "message": content,
                    "time": time_str,
                    "server_id": self.server_id,
                    "channel": self.channel_name_slug,
                    "avatar_url": current_avatar,
                    "attachment_url": attachment_url,
                    "attachment_type": attachment_type if attachment_url else "",
                    "attachment_name": attachment_name if attachment_url else "",
                },
            )

    async def chat_message(self, event):
        if event.get("server_id") != self.server_id or event.get("channel") != self.channel_name_slug:
            return

        content = (event.get("message") or "").strip()
        attachment_url = (event.get("attachment_url") or "").strip()
        if not content and not attachment_url:
            return

        sender_name = (event.get("sender") or event.get("username") or "").strip()
        if not sender_name:
            return

        avatar_url = event.get("avatar_url") or event.get("avatarUrl") or ""
        await self.send(
            text_data=json.dumps(
                {
                    "type": "chat_message",
                    "id": event.get("id"),
                    "client_id": event.get("client_id", ""),
                    "sender": sender_name,
                    "username": sender_name,
                    "message": content,
                    "time": event.get("time", ""),
                    "server_id": event.get("server_id"),
                    "channel": event.get("channel"),
                    "avatar_url": avatar_url,
                    "attachment_url": attachment_url,
                    "attachment_type": event.get("attachment_type", ""),
                    "attachment_name": event.get("attachment_name", ""),
                }
            )
        )

    async def message_deleted(self, event):
        if event.get("server_id") != self.server_id or event.get("channel") != self.channel_name_slug:
            return
        deleted_id = event.get("id") or event.get("message_id")
        await self.send(
            text_data=json.dumps(
                {
                    "type": "delete_message",
                    "id": deleted_id,
                    "server_id": event.get("server_id"),
                    "channel": event.get("channel"),
                }
            )
        )

    async def presence_update(self, event):
        if event.get("server_id") and event.get("server_id") != self.server_id:
            return
        username = (event.get("username") or "").strip()
        if not username:
            return
        await self.send(
            text_data=json.dumps(
                {
                    "type": "presence_update",
                    "username": username,
                    "status": event.get("status", "online"),
                    "avatar_url": event.get("avatar_url", ""),
                    "server_id": event.get("server_id") or self.server_id,
                    "channel": event.get("channel") or self.channel_name_slug,
                }
            )
        )

    @database_sync_to_async
    def _resolve_and_authorize_channel(
        self, raw_server: str, server_token: str, raw_channel: str, channel_token: str
    ):
        server = None
        clean_srv = (raw_server or "").strip()
        if clean_srv.isdigit():
            server = Server.objects.filter(pk=int(clean_srv)).first()
        if server is None and clean_srv:
            server = Server.objects.filter(name__iexact=clean_srv).first()
        if server is None and server_token:
            server = Server.objects.filter(name__iexact=server_token).first()

        is_home_hub = clean_srv.lower() in ("", "home") or server_token.lower() == "home"

        if server is None:
            if not is_home_hub:
                return None, False
            server = Server.objects.filter(name__iexact="home").first()
            if server is None:
                try:
                    server = Server.objects.create(name="home", owner=self.user)
                except IntegrityError:
                    server = Server.objects.filter(name__iexact="home").first()
            if server is None:
                return None, False
            server.members.add(self.user)
        else:
            is_owner = getattr(server, "owner_id", None) == self.user.id
            is_member = server.members.filter(pk=self.user.id).exists()
            is_home = server.name.lower() == "home"
            if not (is_owner or is_member or is_home or self.user.is_staff or self.user.is_superuser):
                return None, False
            if not is_member:
                server.members.add(self.user)

        clean_ch = (raw_channel or "").strip().lstrip("#")[:100]
        channel = (
            Channel.objects.filter(server=server, name__iexact=clean_ch).first()
            if clean_ch
            else None
        ) or (
            Channel.objects.filter(server=server, name__iexact=channel_token).first()
            if channel_token
            else None
        )

        if channel is None:
            ch_name = (clean_ch or channel_token or "geral")[:100]
            channel = Channel.objects.filter(server=server, name__iexact=ch_name).first()
            if channel is None:
                try:
                    channel = Channel.objects.create(
                        server=server,
                        name=ch_name,
                        channel_type="text",
                    )
                except IntegrityError:
                    channel = Channel.objects.filter(server=server, name__iexact=ch_name).first()

        return channel, channel is not None

    @database_sync_to_async
    def _get_recent_messages(self, channel, limit: int = 50) -> list:
        qs = (
            Message.objects.filter(channel=channel)
            .select_related("user", "user__profile")
            .order_by("-id")[:limit]
        )
        items = []
        for msg in reversed(list(qs)):
            content = (msg.content or "").strip()
            att_url, att_type, att_name = _extract_model_attachment(msg)
            if not content and not att_url:
                continue

            created = getattr(msg, "timestamp", None) or getattr(msg, "created_at", None)
            time_str = timezone.localtime(created).strftime("%H:%M") if created else ""
            sender_name = msg.user.username if msg.user else ""
            if not sender_name:
                continue

            items.append(
                {
                    "id": msg.id,
                    "client_id": getattr(msg, "client_id", "") or "",
                    "sender": sender_name,
                    "username": sender_name,
                    "message": content,
                    "time": time_str,
                    "server_id": self.server_id,
                    "channel": self.channel_name_slug,
                    "avatar_url": _get_user_avatar(msg.user),
                    "attachment_url": att_url,
                    "attachment_type": att_type,
                    "attachment_name": att_name,
                }
            )
        return items

    @database_sync_to_async
    def _save_message(
        self,
        user,
        channel,
        content,
        attachment_url="",
        attachment_type="",
        attachment_name="",
        client_id="",
    ):
        fields = _model_field_names(Message)
        kwargs = {
            "user": user,
            "channel": channel,
            "content": content,
        }
        if "attachment_url" in fields:
            kwargs["attachment_url"] = attachment_url
        if "attachment_type" in fields:
            kwargs["attachment_type"] = attachment_type
        if "attachment_name" in fields:
            kwargs["attachment_name"] = attachment_name
        if "client_id" in fields:
            kwargs["client_id"] = client_id
        return Message.objects.create(**kwargs)

    @database_sync_to_async
    def _delete_message(self, msg_id, user_id, is_staff=False) -> bool:
        try:
            numeric_id = int(msg_id)
            qs = Message.objects.filter(id=numeric_id)
            if not is_staff:
                qs = qs.filter(user_id=user_id)
            deleted_count, _ = qs.delete()
            return deleted_count > 0
        except (TypeError, ValueError):
            qs = Message.objects.filter(client_id=str(msg_id)[:100])
            if not is_staff:
                qs = qs.filter(user_id=user_id)
            deleted_count, _ = qs.delete()
            return deleted_count > 0


class DMConsumer(AsyncWebsocketConsumer):
    """
    WebSocket para mensagens diretas (DMs), notificações pessoais e sinalização de chamada entre dois utilizadores.
    """

    HISTORY_LIMIT = 50

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.target_user = None
        self.room_group_name = None
        self.target_personal_group = None
        self.self_personal_group = None
        self.user_avatar = ""
        self._joined_groups = False

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return

        if not self.channel_layer:
            logger.error("Channel layer não configurado no DMConsumer.")
            await self.close(code=4002)
            return

        self.user = user
        target_username = self.scope.get("url_route", {}).get("kwargs", {}).get("username", "")
        self.target_user = await self._get_user_by_username(target_username)
        if not self.target_user:
            await self.close(code=4004)
            return

        if self.target_user.pk == self.user.pk:
            await self.close(code=4003)
            return

        allowed = await self._are_friends(self.user, self.target_user)
        if not allowed:
            await self.close(code=4003)
            return

        self.room_group_name = build_dm_group_name(self.user.username, self.target_user.username)
        self.target_personal_group = build_user_personal_group_name(self.target_user.username)
        self.self_personal_group = build_user_personal_group_name(self.user.username)
        self.user_avatar = await database_sync_to_async(_get_user_avatar)(self.user)

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.channel_layer.group_add(self.self_personal_group, self.channel_name)
        self._joined_groups = True

        await self.accept()

        history = await self._get_recent_dms(self.user, self.target_user, limit=self.HISTORY_LIMIT)
        if history:
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "dm_history",
                        "partner": self.target_user.username,
                        "messages": history,
                    }
                )
            )

    async def disconnect(self, close_code):
        if not self.channel_layer or not self._joined_groups:
            return
        for grp in (self.room_group_name, self.self_personal_group):
            if grp:
                try:
                    await self.channel_layer.group_discard(grp, self.channel_name)
                except Exception:
                    pass
        self._joined_groups = False

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data or len(text_data.encode("utf-8", errors="ignore")) > MAX_WS_PAYLOAD_BYTES:
            return

        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return

        if not isinstance(payload, dict):
            return

        ev_type = payload.get("type") or "dm_message"
        if ev_type not in ALLOWED_DM_EVENT_TYPES:
            return

        if ev_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
            return

        if ev_type in ALLOWED_CALL_EVENT_TYPES:
            signal_data = payload.get("signal")
            if isinstance(signal_data, (dict, list, str, int, float, bool)) or signal_data is None:
                call_event = {
                    "type": "dm_call_event",
                    "event_type": ev_type,
                    "sender": self.user.username,
                    "receiver": self.target_user.username,
                    "signal": signal_data,
                }
                if self.channel_layer and self.room_group_name:
                    await self.channel_layer.group_send(self.room_group_name, call_event)
                if ev_type == "incoming_call" and self.channel_layer and self.target_personal_group:
                    await self.channel_layer.group_send(self.target_personal_group, call_event)
            return

        if ev_type == "delete_dm":
            dm_id = payload.get("id") or payload.get("message_id")
            if dm_id is not None and str(dm_id).strip():
                deleted = await self._delete_dm(dm_id, self.user.id)
                if deleted and self.channel_layer and self.room_group_name:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "dm_deleted",
                            "id": dm_id,
                            "sender": self.user.username,
                            "receiver": self.target_user.username,
                        },
                    )
            return

        raw_content = payload.get("message") if "message" in payload else payload.get("content")
        content = (raw_content if isinstance(raw_content, str) else "").strip()[:MAX_MESSAGE_LENGTH]

        raw_att_url = payload.get("attachment_url")
        attachment_url = (raw_att_url if isinstance(raw_att_url, str) else "").strip()

        raw_att_name = payload.get("attachment_name")
        attachment_name = (raw_att_name if isinstance(raw_att_name, str) else "").strip()[:255]

        raw_att_type = payload.get("attachment_type")
        attachment_type = _normalize_attachment_type(
            raw_att_type if isinstance(raw_att_type, str) else "",
            attachment_url,
        )

        if attachment_url and not _is_valid_media_url(attachment_url):
            attachment_url = ""
            attachment_type = ""
            attachment_name = ""

        if not content and not attachment_url:
            return

        current_avatar = await database_sync_to_async(_get_user_avatar)(self.user)
        self.user_avatar = current_avatar

        msg_id = payload.get("id") or f"dm_{int(timezone.now().timestamp() * 1000)}"
        time_str = str(payload.get("time") or timezone.localtime(timezone.now()).strftime("%H:%M"))[:16]
        client_id = str(payload.get("client_id") or msg_id)[:100]

        saved_dm = await self._save_dm(
            sender=self.user,
            receiver=self.target_user,
            content=content,
            attachment_url=attachment_url,
            attachment_type=attachment_type if attachment_url else "",
            attachment_name=attachment_name if attachment_url else "",
            client_id=client_id,
        )

        final_id = saved_dm.id if saved_dm else msg_id

        dm_event = {
            "type": "dm_message",
            "id": final_id,
            "client_id": client_id,
            "sender": self.user.username,
            "receiver": self.target_user.username,
            "message": content,
            "time": time_str,
            "avatar_url": current_avatar,
            "attachment_url": attachment_url,
            "attachment_type": attachment_type if attachment_url else "",
            "attachment_name": attachment_name if attachment_url else "",
        }
        if self.channel_layer and self.room_group_name:
            await self.channel_layer.group_send(self.room_group_name, dm_event)
        if self.channel_layer and self.target_personal_group:
            await self.channel_layer.group_send(
                self.target_personal_group,
                {
                    "type": "dm_notification",
                    "id": final_id,
                    "sender": self.user.username,
                    "receiver": self.target_user.username,
                    "preview": content[:120] if content else (attachment_name or "Anexo"),
                    "time": time_str,
                    "avatar_url": current_avatar,
                },
            )

    async def dm_message(self, event):
        content = (event.get("message") or "").strip()
        attachment_url = (event.get("attachment_url") or "").strip()
        if not content and not attachment_url:
            return

        sender_name = (event.get("sender") or "").strip()
        if not sender_name:
            return

        avatar_url = event.get("avatar_url") or event.get("avatarUrl") or ""
        await self.send(
            text_data=json.dumps(
                {
                    "type": "dm_message",
                    "id": event.get("id"),
                    "client_id": event.get("client_id", ""),
                    "sender": sender_name,
                    "receiver": event.get("receiver"),
                    "message": content,
                    "time": event.get("time", ""),
                    "avatar_url": avatar_url,
                    "attachment_url": attachment_url,
                    "attachment_type": event.get("attachment_type", ""),
                    "attachment_name": event.get("attachment_name", ""),
                }
            )
        )

    async def dm_deleted(self, event):
        deleted_id = event.get("id") or event.get("message_id")
        await self.send(
            text_data=json.dumps(
                {
                    "type": "delete_dm",
                    "id": deleted_id,
                    "sender": event.get("sender"),
                    "receiver": event.get("receiver"),
                }
            )
        )

    async def dm_notification(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "dm_notification",
                    "id": event.get("id"),
                    "sender": event.get("sender"),
                    "receiver": event.get("receiver"),
                    "preview": event.get("preview", ""),
                    "time": event.get("time", ""),
                    "avatar_url": event.get("avatar_url", ""),
                }
            )
        )

    async def dm_call_event(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": event.get("event_type", "call_signal"),
                    "sender": event.get("sender"),
                    "receiver": event.get("receiver"),
                    "signal": event.get("signal"),
                }
            )
        )

    @database_sync_to_async
    def _get_user_by_username(self, username: str):
        if not username:
            return None
        return User.objects.filter(username__iexact=username.strip()).first()

    @database_sync_to_async
    def _are_friends(self, user_a, user_b) -> bool:
        return FriendRequest.are_friends(user_a, user_b)

    @database_sync_to_async
    def _get_recent_dms(self, user_a, user_b, limit: int = 50) -> list:
        qs = (
            PrivateMessage.objects.filter(
                Q(sender=user_a, receiver=user_b) | Q(sender=user_b, receiver=user_a)
            )
            .select_related("sender", "sender__profile", "receiver")
            .order_by("-id")[:limit]
        )
        items = []
        for dm in reversed(list(qs)):
            content = (dm.content or "").strip()
            att_url, att_type, att_name = _extract_model_attachment(dm)
            if not content and not att_url:
                continue

            created = getattr(dm, "timestamp", None) or getattr(dm, "created_at", None)
            time_str = timezone.localtime(created).strftime("%H:%M") if created else ""
            sender_name = dm.sender.username if dm.sender else ""
            if not sender_name:
                continue

            items.append(
                {
                    "id": dm.id,
                    "client_id": getattr(dm, "client_id", "") or "",
                    "sender": sender_name,
                    "receiver": dm.receiver.username if dm.receiver else "",
                    "message": content,
                    "time": time_str,
                    "avatar_url": _get_user_avatar(dm.sender),
                    "attachment_url": att_url,
                    "attachment_type": att_type,
                    "attachment_name": att_name,
                }
            )
        return items

    @database_sync_to_async
    def _save_dm(
        self,
        sender,
        receiver,
        content,
        attachment_url="",
        attachment_type="",
        attachment_name="",
        client_id="",
    ):
        fields = _model_field_names(PrivateMessage)
        kwargs = {
            "sender": sender,
            "receiver": receiver,
            "content": content,
        }
        if "attachment_url" in fields:
            kwargs["attachment_url"] = attachment_url
        if "attachment_type" in fields:
            kwargs["attachment_type"] = attachment_type
        if "attachment_name" in fields:
            kwargs["attachment_name"] = attachment_name
        if "client_id" in fields:
            kwargs["client_id"] = client_id
        return PrivateMessage.objects.create(**kwargs)

    @database_sync_to_async
    def _delete_dm(self, msg_id, user_id) -> bool:
        try:
            numeric_id = int(msg_id)
            deleted_count, _ = PrivateMessage.objects.filter(id=numeric_id, sender_id=user_id).delete()
            return deleted_count > 0
        except (TypeError, ValueError):
            deleted_count, _ = PrivateMessage.objects.filter(
                client_id=str(msg_id)[:100], sender_id=user_id
            ).delete()
            return deleted_count > 0
