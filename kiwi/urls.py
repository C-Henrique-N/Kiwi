# kiwi/urls.py
from django.conf import settings
from django.urls import path, re_path
from django.views.static import serve

urlpatterns = [
    # ... suas rotas atuais ...
]

# Em produção na Railway (sem bucket externo), permite servir MEDIA_ROOT
urlpatterns += [
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]