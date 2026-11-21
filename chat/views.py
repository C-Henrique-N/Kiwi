import json
import os
from urllib.parse import urlparse

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import models
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .consumers import (
    build_channel_group_name,
    build_dm_group_name,
    build_user_personal_group_name,
    sanitize_group_token,
)
from .models import (
    Channel,
    FriendRequest,
    Message,
    PrivateMessage,
    SavedMusic,
    Server,
    ServerMember,
    ServerRole,
    UserProfile,
)

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic', '.heif', '.avif'}
AUDIO_EXTS = {'.mp3', '.wav', '.ogg', '.webm', '.m4a'}
DOC_EXTS = {'.pdf', '.doc', '.docx', '.txt', '.zip', '.rar', '.csv', '.xls', '.xlsx', '.ppt', '.pptx'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

STANDARD_ACCENT_COLORS = {
    '#ffffff',
    '#2b2d31',
    '#ff9de2',
    '#e74c3c',
    '#23a55a',
    '#5865f2',
    '#95a5a6',
}
ADMIN_ACCENT_COLORS = STANDARD_ACCENT_COLORS | {'#d4af37'}

ALLOWED_FRAME_EFFECTS = {
    'none',
    'gold_glow_frame',
    'neon_ring',
    'galaxy_ring',
    'fire_ring',
    'ufo_orbit',
    'helmet_ring',
    'sakura_hearts',
    'cat_ears',
    'lightning_ring',
    'rain_drops',
    'sparks_burst',
    'bubbles_ring',
    'thunder_purple',
    'ghost_pop',
    'profile_sakura',
    'profile_aurora',
    'profile_stars',
    'profile_cats',
    'profile_butterflies',
    'profile_window',
    'profile_moon',
}


def is_admin_user(user):
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def is_akiira_admin(user):
    return bool(
        user
        and user.is_authenticated
        and user.username.lower() == 'akiirapw'
        and (user.is_staff or user.is_superuser)
    )


def are_friends(user1, user2):
    return FriendRequest.are_friends(user1, user2)


def _get_accessible_server_or_404(user, server_id):
    qs = Server.objects.filter(id=server_id)
    if not is_admin_user(user):
        qs = qs.filter(models.Q(members=user) | models.Q(owner=user)).distinct()
    return get_object_or_404(qs)


def _user_can_manage_server(user, server):
    if not user or not user.is_authenticated or not server:
        return False
    if server.owner_id == user.id or is_admin_user(user):
        return True
    membership = ServerMember.objects.filter(server=server, user=user).select_related('role').first()
    return bool(membership and membership.role and membership.role.can_manage_channels)


def _user_can_kick_members(user, server):
    if not user or not user.is_authenticated or not server:
        return False
    if server.owner_id == user.id or is_admin_user(user):
        return True
    membership = ServerMember.objects.filter(server=server, user=user).select_related('role').first()
    return bool(membership and membership.role and membership.role.can_kick_members)


def _user_can_manage_roles(user, server):
    if not user or not user.is_authenticated or not server:
        return False
    if server.owner_id == user.id or is_admin_user(user):
        return True
    membership = ServerMember.objects.filter(server=server, user=user).select_related('role').first()
    return bool(membership and membership.role and membership.role.can_manage_roles)


def _sanitize_external_url(raw_url):
    val = (raw_url or '').strip()
    if not val:
        return ''
    if val.startswith('/media/'):
        return val
    parsed = urlparse(val)
    if parsed.scheme in {'http', 'https'} and parsed.netloc:
        return val
    return ''


def _sanitize_accent_color(color_value, user, fallback='#ff9de2'):
    raw = (color_value or '').strip().lower()
    allowed = ADMIN_ACCENT_COLORS if is_admin_user(user) else STANDARD_ACCENT_COLORS
    return raw if raw in allowed else fallback


def _sanitize_frame_effect(frame_value, user):
    if not is_akiira_admin(user):
        return 'none'
    raw = (frame_value or 'none').strip().lower()
    return raw if raw in ALLOWED_FRAME_EFFECTS else 'none'


def _get_accepted_friend_users(user):
    accepted = FriendRequest.objects.filter(
        (models.Q(sender=user) | models.Q(receiver=user))
        & models.Q(status='accepted')
    ).select_related('sender', 'receiver', 'sender__profile', 'receiver__profile')

    friends = []
    seen_ids = set()
    for req in accepted:
        friend = req.receiver if req.sender_id == user.id else req.sender
        if friend.id not in seen_ids:
            seen_ids.add(friend.id)
            UserProfile.objects.get_or_create(user=friend)
            friends.append(friend)
    return friends


def _notify_user_group(user_id, event_type, payload, username=None):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    resolved_username = username
    if not resolved_username:
        u = User.objects.filter(pk=int(user_id)).only('username').first()
        resolved_username = u.username if u else str(user_id)
    group_name = build_user_personal_group_name(resolved_username)
    async_to_sync(channel_layer.group_send)(
        group_name,
        {
            'type': 'friend_event',
            'event': event_type,
            'payload': payload,
        },
    )


def _parse_request_data(request):
    if request.content_type and 'application/json' in request.content_type:
        return json.loads(request.body or '{}')
    return request.POST


def _build_music_payload(request, profile):
    audio_src = ''
    if profile.music_audio_file:
        audio_src = request.build_absolute_uri(profile.music_audio_file.url)
    elif profile.music_audio_url:
        audio_src = profile.music_audio_url

    cover_src = ''
    if profile.music_cover_file:
        cover_src = request.build_absolute_uri(profile.music_cover_file.url)
    elif profile.music_cover_url:
        cover_src = profile.music_cover_url

    return {
        'music_url': profile.music_url or '',
        'music_audio_url': audio_src,
        'music_cover_url': cover_src,
    }


def _channel_ws_tokens(channel):
    if not channel:
        return ('home', 'geral')
    srv_token = sanitize_group_token(str(channel.server_id), 'home')
    ch_token = sanitize_group_token(channel.name, 'geral')
    return (srv_token, ch_token)


def _serialize_server_member(request, server, membership):
    u = membership.user
    prof, _ = UserProfile.objects.get_or_create(user=u)
    role = membership.role
    is_owner = (u.id == server.owner_id)
    akiira_admin = is_akiira_admin(u)

    return {
        'id': u.id,
        'username': u.username,
        'is_owner': is_owner,
        'is_admin': is_admin_user(u),
        'is_akiira_admin': akiira_admin,
        'avatarUrl': request.build_absolute_uri(prof.avatar.url) if prof.avatar else '',
        'bannerUrl': request.build_absolute_uri(prof.banner.url) if prof.banner else '',
        'bubble': prof.bubble or '',
        'pronouns': prof.pronouns or '',
        'bio': prof.bio or '',
        'spotify_song': prof.spotify_song or '',
        'spotify_artist': prof.spotify_artist or '',
        'accent_color': _sanitize_accent_color(prof.accent_color, u, fallback=prof.accent_color or '#ff9de2'),
        'frame_effect': _sanitize_frame_effect(getattr(prof, 'frame_effect', 'none'), u),
        'profile_glow': bool(getattr(prof, 'profile_glow', False)) if akiira_admin else False,
        'avatar_pulse': bool(getattr(prof, 'avatar_pulse', False)) if akiira_admin else False,
        'name_style': (getattr(prof, 'name_style', '') or '') if akiira_admin else '',
        'role': role.to_dict() if role else None,
        'joined_at': membership.joined_at.isoformat() if membership.joined_at else '',
    }


@login_required(login_url='login')
def room_view(request, server_id=None, channel_id=None):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    servers = (
        Server.objects.filter(models.Q(members=request.user) | models.Q(owner=request.user))
        .distinct()
        .prefetch_related('channels', 'roles')
    )

    active_server = None
    active_channel = None
    chat_messages = []

    if server_id is not None:
        server_qs = Server.objects.filter(id=server_id)
        if not is_admin_user(request.user):
            server_qs = server_qs.filter(
                models.Q(members=request.user) | models.Q(owner=request.user)
            ).distinct()
        active_server = server_qs.first()

        # Evita 404 seco quando o servidor nao existe ou o usuario nao tem acesso
        if active_server is None:
            return redirect('room_home')

        channels = list(active_server.channels.all())
        if channel_id is not None:
            active_channel = Channel.objects.filter(id=channel_id, server=active_server).first()
            if active_channel is None:
                return redirect('room_server', server_id=active_server.id)
        elif channels:
            active_channel = channels[0]

        if active_channel:
            chat_messages = list(
                Message.objects.filter(channel=active_channel)
                .select_related('user', 'user__profile')
                .order_by('timestamp')[:50]
            )

    akiira_admin = is_akiira_admin(request.user)
    frame_effect = _sanitize_frame_effect(getattr(profile, 'frame_effect', 'none'), request.user)
    profile_glow = bool(getattr(profile, 'profile_glow', False)) if akiira_admin else False
    avatar_pulse = bool(getattr(profile, 'avatar_pulse', False)) if akiira_admin else False
    name_style = (getattr(profile, 'name_style', '') or '') if akiira_admin else ''

    music_payload = _build_music_payload(request, profile)
    sanitized_accent = _sanitize_accent_color(
        profile.accent_color,
        request.user,
        fallback=profile.accent_color or '#ff9de2',
    )

    profile_data = {
        'username': request.user.username,
        'bubble': profile.bubble or '',
        'pronouns': profile.pronouns or '',
        'bio': profile.bio or '',
        'song': profile.spotify_song or '',
        'artist': profile.spotify_artist or '',
        'musicUrl': profile.music_url or '',
        'audioSrc': music_payload.get('music_audio_url', ''),
        'coverUrl': music_payload.get('music_cover_url', ''),
        'accentColor': sanitized_accent,
        'avatarUrl': request.build_absolute_uri(profile.avatar.url) if profile.avatar else '',
        'bannerUrl': request.build_absolute_uri(profile.banner.url) if profile.banner else '',
        'frameEffect': frame_effect != 'none',
        'goldGlowFrame': frame_effect == 'gold_glow_frame',
        'profileGlow': profile_glow,
        'avatarPulse': avatar_pulse,
        'nameStyle': name_style,
        'is_admin': is_admin_user(request.user),
        'is_akiira_admin': akiira_admin,
    }

    return render(
        request,
        'chat/room.html',
        {
            'username': request.user.username,
            'is_admin': is_admin_user(request.user),
            'is_akiira_admin': akiira_admin,
            'frame_effect': frame_effect,
            'profile_glow': profile_glow,
            'avatar_pulse': avatar_pulse,
            'name_style': name_style,
            'profile_json': json.dumps(profile_data),
            'servers': servers,
            'active_server': active_server,
            'active_channel': active_channel,
            'messages': chat_messages,
        },
    )


@login_required(login_url='login')
def profile_page_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    return render(request, 'chat/profile.html', {'profile': profile})


@login_required(login_url='login')
def loja_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    servers = (
        Server.objects.filter(models.Q(members=request.user) | models.Q(owner=request.user))
        .distinct()
        .prefetch_related('channels', 'roles')
    )
    return render(
        request,
        'chat/loja.html',
        {
            'profile': profile,
            'servers': servers,
            'is_akiira_admin': is_akiira_admin(request.user),
        },
    )


@login_required
@require_POST
def create_server(request):
    name = (request.POST.get('name') or '').strip()
    icon = request.FILES.get('icon')
    raw_channels = (request.POST.get('channels') or '').strip()

    if not name:
        return JsonResponse({'status': 'error', 'message': 'Informe o nome do servidor.'}, status=400)

    server = Server.objects.create(name=name[:120], icon=icon, owner=request.user)
    server.members.add(request.user)
    ServerMember.objects.get_or_create(server=server, user=request.user)

    friends = _get_accepted_friend_users(request.user)
    if friends:
        server.members.add(*friends)
        for friend in friends:
            ServerMember.objects.get_or_create(server=server, user=friend)

    channel_names = [
        c.strip().lstrip('#').lower().replace(' ', '-')
        for c in raw_channels.split(',')
        if c.strip()
    ]
    if not channel_names:
        channel_names = ['chat', 'musica']

    created_channels = []
    for idx, ch_name in enumerate(channel_names):
        ch_type = 'audio' if ch_name in {'musica', 'música', 'audio', 'voz', 'call'} else 'text'
        ch, _ = Channel.objects.get_or_create(
            server=server,
            name=ch_name[:100],
            defaults={'channel_type': ch_type, 'position': idx},
        )
        created_channels.append({'id': ch.id, 'name': ch.name, 'channel_type': ch.channel_type})

    icon_url = request.build_absolute_uri(server.icon.url) if server.icon else ''
    return JsonResponse({
        'status': 'success',
        'server': {
            'id': server.id,
            'name': server.name,
            'owner_id': server.owner_id,
            'icon_url': icon_url,
            'channels': created_channels,
        },
    })


@login_required
@require_POST
def update_server(request, server_id):
    server = _get_accessible_server_or_404(request.user, server_id)
    if not _user_can_manage_server(request.user, server):
        return JsonResponse({'status': 'error', 'message': 'Sem permissão para editar este servidor.'}, status=403)

    name = (request.POST.get('name') or '').strip()
    if name:
        server.name = name[:120]

    if 'icon' in request.FILES:
        server.icon = request.FILES['icon']

    server.save()
    return JsonResponse({
        'status': 'success',
        'server': {
            'id': server.id,
            'name': server.name,
            'owner_id': server.owner_id,
            'icon_url': request.build_absolute_uri(server.icon.url) if server.icon else '',
        },
    })


@login_required
@require_POST
def delete_server(request, server_id):
    server = get_object_or_404(Server, id=server_id)
    if server.owner_id != request.user.id and not is_admin_user(request.user):
        return JsonResponse({'status': 'error', 'message': 'Apenas o dono pode excluir o servidor.'}, status=403)

    server.delete()
    return JsonResponse({'status': 'success'})


@login_required
@require_POST
def create_channel(request, server_id):
    server = _get_accessible_server_or_404(request.user, server_id)
    if not _user_can_manage_server(request.user, server):
        return JsonResponse({'status': 'error', 'message': 'Sem permissão para criar canais.'}, status=403)

    raw_name = (request.POST.get('name') or '').strip().lstrip('#').lower().replace(' ', '-')
    channel_type = (request.POST.get('channel_type') or 'text').strip()
    if channel_type not in {'text', 'audio'}:
        channel_type = 'text'

    if not raw_name:
        return JsonResponse({'status': 'error', 'message': 'Informe o nome do canal.'}, status=400)

    if Channel.objects.filter(server=server, name=raw_name).exists():
        return JsonResponse({'status': 'error', 'message': 'Já existe um canal com esse nome neste servidor.'}, status=400)

    next_pos = server.channels.count()
    channel = Channel.objects.create(
        server=server,
        name=raw_name[:100],
        channel_type=channel_type,
        position=next_pos,
    )

    return JsonResponse({
        'status': 'success',
        'channel': {
            'id': channel.id,
            'name': channel.name,
            'channel_type': channel.channel_type,
        },
    })


@login_required
@require_POST
def delete_channel(request, server_id, channel_id):
    server = _get_accessible_server_or_404(request.user, server_id)
    if not _user_can_manage_server(request.user, server):
        return JsonResponse({'status': 'error', 'message': 'Sem permissão para excluir canais.'}, status=403)

    channel = get_object_or_404(Channel, id=channel_id, server=server)
    channel.delete()
    return JsonResponse({'status': 'success'})


@login_required
def get_server_members_grouped(request, server_id):
    server = _get_accessible_server_or_404(request.user, server_id)

    existing_ids = set(
        ServerMember.objects.filter(server=server).values_list('user_id', flat=True)
    )
    missing_member_ids = [
        uid for uid in server.members.values_list('id', flat=True) if uid not in existing_ids
    ]
    if missing_member_ids:
        ServerMember.objects.bulk_create([
            ServerMember(server=server, user_id=uid) for uid in missing_member_ids
        ])

    roles = list(ServerRole.objects.filter(server=server).order_by('position', 'id'))
    memberships = list(
        ServerMember.objects.filter(server=server)
        .select_related('user', 'user__profile', 'role')
        .order_by('user__username')
    )

    serialized_members = [_serialize_server_member(request, server, m) for m in memberships]
    members_by_role_id = {r.id: [] for r in roles}
    unassigned_members = []

    for m_data in serialized_members:
        r_obj = m_data.get('role')
        if r_obj and r_obj['id'] in members_by_role_id:
            members_by_role_id[r_obj['id']].append(m_data)
        else:
            unassigned_members.append(m_data)

    groups = []
    for r in roles:
        groups.append({
            'id': r.id,
            'name': r.name,
            'color': r.color,
            'position': r.position,
            'members': members_by_role_id[r.id],
        })

    if unassigned_members or not groups:
        groups.append({
            'id': None,
            'name': 'Membros',
            'color': '#949ba4',
            'position': 9999,
            'members': unassigned_members,
        })

    return JsonResponse({
        'status': 'success',
        'server_id': server.id,
        'owner_id': server.owner_id,
        'can_manage_roles': _user_can_manage_roles(request.user, server),
        'can_kick_members': _user_can_kick_members(request.user, server),
        'roles': [r.to_dict() for r in roles],
        'groups': groups,
        'members': serialized_members,
    })


@login_required
@require_POST
def create_server_role(request, server_id):
    server = _get_accessible_server_or_404(request.user, server_id)
    if not _user_can_manage_roles(request.user, server):
        return JsonResponse({'status': 'error', 'message': 'Apenas o dono pode criar cargos.'}, status=403)

    data = _parse_request_data(request)
    name = (data.get('name') or '').strip()
    color = (data.get('color') or '#8a63d2').strip()
    try:
        position = int(data.get('position', server.roles.count()))
    except (TypeError, ValueError):
        position = server.roles.count()

    if not name:
        return JsonResponse({'status': 'error', 'message': 'Informe o nome do cargo.'}, status=400)

    if ServerRole.objects.filter(server=server, name__iexact=name).exists():
        return JsonResponse({'status': 'error', 'message': 'Já existe um cargo com esse nome.'}, status=400)

    role = ServerRole.objects.create(
        server=server,
        name=name[:50],
        color=color[:20] or '#8a63d2',
        position=position,
        can_manage_channels=bool(data.get('can_manage_channels', False)),
        can_kick_members=bool(data.get('can_kick_members', False)),
        can_manage_roles=bool(data.get('can_manage_roles', False)),
    )

    return JsonResponse({'status': 'success', 'role': role.to_dict()})


@login_required
@require_POST
def delete_server_role(request, server_id, role_id):
    server = _get_accessible_server_or_404(request.user, server_id)
    if not _user_can_manage_roles(request.user, server):
        return JsonResponse({'status': 'error', 'message': 'Apenas o dono pode excluir cargos.'}, status=403)

    role = get_object_or_404(ServerRole, id=role_id, server=server)
    role.delete()
    return JsonResponse({'status': 'success'})


@login_required
@require_POST
def assign_member_role(request, server_id, user_id):
    server = _get_accessible_server_or_404(request.user, server_id)
    if not _user_can_manage_roles(request.user, server):
        return JsonResponse({'status': 'error', 'message': 'Apenas o dono pode atribuir cargos.'}, status=403)

    target_user = get_object_or_404(User, id=user_id)
    data = _parse_request_data(request)
    role_id = data.get('role_id')

    server.members.add(target_user)
    membership, _ = ServerMember.objects.get_or_create(server=server, user=target_user)

    if role_id in (None, '', 'none', 'null', 0, '0'):
        membership.role = None
    else:
        role = get_object_or_404(ServerRole, id=int(role_id), server=server)
        membership.role = role

    membership.save(update_fields=['role'])
    return JsonResponse({
        'status': 'success',
        'member': _serialize_server_member(request, server, membership),
    })


@login_required
@require_POST
def add_server_member(request, server_id):
    server = _get_accessible_server_or_404(request.user, server_id)
    if not _user_can_manage_roles(request.user, server) and not _user_can_kick_members(request.user, server):
        return JsonResponse({'status': 'error', 'message': 'Sem permissão para adicionar membros.'}, status=403)

    data = _parse_request_data(request)
    username = (data.get('username') or '').strip()
    role_id = data.get('role_id')

    if not username:
        return JsonResponse({'status': 'error', 'message': 'Informe o nome de usuário.'}, status=400)

    target_user = get_object_or_404(User, username__iexact=username)
    server.members.add(target_user)
    membership, _ = ServerMember.objects.get_or_create(server=server, user=target_user)

    if role_id not in (None, '', 'none', 'null'):
        role = get_object_or_404(ServerRole, id=int(role_id), server=server)
        membership.role = role
        membership.save(update_fields=['role'])

    return JsonResponse({
        'status': 'success',
        'member': _serialize_server_member(request, server, membership),
    })


@login_required
@require_POST
def kick_server_member(request, server_id, user_id):
    server = _get_accessible_server_or_404(request.user, server_id)
    if not _user_can_kick_members(request.user, server):
        return JsonResponse({'status': 'error', 'message': 'Sem permissão para expulsar membros.'}, status=403)

    target_user = get_object_or_404(User, id=user_id)
    if target_user.id == server.owner_id:
        return JsonResponse({'status': 'error', 'message': 'Não é possível expulsar o dono do servidor.'}, status=400)

    server.members.remove(target_user)
    ServerMember.objects.filter(server=server, user=target_user).delete()

    _notify_user_group(
        target_user.id,
        'server_kicked',
        {'server_id': server.id, 'server_name': server.name},
        username=target_user.username,
    )

    return JsonResponse({'status': 'success', 'kicked_user_id': target_user.id})


@login_required
@require_POST
def update_profile(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    prev_song = (profile.spotify_song or '').strip()
    prev_artist = (profile.spotify_artist or '').strip()
    prev_music_url = (profile.music_url or '').strip()
    prev_audio_url = (profile.music_audio_url or '').strip()
    prev_cover_url = (profile.music_cover_url or '').strip()

    profile.bubble = (request.POST.get('bubble') if 'bubble' in request.POST else profile.bubble) or ''
    profile.pronouns = (request.POST.get('pronouns') if 'pronouns' in request.POST else profile.pronouns) or ''
    profile.bio = (request.POST.get('bio') if 'bio' in request.POST else profile.bio) or ''
    profile.spotify_song = (request.POST.get('spotify_song') if 'spotify_song' in request.POST else profile.spotify_song) or ''
    profile.spotify_artist = (request.POST.get('spotify_artist') if 'spotify_artist' in request.POST else profile.spotify_artist) or ''

    if 'accent_color' in request.POST:
        profile.accent_color = _sanitize_accent_color(
            request.POST.get('accent_color'),
            request.user,
            fallback=profile.accent_color or '#ff9de2',
        )
    else:
        profile.accent_color = _sanitize_accent_color(profile.accent_color, request.user)

    akiira_admin = is_akiira_admin(request.user)
    if akiira_admin:
        if 'frame_effect' in request.POST and hasattr(profile, 'frame_effect'):
            profile.frame_effect = _sanitize_frame_effect(request.POST.get('frame_effect'), request.user)
        elif hasattr(profile, 'frame_effect'):
            profile.frame_effect = _sanitize_frame_effect(profile.frame_effect, request.user)

        if 'profile_glow' in request.POST and hasattr(profile, 'profile_glow'):
            profile.profile_glow = request.POST.get('profile_glow') in {'1', 'true', 'True', 'on'}

        if 'avatar_pulse' in request.POST and hasattr(profile, 'avatar_pulse'):
            profile.avatar_pulse = request.POST.get('avatar_pulse') in {'1', 'true', 'True', 'on'}

        if 'name_style' in request.POST and hasattr(profile, 'name_style'):
            profile.name_style = (request.POST.get('name_style') or '').strip()[:50]
    else:
        if hasattr(profile, 'frame_effect'):
            profile.frame_effect = 'none'
        if hasattr(profile, 'profile_glow'):
            profile.profile_glow = False
        if hasattr(profile, 'avatar_pulse'):
            profile.avatar_pulse = False
        if hasattr(profile, 'name_style'):
            profile.name_style = ''

    if 'music_url' in request.POST:
        profile.music_url = _sanitize_external_url(request.POST.get('music_url'))

    audio_uploaded = 'music_audio' in request.FILES
    cover_uploaded = 'music_cover' in request.FILES

    if audio_uploaded:
        profile.music_audio_file = request.FILES['music_audio']
        profile.music_audio_url = ''
    elif 'music_audio_url' in request.POST:
        audio_val = (request.POST.get('music_audio_url') or '').strip()
        if not audio_val or audio_val.startswith('blob:'):
            profile.music_audio_url = ''
            profile.music_audio_file = None
        else:
            safe_audio = _sanitize_external_url(audio_val)
            profile.music_audio_url = safe_audio
            if not safe_audio.startswith('/media/'):
                profile.music_audio_file = None

    if cover_uploaded:
        profile.music_cover_file = request.FILES['music_cover']
        profile.music_cover_url = ''
    elif 'music_cover_url' in request.POST:
        cover_val = (request.POST.get('music_cover_url') or '').strip()
        if not cover_val or cover_val.startswith('blob:'):
            profile.music_cover_url = ''
            profile.music_cover_file = None
        else:
            safe_cover = _sanitize_external_url(cover_val)
            profile.music_cover_url = safe_cover
            if not safe_cover.startswith('/media/'):
                profile.music_cover_file = None

    if 'avatar' in request.FILES:
        profile.avatar = request.FILES['avatar']
    if 'banner' in request.FILES:
        profile.banner = request.FILES['banner']

    profile.save()

    track_changed = any([
        profile.spotify_song.strip() != prev_song,
        profile.spotify_artist.strip() != prev_artist,
        profile.music_url.strip() != prev_music_url,
        profile.music_audio_url.strip() != prev_audio_url,
        profile.music_cover_url.strip() != prev_cover_url,
        audio_uploaded,
        cover_uploaded,
    ])
    has_track_data = any([
        profile.spotify_song.strip(),
        profile.spotify_artist.strip(),
        profile.music_url.strip(),
        bool(profile.music_audio_file),
        bool(profile.music_audio_url.strip()),
    ])

    raw_save_lib = request.POST.get('save_to_library')
    should_save_library = (raw_save_lib == '1') or (raw_save_lib is None and track_changed and has_track_data)
    if should_save_library and track_changed and has_track_data:
        SavedMusic.objects.create(
            user=request.user,
            title=profile.spotify_song.strip(),
            artist=profile.spotify_artist.strip(),
            music_url=profile.music_url.strip(),
            audio_file=profile.music_audio_file,
            audio_url='' if profile.music_audio_file else profile.music_audio_url.strip(),
            cover_file=profile.music_cover_file,
            cover_url='' if profile.music_cover_file else profile.music_cover_url.strip(),
        )

    music_data = _build_music_payload(request, profile)
    return JsonResponse({
        'status': 'success',
        'message': 'Perfil atualizado com sucesso!',
        'username': request.user.username,
        'bubble': profile.bubble or '',
        'pronouns': profile.pronouns or '',
        'bio': profile.bio or '',
        'spotify_song': profile.spotify_song or '',
        'spotify_artist': profile.spotify_artist or '',
        'music_url': profile.music_url or '',
        'accent_color': profile.accent_color,
        'avatarUrl': request.build_absolute_uri(profile.avatar.url) if profile.avatar else '',
        'bannerUrl': request.build_absolute_uri(profile.banner.url) if profile.banner else '',
        'is_admin': is_admin_user(request.user),
        'is_akiira_admin': akiira_admin,
        'frame_effect': _sanitize_frame_effect(getattr(profile, 'frame_effect', 'none'), request.user),
        'profile_glow': bool(getattr(profile, 'profile_glow', False)) if akiira_admin else False,
        'avatar_pulse': bool(getattr(profile, 'avatar_pulse', False)) if akiira_admin else False,
        'name_style': (getattr(profile, 'name_style', '') or '') if akiira_admin else '',
        **music_data,
    })


@login_required
def get_user_profile(request, username):
    target_user = get_object_or_404(User, username=username)
    profile, _ = UserProfile.objects.get_or_create(user=target_user)
    music_data = _build_music_payload(request, profile)
    safe_color = _sanitize_accent_color(profile.accent_color, target_user)
    akiira_admin = is_akiira_admin(target_user)

    data = {
        'username': target_user.username,
        'bubble': profile.bubble,
        'pronouns': profile.pronouns,
        'bio': profile.bio,
        'spotify_song': profile.spotify_song,
        'spotify_artist': profile.spotify_artist,
        'accent_color': safe_color,
        'is_admin': is_admin_user(target_user),
        'is_akiira_admin': akiira_admin,
        'frame_effect': _sanitize_frame_effect(getattr(profile, 'frame_effect', 'none'), target_user),
        'profile_glow': bool(getattr(profile, 'profile_glow', False)) if akiira_admin else False,
        'avatar_pulse': bool(getattr(profile, 'avatar_pulse', False)) if akiira_admin else False,
        'name_style': (getattr(profile, 'name_style', '') or '') if akiira_admin else '',
        'avatarUrl': request.build_absolute_uri(profile.avatar.url) if profile.avatar else '',
        'bannerUrl': request.build_absolute_uri(profile.banner.url) if profile.banner else '',
        'is_friend': are_friends(request.user, target_user) if target_user != request.user else False,
        **music_data,
    }
    return JsonResponse(data)


@login_required
def friend_music_feed(request):
    friends = _get_accepted_friend_users(request.user)
    feed = []
    for friend in friends:
        profile, _ = UserProfile.objects.get_or_create(user=friend)
        music_data = _build_music_payload(request, profile)
        recent_tracks = []
        for m in SavedMusic.objects.filter(user=friend)[:10]:
            a_url = request.build_absolute_uri(m.audio_file.url) if m.audio_file else (m.audio_url or '')
            c_url = request.build_absolute_uri(m.cover_file.url) if m.cover_file else (m.cover_url or '')
            recent_tracks.append({
                'id': m.id,
                'title': m.title or 'Faixa sem título',
                'artist': m.artist or 'Artista',
                'music_url': m.music_url or '',
                'audio_url': a_url,
                'cover_url': c_url,
            })

        feed.append({
            'username': friend.username,
            'avatarUrl': request.build_absolute_uri(profile.avatar.url) if profile.avatar else '',
            'spotify_song': profile.spotify_song,
            'spotify_artist': profile.spotify_artist,
            **music_data,
            'saved_musics': recent_tracks,
        })

    return JsonResponse({'status': 'success', 'friends_music': feed})


@login_required
def list_saved_musics(request):
    items = SavedMusic.objects.filter(user=request.user)[:30]
    payload = []
    for m in items:
        a_url = request.build_absolute_uri(m.audio_file.url) if m.audio_file else (m.audio_url or '')
        c_url = request.build_absolute_uri(m.cover_file.url) if m.cover_file else (m.cover_url or '')
        payload.append({
            'id': m.id,
            'title': m.title or 'Faixa sem título',
            'artist': m.artist or 'Artista',
            'music_url': m.music_url or '',
            'audio_url': a_url,
            'cover_url': c_url,
        })
    return JsonResponse({'status': 'success', 'musics': payload})


@login_required
@require_POST
def create_saved_music(request):
    title = (request.POST.get('title') or '').strip()
    artist = (request.POST.get('artist') or '').strip()
    music_url = _sanitize_external_url(request.POST.get('music_url'))
    audio_url = _sanitize_external_url(request.POST.get('audio_url'))
    cover_url = _sanitize_external_url(request.POST.get('cover_url'))
    audio_file = request.FILES.get('audio_file')
    cover_file = request.FILES.get('cover_file')

    if not any([title, artist, music_url, audio_url, audio_file]):
        return JsonResponse({'status': 'error', 'message': 'Informe nome da música, link ou arquivo MP3.'}, status=400)

    m = SavedMusic.objects.create(
        user=request.user,
        title=title[:200],
        artist=artist[:200],
        music_url=music_url[:500],
        audio_file=audio_file,
        audio_url='' if audio_file else audio_url[:500],
        cover_file=cover_file,
        cover_url='' if cover_file else cover_url[:500],
    )

    a_url = request.build_absolute_uri(m.audio_file.url) if m.audio_file else (m.audio_url or '')
    c_url = request.build_absolute_uri(m.cover_file.url) if m.cover_file else (m.cover_url or '')

    return JsonResponse({
        'status': 'success',
        'music': {
            'id': m.id,
            'title': m.title or 'Faixa sem título',
            'artist': m.artist or 'Artista',
            'music_url': m.music_url or '',
            'audio_url': a_url,
            'cover_url': c_url,
        }
    })


@login_required
@require_POST
def delete_saved_music(request, music_id):
    deleted, _ = SavedMusic.objects.filter(id=music_id, user=request.user).delete()
    if not deleted:
        return JsonResponse({'status': 'error', 'message': 'Música não encontrada.'}, status=404)
    return JsonResponse({'status': 'success'})


def register_view(request):
    if request.user.is_authenticated:
        return redirect('room_home')

    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''
        password_confirm = request.POST.get('password_confirm')

        if not username or not password:
            messages.error(request, 'Preencha nome de utilizador e senha.')
        elif len(password) < 6:
            messages.error(request, 'A senha deve ter pelo menos 6 caracteres.')
        elif password_confirm is not None and password != password_confirm:
            messages.error(request, 'As senhas não coincidem.')
        elif User.objects.filter(username__iexact=username).exists():
            messages.error(request, 'Este nome de utilizador já está em uso.')
        else:
            user = User.objects.create_user(username=username, password=password)
            UserProfile.objects.get_or_create(user=user)
            login(request, user)
            return redirect('room_home')

    return render(request, 'chat/register.html')


def login_view(request):
    if request.user.is_authenticated:
        return redirect('room_home')

    next_url = request.POST.get('next') or request.GET.get('next') or ''

    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect('room_home')
        messages.error(request, 'Utilizador ou senha inválidos.')

    return render(request, 'chat/login.html', {'next': next_url})


def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def search_users(request):
    query = (request.GET.get('q') or '').strip()
    if not query:
        return JsonResponse({'status': 'success', 'users': []})

    matches = User.objects.filter(username__icontains=query).exclude(id=request.user.id).select_related('profile')[:15]
    results = []
    for candidate in matches:
        profile = getattr(candidate, 'profile', None)
        already_friends = are_friends(request.user, candidate)
        pending_sent = FriendRequest.objects.filter(
            sender=request.user, receiver=candidate, status='pending'
        ).exists()
        pending_received = FriendRequest.objects.filter(
            sender=candidate, receiver=request.user, status='pending'
        ).exists()

        results.append({
            'username': candidate.username,
            'avatarUrl': request.build_absolute_uri(profile.avatar.url) if (profile and profile.avatar) else '',
            'bubble': (profile.bubble if profile else '') or '',
            'is_friend': already_friends,
            'pending_sent': pending_sent,
            'pending_received': pending_received,
        })

    return JsonResponse({'status': 'success', 'users': results})


@login_required
@require_POST
def send_friend_request(request):
    try:
        data = _parse_request_data(request)
        receiver_username = (data.get('username') or '').strip()

        if not receiver_username:
            return JsonResponse({'status': 'error', 'message': 'Digite o nome de usuário.'}, status=400)

        receiver = get_object_or_404(User, username=receiver_username)

        if receiver == request.user:
            return JsonResponse({'status': 'error', 'message': 'Não podes enviar pedido para ti mesmo.'}, status=400)

        if are_friends(request.user, receiver):
            return JsonResponse({'status': 'error', 'message': f'{receiver.username} já é teu amigo.'}, status=400)

        existing = FriendRequest.objects.filter(sender=request.user, receiver=receiver).first()
        if existing:
            if existing.status == 'pending':
                return JsonResponse({'status': 'success', 'message': f'Pedido de amizade já enviado para {receiver.username}.'})
            existing.status = 'pending'
            existing.save(update_fields=['status', 'updated_at'])
            req_obj = existing
        else:
            req_obj = FriendRequest.objects.create(sender=request.user, receiver=receiver, status='pending')

        _notify_user_group(
            receiver.id,
            'friend_request_received',
            {'id': req_obj.id, 'sender': request.user.username},
            username=receiver.username,
        )

        return JsonResponse({'status': 'success', 'message': f'Pedido de amizade enviado para {receiver.username}!'})
    except Http404:
        return JsonResponse({'status': 'error', 'message': 'Utilizador não encontrado.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required
def get_friend_requests(request):
    requests_qs = FriendRequest.objects.filter(receiver=request.user, status='pending').select_related('sender')
    data = [{'id': req.id, 'sender': req.sender.username} for req in requests_qs]
    return JsonResponse({'status': 'success', 'requests': data})


@login_required
@require_POST
def accept_friend_request(request):
    try:
        data = _parse_request_data(request)
        request_id = data.get('request_id')

        friend_req = get_object_or_404(FriendRequest, id=request_id, receiver=request.user)
        friend_req.status = 'accepted'
        friend_req.save(update_fields=['status', 'updated_at'])

        if is_admin_user(request.user):
            owned = Server.objects.filter(owner=request.user)
            for srv in owned:
                srv.members.add(friend_req.sender)
                ServerMember.objects.get_or_create(server=srv, user=friend_req.sender)

        _notify_user_group(
            friend_req.sender.id,
            'friend_request_accepted',
            {'id': friend_req.id, 'accepter': request.user.username},
            username=friend_req.sender.username,
        )

        return JsonResponse({'status': 'success', 'message': 'Amizade aceite com sucesso!'})
    except Http404:
        return JsonResponse({'status': 'error', 'message': 'Pedido de amizade não encontrado.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required
@require_POST
def reject_friend_request(request):
    try:
        data = _parse_request_data(request)
        request_id = data.get('request_id')
        friend_req = get_object_or_404(FriendRequest, id=request_id, receiver=request.user, status='pending')
        friend_req.status = 'rejected'
        friend_req.save(update_fields=['status', 'updated_at'])
        return JsonResponse({'status': 'success', 'message': 'Pedido recusado.'})
    except Http404:
        return JsonResponse({'status': 'error', 'message': 'Pedido de amizade não encontrado.'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required
def get_friends_list(request):
    try:
        friend_users = _get_accepted_friend_users(request.user)
        friends = []
        for u in friend_users:
            prof = getattr(u, 'profile', None)
            avatar_url = ''
            if prof and prof.avatar:
                try:
                    avatar_url = request.build_absolute_uri(prof.avatar.url)
                except Exception:
                    avatar_url = ''
            friends.append({
                'id': u.id,
                'username': u.username,
                'avatarUrl': avatar_url,
                'bubble': (prof.bubble or '') if prof else '',
                'pronouns': (prof.pronouns or '') if prof else '',
                'spotify_song': (prof.spotify_song or '') if prof else '',
                'spotify_artist': (prof.spotify_artist or '') if prof else '',
                'accent_color': (prof.accent_color or '#ff9de2') if prof else '#ff9de2',
            })
        return JsonResponse({'status': 'success', 'friends': friends})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required
@require_POST
def remove_friend(request):
    try:
        data = _parse_request_data(request)
        target_username = (data.get('username') or '').strip()
        target_user = get_object_or_404(User, username=target_username)
        FriendRequest.objects.filter(
            models.Q(sender=request.user, receiver=target_user)
            | models.Q(sender=target_user, receiver=request.user)
        ).delete()
        return JsonResponse({'status': 'success', 'message': 'Amizade removida.'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required
def get_dm_history(request, username):
    other_user = get_object_or_404(User, username=username)
    if other_user == request.user:
        return JsonResponse({'status': 'error', 'message': 'Não é possível abrir DM consigo mesmo.'}, status=400)

    dms = (
        PrivateMessage.objects.filter(
            (models.Q(sender=request.user) & models.Q(receiver=other_user))
            | (models.Q(sender=other_user) & models.Q(receiver=request.user))
        )
        .select_related('sender', 'receiver')
        .order_by('timestamp', 'id')[:100]
    )
    return JsonResponse({
        'status': 'success',
        'messages': [m.to_dict() for m in dms],
    })


@login_required
@require_POST
def send_message(request):
    data = _parse_request_data(request)
    content = (data.get('content') or '').strip()
    channel_id = data.get('channel_id')
    client_id = (data.get('client_id') or '').strip()[:100]
    reply_to_id = data.get('reply_to')

    if not content:
        return JsonResponse({'status': 'error', 'message': 'Mensagem vazia.'}, status=400)

    channel = None
    if channel_id is not None:
        channel_qs = Channel.objects.filter(id=channel_id)
        if not is_admin_user(request.user):
            channel_qs = channel_qs.filter(
                models.Q(server__members=request.user) | models.Q(server__owner=request.user)
            ).distinct()
        channel = get_object_or_404(channel_qs)

    reply_to = Message.objects.filter(id=reply_to_id).first() if reply_to_id else None

    msg = Message.objects.create(
        user=request.user,
        channel=channel,
        reply_to=reply_to,
        content=content,
        client_id=client_id,
    )

    payload = msg.to_dict()
    channel_layer = get_channel_layer()
    if channel_layer:
        srv_token, ch_token = _channel_ws_tokens(channel)
        group_name = build_channel_group_name(srv_token, ch_token)
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                'type': 'chat_message',
                'message': payload,
            },
        )

    return JsonResponse({'status': 'success', 'message': payload})


@login_required
@require_POST
def delete_message(request, message_id):
    msg = get_object_or_404(Message, id=message_id)
    if msg.user_id != request.user.id and not is_admin_user(request.user):
        return JsonResponse({'status': 'error', 'message': 'Sem permissão para apagar esta mensagem.'}, status=403)
    msg.delete()
    return JsonResponse({'status': 'success', 'deleted_id': message_id})


@login_required
@require_POST
def delete_dm_message(request, message_id):
    msg = get_object_or_404(PrivateMessage, id=message_id)
    if msg.sender_id != request.user.id and not is_admin_user(request.user):
        return JsonResponse({'status': 'error', 'message': 'Sem permissão para apagar esta DM.'}, status=403)
    msg.delete()
    return JsonResponse({'status': 'success', 'deleted_id': message_id})


@login_required
@require_POST
def upload_attachment(request):
    uploaded = request.FILES.get('file') or request.FILES.get('attachment')
    if not uploaded:
        return JsonResponse({'status': 'error', 'message': 'Nenhum arquivo enviado.'}, status=400)

    if uploaded.size > MAX_FILE_SIZE:
        return JsonResponse({'status': 'error', 'message': 'Arquivo excede o limite de 10 MB.'}, status=400)

    ext = os.path.splitext(uploaded.name or '')[1].lower()
    if ext in IMAGE_EXTS:
        att_type = 'image'
    elif ext in AUDIO_EXTS:
        att_type = 'audio'
    elif ext in DOC_EXTS:
        att_type = 'document'
    else:
        return JsonResponse({'status': 'error', 'message': 'Formato de arquivo não suportado.'}, status=400)

    channel_id = request.POST.get('channel_id')
    dm_recipient_username = (request.POST.get('recipient') or '').strip()
    content = (request.POST.get('content') or '').strip()
    client_id = (request.POST.get('client_id') or '').strip()[:100]

    channel_layer = get_channel_layer()

    if dm_recipient_username:
        recipient = get_object_or_404(User, username=dm_recipient_username)
        dm = PrivateMessage.objects.create(
            sender=request.user,
            receiver=recipient,
            content=content,
            attachment=uploaded,
            attachment_type=att_type,
            client_id=client_id,
        )
        payload = dm.to_dict()
        if channel_layer:
            dm_group = build_dm_group_name(request.user.username, recipient.username)
            async_to_sync(channel_layer.group_send)(dm_group, {'type': 'dm_message', 'message': payload})
        return JsonResponse({'status': 'success', 'message': payload})

    channel = None
    if channel_id:
        channel_qs = Channel.objects.filter(id=channel_id)
        if not is_admin_user(request.user):
            channel_qs = channel_qs.filter(
                models.Q(server__members=request.user) | models.Q(server__owner=request.user)
            ).distinct()
        channel = get_object_or_404(channel_qs)

    msg = Message.objects.create(
        user=request.user,
        channel=channel,
        content=content,
        attachment=uploaded,
        attachment_type=att_type,
        client_id=client_id,
    )
    payload = msg.to_dict()
    if channel_layer:
        srv_token, ch_token = _channel_ws_tokens(channel)
        group_name = build_channel_group_name(srv_token, ch_token)
        async_to_sync(channel_layer.group_send)(group_name, {'type': 'chat_message', 'message': payload})

    return JsonResponse({'status': 'success', 'message': payload})
