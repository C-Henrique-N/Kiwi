import json
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from .models import Server, ServerMemberRole, UserProfile

User = get_user_model()


def _can_manage_server_members(user, server):
    if not user or not user.is_authenticated:
        return False
    if server.owner_id == user.id or user.is_superuser:
        return True
    return ServerMemberRole.objects.filter(
        server=server,
        user=user,
        role='admin',
    ).exists()


def _broadcast_server_members_update(server_id, action, user_id, username='', role='member'):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    # Ajuste o prefixo se o seu Consumer usar outro nome de grupo para servidores
    group_name = f'server_{server_id}'
    async_to_sync(channel_layer.group_send)(
        group_name,
        {
            'type': 'server_member_update',
            'action': action,      # 'added', 'removed' ou 'role_updated'
            'server_id': server_id,
            'user_id': user_id,
            'username': username,
            'role': role,
        },
    )


def _serialize_member(user, server, role_map):
    profile = getattr(user, 'profile', None)
    if user.id == server.owner_id:
        role_key = 'owner'
        role_label = 'Owner'
    else:
        role_key = role_map.get(user.id, 'member')
        role_label = dict(ServerMemberRole.ROLE_CHOICES).get(role_key, 'Membro')

    song = (profile.spotify_song if profile else '').strip()
    artist = (profile.spotify_artist if profile else '').strip()
    activity = f'🎵 {song} • {artist}' if song and artist else (profile.bubble if profile else '')

    return {
        'id': user.id,
        'username': user.username,
        'role': role_key,
        'role_label': role_label,
        'is_owner': user.id == server.owner_id,
        'avatar_url': profile.avatar.url if (profile and profile.avatar) else '',
        'banner_url': profile.banner.url if (profile and profile.banner) else '',
        'bubble': profile.bubble if profile else '',
        'activity': activity,
        'accent_color': profile.accent_color if profile else '#8a63d2',
    }


@login_required
@require_GET
def server_members_list(request, server_id):
    server = get_object_or_404(Server, pk=server_id)

    is_member = (
        server.owner_id == request.user.id
        or server.members.filter(pk=request.user.id).exists()
        or request.user.is_superuser
    )
    if not is_member:
        return JsonResponse({'error': 'Acesso negado.'}, status=403)

    members_qs = server.members.select_related('profile').order_by('username')
    role_map = dict(
        ServerMemberRole.objects.filter(server=server).values_list('user_id', 'role')
    )

    members_data = [_serialize_member(u, server, role_map) for u in members_qs]
    role_priority = {'owner': 0, 'admin': 1, 'member': 2}
    members_data.sort(key=lambda m: (role_priority.get(m['role'], 9), m['username'].lower()))

    return JsonResponse({
        'ok': True,
        'server_id': server.id,
        'server_name': server.name,
        'can_manage': _can_manage_server_members(request.user, server),
        'is_owner': server.owner_id == request.user.id,
        'members': members_data,
    })


@login_required
@require_POST
def add_server_member(request, server_id):
    server = get_object_or_404(Server, pk=server_id)

    if not _can_manage_server_members(request.user, server):
        return JsonResponse({'error': 'Sem permissão para adicionar membros.'}, status=403)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido.'}, status=400)

    username = (payload.get('username') or '').strip().lstrip('@')
    if not username:
        return JsonResponse({'error': 'Informe o nome de usuário.'}, status=400)

    target_user = User.objects.filter(username__iexact=username).first()
    if not target_user:
        return JsonResponse({'error': f'Usuário @{username} não encontrado.'}, status=404)

    if server.members.filter(pk=target_user.id).exists():
        return JsonResponse({'error': 'Esse usuário já faz parte do servidor.'}, status=400)

    UserProfile.objects.get_or_create(user=target_user)
    server.members.add(target_user)
    ServerMemberRole.objects.update_or_create(
        server=server,
        user=target_user,
        defaults={'role': 'member'},
    )

    _broadcast_server_members_update(
        server_id=server.id,
        action='added',
        user_id=target_user.id,
        username=target_user.username,
        role='member',
    )

    role_map = dict(
        ServerMemberRole.objects.filter(server=server).values_list('user_id', 'role')
    )
    return JsonResponse({
        'ok': True,
        'member': _serialize_member(target_user, server, role_map),
    })


@login_required
@require_POST
def update_server_member_role(request, server_id, user_id):
    server = get_object_or_404(Server, pk=server_id)

    if server.owner_id != request.user.id and not request.user.is_superuser:
        return JsonResponse({'error': 'Apenas o dono do servidor pode alterar cargos.'}, status=403)

    if server.owner_id == user_id:
        return JsonResponse({'error': 'Não é possível alterar o cargo do dono.'}, status=400)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido.'}, status=400)

    new_role = payload.get('role')
    if new_role not in ('admin', 'member'):
        return JsonResponse({'error': 'Cargo inválido.'}, status=400)

    target_user = get_object_or_404(User, pk=user_id)
    if not server.members.filter(pk=target_user.id).exists():
        return JsonResponse({'error': 'Usuário não está neste servidor.'}, status=404)

    ServerMemberRole.objects.update_or_create(
        server=server,
        user=target_user,
        defaults={'role': new_role},
    )

    _broadcast_server_members_update(
        server_id=server.id,
        action='role_updated',
        user_id=target_user.id,
        username=target_user.username,
        role=new_role,
    )

    return JsonResponse({'ok': True, 'user_id': target_user.id, 'role': new_role})


@login_required
@require_POST
def remove_server_member(request, server_id, user_id):
    server = get_object_or_404(Server, pk=server_id)

    if not _can_manage_server_members(request.user, server):
        return JsonResponse({'error': 'Sem permissão para remover membros.'}, status=403)

    if server.owner_id == user_id:
        return JsonResponse({'error': 'Não é possível remover o dono do servidor.'}, status=400)

    target_user = get_object_or_404(User, pk=user_id)
    if not server.members.filter(pk=target_user.id).exists():
        return JsonResponse({'error': 'Usuário não está neste servidor.'}, status=404)

    server.members.remove(target_user)
    ServerMemberRole.objects.filter(server=server, user=target_user).delete()

    _broadcast_server_members_update(
        server_id=server.id,
        action='removed',
        user_id=target_user.id,
        username=target_user.username,
    )

    return JsonResponse({'ok': True, 'removed_user_id': target_user.id})