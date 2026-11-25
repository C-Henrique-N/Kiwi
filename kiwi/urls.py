# kiwi/urls.py
from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles import finders
from django.http import Http404, HttpResponse
from django.urls import include, path, re_path
from django.views.static import serve


def _serve_static_root(filename, content_type):
    file_path = finders.find(filename)
    if not file_path:
        raise Http404(f"{filename} not found")
    with open(file_path, "rb") as f:
        return HttpResponse(f.read(), content_type=content_type)


def manifest_view(request):
    return _serve_static_root("manifest.json", "application/manifest+json")


def service_worker_view(request):
    return _serve_static_root("service-worker.js", "application/javascript")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("manifest.json", manifest_view, name="pwa_manifest"),
    path("service-worker.js", service_worker_view, name="pwa_service_worker"),
    path("", include("chat.urls")),
]

# Sempre por último
urlpatterns += [
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]