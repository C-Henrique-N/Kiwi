import json
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone

from .models import Channel, DirectMessage, Message, MessageReaction, UserProfile


def build_user_personal_group_name(username):
    clean = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in (username or ""))
    return f"kiwi_user_{clean}"


def _is_kiwi_admin(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return bool(
        user.is_superuser
        or user.is_staff
        or (user.username or "").strip().lower() == "akiirapw"
    )


def get_display_name_and_avatar(user):
    display_name = user.username
    avatar_url = ""
    profile = UserProfile.objects.filter(user=user).first()
    if profile:
        custom_name = (getattr(profile, "display_name", "") or "").strip()
        if custom_name:
            display_name = custom_name
        try:
            avatar_url = profile.avatar.url if profile.avatar else ""
        except Exception:
            avatar_url = ""
    return display_name, avatar_url


def _format_time(msg):
    dt = getattr(msg, "timestamp", None) or getattr(msg, "created_at", None)
    if not dt:
        return ""
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local_dt.strftime("%H:%M")


def _serialize_ws_message(msg, channel_id=None):
    author = getattr(msg, "user", None) or getattr(msg, "sender", None)
    display_name, avatar_url = get_display_name_and_avatar(author)

    att_url = ""
    att_name = ""
    if msg.attachment:
        try:
            att_url = msg.attachment.url
            att_name = msg.attachment.name.split("/")[-1]
        except Exception:
            att_url = ""

    reactions = {}
    for r in msg.reactions.all():
        entry = reactions.setdefault(r.emoji, {"emoji": r.emoji, "count": 0, "users": []})
        entry["count"] += 1
        entry["users"].append(r.user.username)

    ts = _format_time(msg)
    return {
        "id": msg.id,
        "client_id": getattr(msg, "client_id", "") or "",
        "channel_id": int(channel_id if channel_id is not None else (msg.channel_id or 0)),
        "sender": author.username,
        "author": author.username,
        "display_name": display_name,
        "avatar_url": avatar_url,
        "message": msg.content or "",
        "content": msg.content or "",
        "text": msg.content or "",
        "attachment_url": att_url,
        "attachment_name": att_name,
        "attachment_type": msg.attachment_type or "",
        "timestamp": ts,
        "time": ts,
        "reactions": list(reactions.values()),
    }


def _serialize_ws_dm(dm):
    sender = getattr(dm, "sender", None) or getattr(dm, "user", None)
    receiver = getattr(dm, "receiver", None) or getattr(dm, "recipient", None)
    display_name, avatar_url = get_display_name_and_avatar(sender)

    att_url = ""
    att_name = ""
    if dm.attachment:
        try:
            att_url = dm.attachment.url
            att_name = dm.attachment.name.split("/")[-1]
        except Exception:
            att_url = ""

    ts = _format_time(dm)
    return {
        "id": dm.id,
        "client_id": getattr(dm, "client_id", "") or "",
        "sender": sender.username if sender else "",
        "author": sender.username if sender else "",
        "receiver": receiver.username if receiver else "",
        "recipient": receiver.username if receiver else "",
        "display_name": display_name,
        "avatar_url": avatar_url,
        "message": dm.content or "",
        "content": dm.content or "",
        "text": dm.content or "",
        "attachment_url": att_url,
        "attachment_name": att_name,
        "attachment_type": dm.attachment_type or "",
        "timestamp": ts,
        "time": ts,
    }


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        kwargs = self.scope.get("url_route", {}).get("kwargs", {})
        raw_channel = kwargs.get("channel_id") or kwargs.get("channel_name") or 1
        self.channel_id = await self._resolve_channel_id(raw_channel)

        self.room_group_name = f"kiwi_chat_{self.channel_id}"
        self.personal_group_name = build_user_personal_group_name(self.user.username)

        self.feed_group_name = "kiwi_feed"
        await self.channel_layer.group_add(self.feed_group_name, self.channel_name)

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.channel_layer.group_add(self.personal_group_name, self.channel_name)
        await self.accept()

        history = await self._get_recent_messages(self.channel_id)
        await self.send(text_data=json.dumps({
            "type": "chat_history",
            "channel_id": int(self.channel_id),
            "messages": history,
        }))

    async def disconnect(self, close_code):
        for grp in ("room_group_name", "personal_group_name"):
            if hasattr(self, grp):
                await self.channel_layer.group_discard(getattr(self, grp), self.channel_name)

    async def broadcast_profile_post(self, event):
        await self.send(text_data=json.dumps({
            "type": "profile_post_created",
            "post": event["payload"],
        }))

    async def broadcast_profile_post_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "profile_post_updated",
            "post": event["payload"],
        }))

    async def broadcast_profile_post_delete(self, event):
        await self.send(text_data=json.dumps({
            "type": "profile_post_deleted",
            "post_id": event["payload"].get("id"),
        }))


    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            return

        msg_type = data.get("type") or data.get("action")

        if msg_type in ("chat_message", "message", "send_message"):
            text = (data.get("message") or data.get("text") or data.get("content") or "").strip()
            attachment_url = (data.get("attachment_url") or data.get("attachmentUrl") or "").strip()
            attachment_name = (data.get("attachment_name") or data.get("attachmentName") or "").strip()
            attachment_type = (data.get("attachment_type") or data.get("attachmentType") or "").strip()
            client_id = (data.get("client_id") or data.get("id") or "")[:100]

            if not text and not attachment_url:
                return

            payload = await self._save_message(
                channel_id=self.channel_id,
                user=self.user,
                text=text,
                attachment_url=attachment_url,
                attachment_name=attachment_name,
                attachment_type=attachment_type,
                client_id=client_id,
            )
            if payload:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "broadcast_chat_message",
                        "payload": payload,
                    },
                )

        elif msg_type in ("reaction", "toggle_reaction"):
            message_id = data.get("message_id")
            emoji = (data.get("emoji") or "").strip()
            if message_id and emoji:
                updated = await self._toggle_reaction(message_id, self.user, emoji)
                if updated is not None:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "broadcast_reaction_update",
                            "payload": updated,
                        },
                    )

        elif msg_type == "delete_message":
            message_id = data.get("message_id")
            if message_id and await self._delete_message(message_id, self.user):
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "broadcast_delete_message",
                        "message_id": message_id,
                    },
                )

        elif msg_type in ("webrtc_offer", "webrtc_answer", "webrtc_ice", "call_invite", "call_hangup"):
            signal_data = dict(data)
            signal_data["sender"] = self.user.username
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "broadcast_call_signal",
                    "sender": self.user.username,
                    "data": signal_data,
                },
            )

    async def broadcast_chat_message(self, event):
        payload = dict(event["payload"])
        payload["type"] = "chat_message"
        await self.send(text_data=json.dumps(payload))

    async def broadcast_reaction_update(self, event):
        await self.send(text_data=json.dumps({
            "type": "reaction_update",
            "message": event["payload"],
        }))

    async def broadcast_delete_message(self, event):
        await self.send(text_data=json.dumps({
            "type": "delete_message",
            "message_id": event["message_id"],
        }))

    async def broadcast_call_signal(self, event):
        if event.get("sender") == self.user.username:
            return
        await self.send(text_data=json.dumps(event["data"]))

    @database_sync_to_async
    def _resolve_channel_id(self, raw_channel):
        try:
            cid = int(raw_channel)
            if Channel.objects.filter(id=cid).exists():
                return cid
        except (TypeError, ValueError):
            pass
        ch = Channel.objects.filter(name__iexact=str(raw_channel).strip()).first()
        if ch:
            return ch.id
        first_ch = Channel.objects.order_by("id").first()
        return first_ch.id if first_ch else 1

    @database_sync_to_async
    def _get_recent_messages(self, channel_id):
        channel = Channel.objects.filter(id=channel_id).first()
        if not channel:
            return []
        recent_desc = list(
            Message.objects.filter(channel=channel)
            .select_related("user")
            .prefetch_related("reactions", "reactions__user")
            .order_by("-timestamp", "-id")[:50]
        )
        return [_serialize_ws_message(msg, channel_id=channel.id) for msg in reversed(recent_desc)]

    @database_sync_to_async
    def _save_message(
        self,
        channel_id,
        user,
        text,
        attachment_url="",
        attachment_name="",
        attachment_type="",
        client_id="",
    ):
        channel = Channel.objects.filter(id=channel_id).first()
        if not channel:
            return None

        msg = Message.objects.create(
            channel=channel,
            user=user,
            content=(text or "")[:2000],
            attachment_type=(attachment_type or "")[:20],
            client_id=client_id,
        )
        media_url = getattr(settings, "MEDIA_URL", "/media/")
        if attachment_url and attachment_url.startswith(media_url):
            msg.attachment.name = attachment_url[len(media_url):].lstrip("/")
            msg.save(update_fields=["attachment"])

        payload = _serialize_ws_message(msg, channel_id=channel.id)
        if not payload["attachment_url"] and attachment_url and not attachment_url.startswith("blob:"):
            payload["attachment_url"] = attachment_url
        if attachment_name and not payload["attachment_name"]:
            payload["attachment_name"] = attachment_name
        return payload

    @database_sync_to_async
    def _toggle_reaction(self, message_id, user, emoji):
        msg = Message.objects.filter(id=message_id).first()
        if not msg:
            return None
        existing = MessageReaction.objects.filter(message=msg, user=user, emoji=emoji).first()
        if existing:
            existing.delete()
        else:
            MessageReaction.objects.create(message=msg, user=user, emoji=emoji)

        reactions = {}
        for r in msg.reactions.select_related("user").all():
            entry = reactions.setdefault(r.emoji, {"emoji": r.emoji, "count": 0, "users": []})
            entry["count"] += 1
            entry["users"].append(r.user.username)
        return {
            "id": msg.id,
            "reactions": list(reactions.values()),
        }

    @database_sync_to_async
    def _delete_message(self, message_id, user):
        msg = Message.objects.filter(id=message_id).first()
        if not msg:
            return False
        if msg.user_id != user.id and not _is_kiwi_admin(user):
            return False
        msg.delete()
        return True


class DMConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        self.target_username = self.scope["url_route"]["kwargs"].get("username", "")
        self.target_user = await self._get_user(self.target_username)
        if not self.target_user or self.target_user.id == self.user.id:
            await self.close(code=4004)
            return

        pair = sorted([self.user.username.lower(), self.target_user.username.lower()])
        self.room_group_name = f"kiwi_dm_{pair[0]}_{pair[1]}"
        self.self_personal_group = build_user_personal_group_name(self.user.username)
        self.target_personal_group = build_user_personal_group_name(self.target_user.username)

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.channel_layer.group_add(self.self_personal_group, self.channel_name)
        await self.channel_layer.group_add(self.target_personal_group, self.channel_name)
        await self.accept()

        history = await self._get_dm_history(self.user, self.target_user)
        await self.send(text_data=json.dumps({
            "type": "dm_history",
            "with": self.target_user.username,
            "messages": history,
        }))

    async def disconnect(self, close_code):
        for grp in ("room_group_name", "self_personal_group", "target_personal_group"):
            if hasattr(self, grp):
                await self.channel_layer.group_discard(getattr(self, grp), self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            return

        msg_type = data.get("type") or data.get("action")
        if msg_type in ("dm_message", "chat_message", "message", "send_message"):
            text = (data.get("message") or data.get("text") or data.get("content") or "").strip()
            attachment_url = (data.get("attachment_url") or data.get("attachmentUrl") or "").strip()
            attachment_name = (data.get("attachment_name") or data.get("attachmentName") or "").strip()
            attachment_type = (data.get("attachment_type") or data.get("attachmentType") or "").strip()
            client_id = (data.get("client_id") or data.get("id") or "")[:100]

            if not text and not attachment_url:
                return

            payload = await self._save_dm(
                sender=self.user,
                receiver=self.target_user,
                text=text,
                attachment_url=attachment_url,
                attachment_name=attachment_name,
                attachment_type=attachment_type,
                client_id=client_id,
            )
            if payload:
                event_payload = {
                    "type": "broadcast_dm_message",
                    "payload": payload,
                }
                await self.channel_layer.group_send(self.room_group_name, event_payload)
                await self.channel_layer.group_send(self.target_personal_group, event_payload)

        elif msg_type in ("webrtc_offer", "webrtc_answer", "webrtc_ice", "call_invite", "call_hangup"):
            signal_data = dict(data)
            signal_data["sender"] = self.user.username
            event_signal = {
                "type": "broadcast_dm_signal",
                "sender": self.user.username,
                "data": signal_data,
            }
            await self.channel_layer.group_send(self.room_group_name, event_signal)
            await self.channel_layer.group_send(self.target_personal_group, event_signal)

    async def broadcast_dm_message(self, event):
        payload = dict(event["payload"])
        payload["type"] = "dm_message"
        await self.send(text_data=json.dumps(payload))

    async def broadcast_dm_signal(self, event):
        if event.get("sender") == self.user.username:
            return
        await self.send(text_data=json.dumps(event["data"]))

    @database_sync_to_async
    def _get_user(self, username):
        return User.objects.filter(username__iexact=username).first()

    @database_sync_to_async
    def _get_dm_history(self, u1, u2):
        recent_desc = list(
            DirectMessage.objects.filter(
                Q(sender=u1, receiver=u2) | Q(sender=u2, receiver=u1)
            )
            .select_related("sender", "receiver")
            .order_by("-timestamp", "-id")[:60]
        )
        return [_serialize_ws_dm(dm) for dm in reversed(recent_desc)]

    @database_sync_to_async
    def _save_dm(
        self,
        sender,
        receiver,
        text,
        attachment_url="",
        attachment_name="",
        attachment_type="",
        client_id="",
    ):
        dm = DirectMessage.objects.create(
            sender=sender,
            receiver=receiver,
            content=(text or "")[:2000],
            attachment_type=(attachment_type or "")[:20],
            client_id=client_id,
        )
        media_url = getattr(settings, "MEDIA_URL", "/media/")
        if attachment_url and attachment_url.startswith(media_url):
            dm.attachment.name = attachment_url[len(media_url):].lstrip("/")
            dm.save(update_fields=["attachment"])

        payload = _serialize_ws_dm(dm)
        if not payload["attachment_url"] and attachment_url and not attachment_url.startswith("blob:"):
            payload["attachment_url"] = attachment_url
        if attachment_name and not payload["attachment_name"]:
            payload["attachment_name"] = attachment_name
        return payload