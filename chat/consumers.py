import json
import re
from urllib.parse import urlparse

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from .models import Channel, FriendRequest, Message, PrivateMessage, Server

User = get_user_model()


def _sanitize_group_token(value: str, fallback: str = "default") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", (value or "").strip())
    return cleaned[:80] if cleaned else fallback


def _is_valid_media_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    value = url.strip()
    if value.startswith("/media/") or value.startswith("media/"):
        return True
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _normalize_attachment_type(att_type: str, att_url: str) -> str:
    allowed = {"image", "audio", "file", "video"}
    if att_type in allowed:
        return att_type
    lower_url = (att_url or "").lower()
    if any(lower_url.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if any(lower_url.endswith(ext) for ext in (".mp3", ".ogg", ".wav", ".webm", ".m4a")):
        return "audio"
    return "file"


def _get_user_avatar(user) -> str:
    profile = getattr(user, "profile", None)
    if profile:
        if getattr(profile, "avatar", None):
            try:
                return profile.avatar.url
            except Exception:
                pass
        if getattr(profile, "music_cover_file", None):
            try:
                return profile.music_cover_file.url
            except Exception:
                pass
        cover_url = getattr(profile, "music_cover_url", "") or ""
        if isinstance(cover_url, str) and _is_valid_media_url(cover_url):
            return cover_url.strip()
    return ""


def _model_field_names(model_cls) -> set:
    return {f.name for f in model_cls._meta.get_fields()}


class ChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket para canais isolados por servidor + canal:
    Ex.: /ws/chat/<room_slug>/?server=<server_id>&channel=<channel_name>
    """

    HISTORY_LIMIT = 50

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return

        self.user = user
        self.room_slug = _sanitize_group_token(
            self.scope["url_route"]["kwargs"].get("room_slug", "geral"),
            fallback="geral",
        )

        query_params = self._parse_query_string()
        raw_server = query_params.get("server") or "home"
        raw_channel = query_params.get("channel") or self.room_slug

        self.server_id = _sanitize_group_token(raw_server, fallback="home")
        self.channel_name_slug = _sanitize_group_token(raw_channel, fallback="geral")
        self.group_name = f"kiwi_srv_{self.server_id}_ch_{self.channel_name_slug}"
        self.presence_group = f"kiwi_presence_srv_{self.server_id}"

        channel_obj, allowed = await self._resolve_and_authorize_channel(
            raw_server=raw_server,
            server_token=self.server_id,
            raw_channel=raw_channel,
            channel_token=self.channel_name_slug,
        )
        if not allowed or channel_obj is None:
            await self.close(code=4003)
            return

        self.channel_obj = channel_obj

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.channel_layer.group_add(self.presence_group, self.channel_name)
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
                "avatar_url": await database_sync_to_async(_get_user_avatar)(self.user),
                "server_id": self.server_id,
                "channel": self.channel_name_slug,
            },
        )

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if hasattr(self, "presence_group") and hasattr(self, "user"):
            await self.channel_layer.group_discard(self.presence_group, self.channel_name)
            try:
                await self.channel_layer.group_send(
                    self.presence_group,
                    {
                        "type": "presence_update",
                        "username": self.user.username,
                        "status": "offline",
                        "avatar_url": await database_sync_to_async(_get_user_avatar)(self.user),
                        "server_id": getattr(self, "server_id", "home"),
                        "channel": getattr(self, "channel_name_slug", "geral"),
                    },
                )
            except Exception:
                pass

    def _parse_query_string(self) -> dict:
        raw_qs = self.scope.get("query_string", b"").decode("utf-8", errors="ignore")
        params = {}
        for part in raw_qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip()
        return params

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return

        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return

        msg_type = payload.get("type") or "chat_message"

        if msg_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
            return

        if msg_type == "delete_message":
            msg_id = payload.get("id")
            if msg_id:
                deleted = await self._delete_message(msg_id, self.user.id)
                if deleted:
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

        content = (payload.get("message") or payload.get("content") or "").strip()
        attachment_url = (payload.get("attachment_url") or "").strip()
        attachment_name = (payload.get("attachment_name") or "").strip()[:255]
        attachment_type = _normalize_attachment_type(
            payload.get("attachment_type") or "",
            attachment_url,
        )

        if attachment_url and not _is_valid_media_url(attachment_url):
            attachment_url = ""
            attachment_type = ""
            attachment_name = ""

        if not content and not attachment_url:
            return

        raw_avatar = (payload.get("avatar_url") or payload.get("avatar") or "").strip()
        safe_avatar = (
            raw_avatar
            if _is_valid_media_url(raw_avatar)
            else await database_sync_to_async(_get_user_avatar)(self.user)
        )

        msg_id = payload.get("id") or f"msg_{int(timezone.now().timestamp() * 1000)}"
        time_str = payload.get("time") or timezone.localtime(timezone.now()).strftime("%H:%M")
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

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat_message",
                "id": saved_msg.id if saved_msg else msg_id,
                "client_id": client_id,
                "sender": self.user.username,
                "username": self.user.username,
                "message": content,
                "time": time_str,
                "server_id": self.server_id,
                "channel": self.channel_name_slug,
                "avatar_url": safe_avatar,
                "attachment_url": attachment_url,
                "attachment_type": attachment_type if attachment_url else "",
                "attachment_name": attachment_name if attachment_url else "",
            },
        )

    async def chat_message(self, event):
        if event.get("server_id") != self.server_id or event.get("channel") != self.channel_name_slug:
            return
        await self.send(
            text_data=json.dumps(
                {
                    "type": "chat_message",
                    "id": event.get("id"),
                    "client_id": event.get("client_id", ""),
                    "sender": event.get("sender"),
                    "username": event.get("username"),
                    "message": event.get("message", ""),
                    "time": event.get("time", ""),
                    "server_id": event.get("server_id"),
                    "channel": event.get("channel"),
                    "avatar_url": event.get("avatar_url", ""),
                    "attachment_url": event.get("attachment_url", ""),
                    "attachment_type": event.get("attachment_type", ""),
                    "attachment_name": event.get("attachment_name", ""),
                }
            )
        )

    async def message_deleted(self, event):
        if event.get("server_id") != self.server_id or event.get("channel") != self.channel_name_slug:
            return
        await self.send(
            text_data=json.dumps(
                {
                    "type": "delete_message",
                    "id": event.get("id"),
                    "server_id": event.get("server_id"),
                    "channel": event.get("channel"),
                }
            )
        )

    async def presence_update(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "presence_update",
                    "username": event.get("username"),
                    "status": event.get("status", "online"),
                    "avatar_url": event.get("avatar_url", ""),
                    "server_id": event.get("server_id"),
                    "channel": event.get("channel"),
                }
            )
        )

    @database_sync_to_async
    def _resolve_and_authorize_channel(self, raw_server: str, server_token: str, raw_channel: str, channel_token: str):
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
            server, _ = Server.objects.get_or_create(
                name="home",
                defaults={"owner": self.user},
            )
            server.members.add(self.user)
        else:
            is_owner = getattr(server, "owner_id", None) == self.user.id
            is_member = server.members.filter(pk=self.user.id).exists()
            is_public = bool(getattr(server, "is_public", False)) or server.name.lower() == "home"
            if not (is_owner or is_member or is_public or self.user.is_staff):
                return None, False
            if not is_member:
                server.members.add(self.user)

        clean_ch = (raw_channel or "").strip().lstrip("#")[:100]
        channel = (
            Channel.objects.filter(server=server, name__iexact=clean_ch).first()
            or Channel.objects.filter(server=server, name__iexact=channel_token).first()
        )
        if channel is None:
            ch_name = (clean_ch or channel_token or "geral")[:100]
            channel, _ = Channel.objects.get_or_create(
                server=server,
                name=ch_name,
                defaults={"channel_type": "text"},
            )
        return channel, True

    @database_sync_to_async
    def _get_recent_messages(self, channel, limit: int = 50) -> list:
        qs = (
            Message.objects.filter(channel=channel)
            .select_related("user", "user__profile")
            .order_by("-id")[:limit]
        )
        items = []
        for msg in reversed(list(qs)):
            created = getattr(msg, "timestamp", None) or getattr(msg, "created_at", None)
            time_str = timezone.localtime(created).strftime("%H:%M") if created else ""
            att_url = getattr(msg, "attachment_url", "") or ""
            if not att_url and getattr(msg, "attachment", None):
                try:
                    att_url = msg.attachment.url
                except Exception:
                    att_url = ""
            items.append(
                {
                    "id": msg.id,
                    "client_id": getattr(msg, "client_id", "") or "",
                    "sender": msg.user.username,
                    "username": msg.user.username,
                    "message": msg.content or "",
                    "time": time_str,
                    "server_id": self.server_id,
                    "channel": self.channel_name_slug,
                    "avatar_url": _get_user_avatar(msg.user),
                    "attachment_url": att_url,
                    "attachment_type": getattr(msg, "attachment_type", "") or "",
                    "attachment_name": getattr(msg, "attachment_name", "") or "",
                }
            )
        return items

    @database_sync_to_async
    def _save_message(self, user, channel, content, attachment_url="", attachment_type="", attachment_name="", client_id=""):
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
    def _delete_message(self, msg_id, user_id) -> bool:
        try:
            numeric_id = int(msg_id)
        except (TypeError, ValueError):
            deleted_count, _ = Message.objects.filter(client_id=str(msg_id), user_id=user_id).delete()
            return deleted_count > 0
        deleted_count, _ = Message.objects.filter(id=numeric_id, user_id=user_id).delete()
        return deleted_count > 0


class DMConsumer(AsyncWebsocketConsumer):
    """
    WebSocket para mensagens diretas (DMs), notificações pessoais e sinalização de chamada entre dois utilizadores.
    """

    HISTORY_LIMIT = 50

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return

        self.user = user
        target_username = self.scope["url_route"]["kwargs"].get("username", "")
        self.target_user = await self._get_user_by_username(target_username)
        if not self.target_user:
            await self.close(code=4004)
            return

        allowed = await self._are_friends(self.user, self.target_user)
        if not allowed:
            await self.close(code=4003)
            return

        pair = sorted([self.user.username.lower(), self.target_user.username.lower()])
        self.room_group_name = f"kiwi_dm_{_sanitize_group_token(pair[0])}_{_sanitize_group_token(pair[1])}"
        self.target_personal_group = f"kiwi_user_{_sanitize_group_token(self.target_user.username.lower())}"
        self.self_personal_group = f"kiwi_user_{_sanitize_group_token(self.user.username.lower())}"

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.channel_layer.group_add(self.self_personal_group, self.channel_name)
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
        for grp in ("room_group_name", "self_personal_group"):
            if hasattr(self, grp):
                await self.channel_layer.group_discard(getattr(self, grp), self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return

        ev_type = payload.get("type") or "dm_message"

        if ev_type in ("call_signal", "call_hangup", "incoming_call"):
            call_event = {
                "type": "dm_call_event",
                "event_type": ev_type,
                "sender": self.user.username,
                "receiver": self.target_user.username,
                "signal": payload.get("signal"),
            }
            await self.channel_layer.group_send(self.room_group_name, call_event)
            if ev_type == "incoming_call":
                await self.channel_layer.group_send(self.target_personal_group, call_event)
            return

        content = (payload.get("message") or payload.get("content") or "").strip()
        attachment_url = (payload.get("attachment_url") or "").strip()
        attachment_name = (payload.get("attachment_name") or "").strip()[:255]
        attachment_type = _normalize_attachment_type(
            payload.get("attachment_type") or "",
            attachment_url,
        )

        if attachment_url and not _is_valid_media_url(attachment_url):
            attachment_url = ""
            attachment_type = ""
            attachment_name = ""

        if not content and not attachment_url:
            return

        raw_avatar = (payload.get("avatar_url") or payload.get("avatar") or "").strip()
        safe_avatar = (
            raw_avatar
            if _is_valid_media_url(raw_avatar)
            else await database_sync_to_async(_get_user_avatar)(self.user)
        )
        msg_id = payload.get("id") or f"dm_{int(timezone.now().timestamp() * 1000)}"
        time_str = payload.get("time") or timezone.localtime(timezone.now()).strftime("%H:%M")
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

        dm_event = {
            "type": "dm_message",
            "id": saved_dm.id if saved_dm else msg_id,
            "client_id": client_id,
            "sender": self.user.username,
            "receiver": self.target_user.username,
            "message": content,
            "time": time_str,
            "avatar_url": safe_avatar,
            "attachment_url": attachment_url,
            "attachment_type": attachment_type if attachment_url else "",
            "attachment_name": attachment_name if attachment_url else "",
        }
        await self.channel_layer.group_send(self.room_group_name, dm_event)
        await self.channel_layer.group_send(
            self.target_personal_group,
            {
                "type": "dm_notification",
                "id": dm_event["id"],
                "sender": self.user.username,
                "receiver": self.target_user.username,
                "preview": content[:120] if content else (attachment_name or "Anexo"),
                "time": time_str,
                "avatar_url": safe_avatar,
            },
        )

    async def dm_message(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "dm_message",
                    "id": event.get("id"),
                    "client_id": event.get("client_id", ""),
                    "sender": event.get("sender"),
                    "receiver": event.get("receiver"),
                    "message": event.get("message", ""),
                    "time": event.get("time", ""),
                    "avatar_url": event.get("avatar_url", ""),
                    "attachment_url": event.get("attachment_url", ""),
                    "attachment_type": event.get("attachment_type", ""),
                    "attachment_name": event.get("attachment_name", ""),
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
            created = getattr(dm, "timestamp", None) or getattr(dm, "created_at", None)
            time_str = timezone.localtime(created).strftime("%H:%M") if created else ""
            att_url = getattr(dm, "attachment_url", "") or ""
            if not att_url and getattr(dm, "attachment", None):
                try:
                    att_url = dm.attachment.url
                except Exception:
                    att_url = ""
            items.append(
                {
                    "id": dm.id,
                    "client_id": getattr(dm, "client_id", "") or "",
                    "sender": dm.sender.username,
                    "receiver": dm.receiver.username,
                    "message": dm.content or "",
                    "time": time_str,
                    "avatar_url": _get_user_avatar(dm.sender),
                    "attachment_url": att_url,
                    "attachment_type": getattr(dm, "attachment_type", "") or "",
                    "attachment_name": getattr(dm, "attachment_name", "") or "",
                }
            )
        return items

    @database_sync_to_async
    def _save_dm(self, sender, receiver, content, attachment_url="", attachment_type="", attachment_name="", client_id=""):
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