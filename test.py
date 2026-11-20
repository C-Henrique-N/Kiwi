@'
from django.contrib.auth import get_user_model
from chat.models import Server, Channel, Message, PrivateMessage

User = get_user_model()
print("Users:", list(User.objects.values("id", "username", "is_active")))
print("Servers:", list(Server.objects.values("id", "name", "owner_id")))
print("Channels:", list(Channel.objects.values("id", "name", "server_id")))
print("Messages:", Message.objects.count(), "DMs:", PrivateMessage.objects.count())
'@ | python manage.py shell