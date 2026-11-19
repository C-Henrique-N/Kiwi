from django.urls import re_path
from chat import consumers # substitua 'chat' pelo nome da sua app

websocket_urlpatterns = [
    # A rota deve obrigatoriamente incluir a barra inicial correspondente
    re_path(r'ws/chat/(?P<room_name>\w+)/$', consumers.ChatConsumer.as_asgi()),
    re_path(r'ws/dm/(?P<username>[^/]+)/$', consumers.DMConsumer.as_asgi()),
]