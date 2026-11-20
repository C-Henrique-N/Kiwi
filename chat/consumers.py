import json
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from django.db.models import Q
from .models import Channel, FriendRequest, Message, PrivateMessage


def _is_valid_media_url(url_value):
    if not url_value:
        return False
    lowered = url_value.strip().lower()
    return lowered.startswith(('http://', 'https://', '/media/'))


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_name = str(self.scope['url_route']['kwargs']['room_name'])
        self.room_group_name = f'chat_{self.room_name}'
        self.user = self.scope.get('user')
        self.user_group_name = (
            f'user_{self.user.id}'
            if self.user and self.user.is_authenticated
            else None
        )

        if not self.user or not self.user.is_authenticated:
            await self.close(code=4401)
            return

        self.channel_obj = await self.get_allowed_channel(self.user, self.room_name)
        if not self.channel_obj:
            await self.close(code=4403)
            return

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        if self.user_group_name:
            await self.channel_layer.group_add(
                self.user_group_name,
                self.channel_name
            )
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
        if getattr(self, 'user_group_name', None):
            await self.channel_layer.group_discard(
                self.user_group_name,
                self.channel_name
            )

    async def incoming_call(self, event):
        await self.send(text_data=json.dumps({
            'type': 'incoming_call',
            'caller': event.get('caller', ''),
            'signal': event.get('signal'),
        }))

    async def friend_event(self, event):
        await self.send(text_data=json.dumps({
            'type': 'friend_event',
            'event': event.get('event', ''),
            'payload': event.get('payload') or {},
        }))

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        action = data.get('action')

        if action == 'delete_message':
            message_id = data.get('message_id') or data.get('id')
            deleted = await self.delete_message(message_id)
            if deleted:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'message_deleted',
                        'message_id': message_id,
                    }
                )
            return

        if action == 'friend_notify':
            target_user_id = data.get('target_user_id')
            if target_user_id:
                try:
                    clean_target_id = int(target_user_id)
                except (TypeError, ValueError):
                    return
                if await self.can_notify_user(self.user, clean_target_id):
                    await self.channel_layer.group_send(
                        f'user_{clean_target_id}',
                        {
                            'type': 'friend_event',
                            'event': data.get('event', 'friend_update'),
                            'payload': data.get('payload') or {},
                        }
                    )
            return

        username = self.user.username
        avatar_url = data.get('avatarUrl') or data.get('avatar') or ''
        raw_attachment_url = (data.get('attachment_url') or '').strip()
        attachment_url = raw_attachment_url if _is_valid_media_url(raw_attachment_url) else ''
        attachment_type = (data.get('attachment_type') or '').strip()
        message = (data.get('message') or '').strip()

        if not message and not attachment_url:
            return

        msg = await self.save_message(
            self.user,
            self.channel_obj,
            message,
            attachment_url=attachment_url,
            attachment_type=attachment_type,
        )
        if not msg:
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'id': msg.id,
                'message': msg.content,
                'username': username,
                'avatarUrl': avatar_url,
                'attachment_url': attachment_url,
                'attachment_type': attachment_type,
            }
        )

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'chat_message',
            'id': event.get('id'),
            'message': event.get('message', ''),
            'username': event.get('username', ''),
            'avatarUrl': event.get('avatarUrl', ''),
            'attachment_url': event.get('attachment_url', ''),
            'attachment_type': event.get('attachment_type', ''),
        }))

    async def message_deleted(self, event):
        msg_id = event.get('message_id') if event.get('message_id') is not None else event.get('id')
        await self.send(text_data=json.dumps({
            'type': 'message_deleted',
            'message_id': msg_id,
        }))

    @database_sync_to_async
    def can_notify_user(self, sender, target_id):
        if not sender or not sender.is_authenticated:
            return False
        if sender.id == target_id:
            return True
        return FriendRequest.are_friends(sender, User(id=target_id))

    @database_sync_to_async
    def get_allowed_channel(self, user, room_identifier):
        if str(room_identifier).isdigit():
            return Channel.objects.filter(
                id=int(room_identifier),
                server__members=user,
            ).select_related('server').first()
        return None

    @database_sync_to_async
    def save_message(self, user, channel_obj, message, attachment_url='', attachment_type=''):
        if not user or not channel_obj:
            return None
        return Message.objects.create(
            user=user,
            channel=channel_obj,
            content=message,
            attachment_type=attachment_type[:20] if attachment_type else '',
        )

    @database_sync_to_async
    def delete_message(self, message_id):
        if not message_id:
            return False
        try:
            msg_id = int(message_id)
        except (TypeError, ValueError):
            return False

        msg = Message.objects.filter(id=msg_id, channel=self.channel_obj).first()
        if not msg:
            return False

        if msg.user != self.user and not (self.user.is_staff or self.user.is_superuser):
            return False

        msg.delete()
        return True


class DMConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get('user')
        self.other_username = self.scope['url_route']['kwargs']['username']

        if not self.user or not self.user.is_authenticated:
            await self.close(code=4401)
            return

        self.other_user = await self.get_other_if_friend(self.user, self.other_username)
        if not self.other_user:
            await self.close(code=4403)
            return

        ids = sorted([self.user.id, self.other_user.id])
        self.room_group_name = f'dm_{ids[0]}_{ids[1]}'

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        msg_type = data.get('type')
        action = data.get('action')

        if msg_type == 'call_signal':
            signal = data.get('signal') or {}
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'call_signal',
                    'sender': self.user.username,
                    'sender_channel': self.channel_name,
                    'signal': signal,
                }
            )
            if signal.get('offer'):
                await self.channel_layer.group_send(
                    f'user_{self.other_user.id}',
                    {
                        'type': 'incoming_call',
                        'caller': self.user.username,
                        'signal': signal,
                    }
                )
            return

        if msg_type == 'call_hangup':
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'call_hangup',
                    'sender': self.user.username,
                    'sender_channel': self.channel_name,
                }
            )
            return

        if msg_type == 'music_sync':
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'music_sync',
                    'sender': self.user.username,
                    'sender_channel': self.channel_name,
                    'music': data.get('music') or {},
                }
            )
            return

        if action == 'delete_dm':
            msg_id = data.get('message_id') or data.get('id')
            if await self.delete_dm(msg_id):
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'dm_deleted',
                        'message_id': msg_id,
                    }
                )
            return

        raw_attachment_url = (data.get('attachment_url') or '').strip()
        attachment_url = raw_attachment_url if _is_valid_media_url(raw_attachment_url) else ''
        attachment_type = (data.get('attachment_type') or '').strip()
        message = (data.get('message') or '').strip()

        if not message and not attachment_url:
            return

        msg = await self.save_dm(
            self.user,
            self.other_user,
            message,
            attachment_url=attachment_url,
            attachment_type=attachment_type,
        )
        if not msg:
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'dm_message',
                'id': msg.id,
                'message': msg.content,
                'sender': self.user.username,
                'attachment_url': attachment_url,
                'attachment_type': attachment_type,
            }
        )

    async def call_signal(self, event):
        if event.get('sender_channel') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type': 'call_signal',
            'sender': event.get('sender', ''),
            'signal': event.get('signal'),
        }))

    async def call_hangup(self, event):
        if event.get('sender_channel') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type': 'call_hangup',
            'sender': event.get('sender', ''),
        }))

    async def music_sync(self, event):
        if event.get('sender_channel') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type': 'music_sync',
            'sender': event.get('sender', ''),
            'music': event.get('music') or {},
        }))

    async def dm_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'dm_message',
            'id': event.get('id'),
            'message': event.get('message', ''),
            'sender': event.get('sender', ''),
            'attachment_url': event.get('attachment_url', ''),
            'attachment_type': event.get('attachment_type', ''),
        }))

    async def dm_deleted(self, event):
        msg_id = event.get('message_id') if event.get('message_id') is not None else event.get('id')
        await self.send(text_data=json.dumps({
            'type': 'dm_deleted',
            'message_id': msg_id,
        }))

    async def dm_message_deleted(self, event):
        await self.dm_deleted(event)

    @database_sync_to_async
    def get_other_if_friend(self, user, other_username):
        other = User.objects.filter(username=other_username).first()
        if not other or other.id == user.id:
            return None

        is_friend = FriendRequest.objects.filter(
            (Q(sender=user, receiver=other) | Q(sender=other, receiver=user)) &
            Q(status='accepted')
        ).exists()
        return other if is_friend else None

    @database_sync_to_async
    def save_dm(self, sender, receiver, content, attachment_url='', attachment_type=''):
        return PrivateMessage.objects.create(
            sender=sender,
            receiver=receiver,
            content=content,
            attachment_type=attachment_type[:20] if attachment_type else '',
        )

    @database_sync_to_async
    def delete_dm(self, msg_id):
        if not msg_id:
            return False
        try:
            clean_id = int(msg_id)
        except (TypeError, ValueError):
            return False
        msg = PrivateMessage.objects.filter(id=clean_id, sender=self.user).first()
        if not msg:
            return False
        msg.delete()
        return True