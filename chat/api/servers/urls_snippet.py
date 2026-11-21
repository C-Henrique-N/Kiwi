from django.urls import path
from . import views

# Adicione estas rotas ao seu urlpatterns existente em chat/urls.py:
member_urlpatterns = [
    path(
        'api/servers/<int:server_id>/members/',
        views.server_members_list,
        name='server_members_list',
    ),
    path(
        'api/servers/<int:server_id>/members/add/',
        views.add_server_member,
        name='add_server_member',
    ),
    path(
        'api/servers/<int:server_id>/members/<int:user_id>/role/',
        views.update_server_member_role,
        name='update_server_member_role',
    ),
    path(
        'api/servers/<int:server_id>/members/<int:user_id>/remove/',
        views.remove_server_member,
        name='remove_server_member',
    ),
]