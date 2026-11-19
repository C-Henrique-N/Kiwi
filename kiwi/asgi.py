import os
import django

# 1. Configura a variável de ambiente primeiro
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'kiwi.settings')

# 2. Inicializa o Django antes de importar qualquer módulo que use modelos
django.setup()

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import chat.routing

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(
            chat.routing.websocket_urlpatterns
        )
    ),
})