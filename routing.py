from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(
        r"^ws/chat/(?P<server_id>[^/]+)/(?P<channel_name>[^/]+)/$",
        consumers.ChatConsumer.as_asgi(),
    ),
    re_path(r"^ws/chat/(?P<room_slug>[^/]+)/$", consumers.ChatConsumer.as_asgi()),
    re_path(r"^ws/dm/(?P<username>[^/]+)/$", consumers.DMConsumer.as_asgi()),
]