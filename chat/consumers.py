import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import User
from django.db.models import Q
from .models import Message, PrivateMessage, FriendRequest


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_name = self.scope['url_route']['kwargs']['room_name']
        self.room_group_name = f'chat_{self.room_name}'

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        data = json.loads(text_data)
        action = data.get('action')

        if action == 'delete_message':
            message_id = data.get('message_id')
            username = data.get('username', '')
            deleted = await self.delete_message(message_id, username)

            if deleted:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'message_deleted',
                        'message_id': message_id,
                    }
                )
            return

        scope_user = self.scope.get('user')
        default_username = scope_user.username if scope_user and scope_user.is_authenticated else ''
        username = data.get('username') or default_username
        avatar_url = data.get('avatarUrl') or data.get('avatar') or ''
        attachment_url = data.get('attachment_url', '')
        attachment_type = data.get('attachment_type', '')
        message = data.get('message', '').strip()

        if attachment_url:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'id': data.get('id'),
                    'message': message,
                    'username': username,
                    'avatarUrl': avatar_url,
                    'attachment_url': attachment_url,
                    'attachment_type': attachment_type,
                }
            )
            return

        if not message:
            return

        msg = await self.save_message(username, self.room_name, message)
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
                'attachment_url': '',
                'attachment_type': '',
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
        await self.send(text_data=json.dumps({
            'type': 'message_deleted',
            'message_id': event['message_id'],
        }))

    @database_sync_to_async
    def save_message(self, username, room, message):
        user = User.objects.filter(username=username).first()
        if not user:
            return None
        return Message.objects.create(user=user, room=room, content=message)

    @database_sync_to_async
    def delete_message(self, message_id, username):
        if not message_id:
            return False

        msg = Message.objects.filter(id=message_id).first()
        if not msg:
            return False

        request_user = self.scope.get('user')
        if request_user and request_user.is_authenticated:
            if msg.user != request_user and not request_user.is_staff:
                return False
        elif username and msg.user.username != username:
            return False

        msg.delete()
        return True


class DMConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get('user')
        self.other_username = self.scope['url_route']['kwargs']['username']

        if not self.user or not self.user.is_authenticated:
            await self.close()
            return

        self.other_user = await self.get_other_if_friend(self.user, self.other_username)
        if not self.other_user:
            await self.close()
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
        data = json.loads(text_data)
        action = data.get('action')

        if action == 'delete_dm':
            msg_id = data.get('message_id')
            if await self.delete_dm(msg_id):
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'dm_deleted',
                        'message_id': msg_id,
                    }
                )
            return

        attachment_url = data.get('attachment_url', '')
        attachment_type = data.get('attachment_type', '')
        message = data.get('message', '').strip()

        if attachment_url:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'dm_message',
                    'id': data.get('id'),
                    'message': message,
                    'sender': self.user.username,
                    'attachment_url': attachment_url,
                    'attachment_type': attachment_type,
                }
            )
            return

        if not message:
            return

        msg = await self.save_dm(self.user, self.other_user, message)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'dm_message',
                'id': msg.id,
                'message': msg.content,
                'sender': self.user.username,
                'attachment_url': '',
                'attachment_type': '',
            }
        )

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
        await self.send(text_data=json.dumps({
            'type': 'dm_deleted',
            'message_id': event['message_id'],
        }))

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
    def save_dm(self, sender, receiver, content):
        return PrivateMessage.objects.create(
            sender=sender,
            receiver=receiver,
            content=content
        )

    @database_sync_to_async
    def delete_dm(self, msg_id):
        if not msg_id:
            return False
        msg = PrivateMessage.objects.filter(id=msg_id, sender=self.user).first()
        if not msg:
            return False
        msg.delete()
        return True