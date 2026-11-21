import json

# Adicione este handler dentro do seu AsyncWebsocketConsumer em chat/consumers.py
# (e garanta que ao entrar em um servidor o socket faz group_add(f"server_{server_id}", self.channel_name)):

async def server_member_update(self, event):
    await self.send(text_data=json.dumps({
        'type': 'server_member_update',
        'action': event['action'],        # 'added', 'removed' ou 'role_updated'
        'server_id': event['server_id'],
        'user_id': event['user_id'],
        'username': event['username'],
        'role': event.get('role', 'member'),
    }))