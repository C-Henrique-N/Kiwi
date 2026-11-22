import json
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone
from .models import Channel, DirectMessage, Friendship, Message, MessageReaction, Profile


def build_user_personal_group_name(username):
    clean = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in (username or ""))
    return f"kiwi_user_{clean}"


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        self.channel_id = self.scope["url_route"]["kwargs"].get("channel_id", 1)
        self.room_group_name = f"kiwi_chat_{self.channel_id}"
        self.personal_group_name = build_user_personal_group_name(self.user.username)

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
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        if hasattr(self, "personal_group_name"):
            await self.channel_layer.group_discard(self.personal_group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            return

        msg_type = data.get("type") or "chat_message"

        if msg_type in ("chat_message", "message"):
            text = (data.get("message") or data.get("text") or "").strip()
            attachment_url = (data.get("attachment_url") or "").strip()
            attachment_name = (data.get("attachment_name") or "").strip()
            attachment_type = (data.get("attachment_type") or "").strip()
            client_id = data.get("id") or data.get("client_id")

            if not text and not attachment_url:
                return

            saved = await self._save_message(
                self.channel_id,
                self.user,
                text,
                attachment_url=attachment_url,
                attachment_name=attachment_name,
                attachment_type=attachment_type,
            )
            if saved:
                if client_id:
                    saved["client_id"] = client_id
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {"type": "broadcast_chat_message", "payload": saved},
                )

        elif msg_type == "reaction":
            message_id = data.get("message_id")
            emoji = (data.get("emoji") or "👍").strip()[:16]
            if message_id and emoji:
                updated = await self._toggle_reaction(message_id, self.user, emoji)
                if updated:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {"type": "broadcast_reaction_update", "payload": updated},
                    )

        elif msg_type == "delete_message":
            message_id = data.get("message_id")
            if message_id:
                deleted = await self._delete_message(message_id, self.user)
                if deleted:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {"type": "broadcast_delete_message", "message_id": int(message_id)},
                    )

        elif msg_type in ("webrtc_offer", "webrtc_answer", "webrtc_ice", "call_invite", "call_hangup"):
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "broadcast_call_signal",
                    "sender": self.user.username,
                    "signal_type": msg_type,
                    "data": data,
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
    def _get_recent_messages(self, channel_id):
        channel = Channel.objects.filter(id=channel_id).first()
        if not channel:
            return []
        qs = (
            Message.objects.filter(channel=channel)
            .select_related("sender", "sender__profile")
            .prefetch_related("reactions", "reactions__user")
            .order_by("-created_at")[:50]
        )
        result = []
        for msg in reversed(list(qs)):
            p = getattr(msg.sender, "profile", None)
            avatar_url = ""
            display_name = msg.sender.username
            if p:
                display_name = (p.display_name or "").strip() or msg.sender.username
                try:
                    avatar_url = p.avatar.url if p.avatar else ""
                except Exception:
                    avatar_url = ""

            att_url = ""
            att_name = ""
            if msg.attachment:
                try:
                    att_url = msg.attachment.url
                    att_name = msg.attachment.name.split("/")[-1]
                except Exception:
                    att_url = ""

            reactions = {}
            for rx in msg.reactions.all():
                reactions.setdefault(rx.emoji, {"emoji": rx.emoji, "count": 0, "me": False})
                reactions[rx.emoji]["count"] += 1
                if rx.user_id == self.user.id:
                    reactions[rx.emoji]["me"] = True

            ts = timezone.localtime(msg.created_at).strftime("%H:%M")
            result.append({
                "id": msg.id,
                "channel_id": channel.id,
                "sender": msg.sender.username,
                "display_name": display_name,
                "avatar_url": avatar_url,
                "message": msg.content or "",
                "text": msg.content or "",
                "attachment_url": att_url,
                "attachment_name": att_name,
                "attachment_type": msg.attachment_type or "",
                "timestamp": ts,
                "time": ts,
                "reactions": list(reactions.values()),
            })
        return result

    @database_sync_to_async
    def _save_message(self, channel_id, user, text, attachment_url="", attachment_name="", attachment_type=""):
        channel = Channel.objects.filter(id=channel_id).first()
        if not channel:
            channel = Channel.objects.order_by("id").first()
        if not channel:
            return None

        msg = Message.objects.create(
            channel=channel,
            sender=user,
            content=(text or "")[:2000],
            attachment_type=(attachment_type or "")[:64],
        )
        if attachment_url and attachment_url.startswith("/"):
            from django.conf import settings

            media_url = getattr(settings, "MEDIA_URL", "/media/")
            if attachment_url.startswith(media_url):
                msg.attachment.name = attachment_url[len(media_url):].lstrip("/")
                msg.save()

        p, _ = Profile.objects.get_or_create(user=user)
        avatar_url = ""
        try:
            avatar_url = p.avatar.url if p.avatar else ""
        except Exception:
            avatar_url = ""

        att_final_url = ""
        if msg.attachment:
            try:
                att_final_url = msg.attachment.url
            except Exception:
                att_final_url = attachment_url
        else:
            att_final_url = attachment_url

        ts = timezone.localtime(msg.created_at).strftime("%H:%M")
        return {
            "id": msg.id,
            "channel_id": channel.id,
            "sender": user.username,
            "display_name": (p.display_name or "").strip() or user.username,
            "avatar_url": avatar_url,
            "message": msg.content or "",
            "text": msg.content or "",
            "attachment_url": att_final_url,
            "attachment_name": attachment_name or (msg.attachment.name.split("/")[-1] if msg.attachment else ""),
            "attachment_type": msg.attachment_type or "",
            "timestamp": ts,
            "time": ts,
            "reactions": [],
        }

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
        for rx in msg.reactions.all():
            reactions.setdefault(rx.emoji, {"emoji": rx.emoji, "count": 0, "me": False})
            reactions[rx.emoji]["count"] += 1
            if rx.user_id == user.id:
                reactions[rx.emoji]["me"] = True
        return {"id": msg.id, "channel_id": msg.channel_id, "reactions": list(reactions.values())}

    @database_sync_to_async
    def _delete_message(self, message_id, user):
        msg = Message.objects.filter(id=message_id).first()
        if not msg:
            return False
        is_admin = user.is_superuser or (user.username or "").strip().lower() == "akiirapw"
        if msg.sender_id != user.id and not is_admin:
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
        await self.accept()

        history = await self._get_dm_history(self.user, self.target_user)
        await self.send(text_data=json.dumps({
            "type": "dm_history",
            "peer": self.target_user.username,
            "messages": history,
        }))

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        if hasattr(self, "self_personal_group"):
            await self.channel_layer.group_discard(self.self_personal_group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except Exception:
            return

        msg_type = data.get("type") or "dm_message"
        if msg_type in ("dm_message", "chat_message", "message"):
            text = (data.get("message") or data.get("text") or "").strip()
            attachment_url = (data.get("attachment_url") or "").strip()
            attachment_name = (data.get("attachment_name") or "").strip()
            attachment_type = (data.get("attachment_type") or "").strip()
            client_id = data.get("id") or data.get("client_id")

            if not text and not attachment_url:
                return

            saved = await self._save_dm(
                self.user,
                self.target_user,
                text,
                attachment_url=attachment_url,
                attachment_name=attachment_name,
                attachment_type=attachment_type,
            )
            if saved:
                if client_id:
                    saved["client_id"] = client_id
                event_payload = {"type": "broadcast_dm_message", "payload": saved}
                await self.channel_layer.group_send(self.room_group_name, event_payload)
                await self.channel_layer.group_send(self.target_personal_group, event_payload)

        elif msg_type in ("webrtc_offer", "webrtc_answer", "webrtc_ice", "call_invite", "call_hangup"):
            event_signal = {
                "type": "broadcast_dm_signal",
                "sender": self.user.username,
                "data": data,
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
        qs = (
            DirectMessage.objects.filter(
                Q(sender=u1, recipient=u2) | Q(sender=u2, recipient=u1)
            )
            .select_related("sender", "recipient", "sender__profile")
            .order_by("created_at")[:60]
        )
        items = []
        for dm in qs:
            p = getattr(dm.sender, "profile", None)
            avatar_url = ""
            display_name = dm.sender.username
            if p:
                display_name = (p.display_name or "").strip() or dm.sender.username
                try:
                    avatar_url = p.avatar.url if p.avatar else ""
                except Exception:
                    avatar_url = ""
            att_url = ""
            att_name = ""
            if dm.attachment:
                try:
                    att_url = dm.attachment.url
                    att_name = dm.attachment.name.split("/")[-1]
                except Exception:
                    att_url = ""
            ts = timezone.localtime(dm.created_at).strftime("%H:%M")
            items.append({
                "id": dm.id,
                "sender": dm.sender.username,
                "recipient": dm.recipient.username,
                "display_name": display_name,
                "avatar_url": avatar_url,
                "message": dm.content or "",
                "text": dm.content or "",
                "attachment_url": att_url,
                "attachment_name": att_name,
                "attachment_type": dm.attachment_type or "",
                "timestamp": ts,
                "time": ts,
            })
        return items

    @database_sync_to_async
    def _save_dm(self, sender, recipient, text, attachment_url="", attachment_name="", attachment_type=""):
        dm = DirectMessage.objects.create(
            sender=sender,
            recipient=recipient,
            content=(text or "")[:2000],
            attachment_type=(attachment_type or "")[:64],
        )
        if attachment_url and attachment_url.startswith("/"):
            from django.conf import settings

            media_url = getattr(settings, "MEDIA_URL", "/media/")
            if attachment_url.startswith(media_url):
                dm.attachment.name = attachment_url[len(media_url):].lstrip("/")
                dm.save()

        p, _ = Profile.objects.get_or_create(user=sender)
        avatar_url = ""
        try:
            avatar_url = p.avatar.url if p.avatar else ""
        except Exception:
            avatar_url = ""

        ts = timezone.localtime(dm.created_at).strftime("%H:%M")
        return {
            "id": dm.id,
            "sender": sender.username,
            "recipient": recipient.username,
            "display_name": (p.display_name or "").strip() or sender.username,
            "avatar_url": avatar_url,
            "message": dm.content or "",
            "text": dm.content or "",
            "attachment_url": attachment_url,
            "attachment_name": attachment_name or (dm.attachment.name.split("/")[-1] if dm.attachment else ""),
            "attachment_type": dm.attachment_type or "",
            "timestamp": ts,
            "time": ts,
        }
