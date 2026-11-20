import json
import os
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import models
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from .models import (
    Channel,
    FriendRequest,
    Message,
    PrivateMessage,
    SavedMusic,
    Server,
    UserProfile,
)

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
AUDIO_EXTS = {'.mp3', '.wav', '.ogg', '.webm', '.m4a'}
DOC_EXTS = {'.pdf', '.doc', '.docx', '.txt', '.zip', '.rar', '.csv', '.xls', '.xlsx', '.ppt', '.pptx'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def is_admin_user(user):
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def are_friends(user1, user2):
    return FriendRequest.are_friends(user1, user2)


def _get_accepted_friend_users(user):
    accepted = FriendRequest.objects.filter(
        (models.Q(sender=user) | models.Q(receiver=user)) &
        models.Q(status='accepted')
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


def _notify_user_group(user_id, event_type, payload):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    async_to_sync(channel_layer.group_send)(
        f'user_{int(user_id)}',
        {
            'type': 'friend_event',
            'event': event_type,
            'payload': payload,
        },
    )


@login_required(login_url='login')
def room_view(request, server_id=None, channel_id=None):
    UserProfile.objects.get_or_create(user=request.user)
    servers = Server.objects.filter(members=request.user).prefetch_related('channels')

    active_server = None
    active_channel = None
    messages = []

    if server_id is not None:
        active_server = get_object_or_404(Server, id=server_id, members=request.user)
        channels = list(active_server.channels.all())
        if channel_id is not None:
            active_channel = get_object_or_404(Channel, id=channel_id, server=active_server)
        elif channels:
            active_channel = channels[0]

        if active_channel:
            messages = list(
                Message.objects.filter(channel=active_channel)
                .select_related('user', 'user__profile')
                .order_by('timestamp')[:50]
            )

    return render(
        request,
        'chat/room.html',
        {
            'username': request.user.username,
            'is_admin': is_admin_user(request.user),
            'servers': servers,
            'active_server': active_server,
            'active_channel': active_channel,
            'messages': messages,
        },
    )


@login_required
@require_POST
def create_server(request):
    if not is_admin_user(request.user):
        return JsonResponse(
            {'status': 'error', 'message': 'Apenas administradores podem criar servidores no momento.'},
            status=403,
        )

    name = (request.POST.get('name') or '').strip()
    icon = request.FILES.get('icon')
    raw_channels = (request.POST.get('channels') or '').strip()

    if not name:
        return JsonResponse({'status': 'error', 'message': 'Informe o nome do servidor.'}, status=400)

    server = Server.objects.create(name=name, icon=icon, owner=request.user)

    friends = _get_accepted_friend_users(request.user)
    if friends:
        server.members.add(*friends)

    channel_names = [c.strip().lstrip('#').lower().replace(' ', '-') for c in raw_channels.split(',') if c.strip()]
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
            'icon_url': icon_url,
            'channels': created_channels,
        },
    })


@login_required
@require_POST
def create_channel(request, server_id):
    if not is_admin_user(request.user):
        return JsonResponse(
            {'status': 'error', 'message': 'Apenas administradores podem criar canais.'},
            status=403,
        )

    server = get_object_or_404(Server, id=server_id, owner=request.user)
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


@login_required
def update_profile(request):
    if request.method == 'POST':
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        profile.bubble = request.POST.get('bubble', profile.bubble)
        profile.pronouns = request.POST.get('pronouns', profile.pronouns)
        profile.bio = request.POST.get('bio', profile.bio)
        profile.spotify_song = request.POST.get('spotify_song', profile.spotify_song)
        profile.spotify_artist = request.POST.get('spotify_artist', profile.spotify_artist)
        profile.accent_color = request.POST.get('accent_color', profile.accent_color)

        if 'music_url' in request.POST:
            profile.music_url = request.POST.get('music_url', '').strip()

        if 'music_audio' in request.FILES:
            profile.music_audio_file = request.FILES['music_audio']
            profile.music_audio_url = ''
        elif 'music_audio_url' in request.POST:
            audio_val = request.POST.get('music_audio_url', '').strip()
            if audio_val and not audio_val.startswith('blob:'):
                profile.music_audio_url = audio_val
                if not audio_val.startswith('/media/'):
                    profile.music_audio_file = None

        if 'music_cover' in request.FILES:
            profile.music_cover_file = request.FILES['music_cover']
            profile.music_cover_url = ''
        elif 'music_cover_url' in request.POST:
            cover_val = request.POST.get('music_cover_url', '').strip()
            if cover_val and not cover_val.startswith('blob:'):
                profile.music_cover_url = cover_val
                if not cover_val.startswith('/media/'):
                    profile.music_cover_file = None

        if 'avatar' in request.FILES:
            profile.avatar = request.FILES['avatar']
        if 'banner' in request.FILES:
            profile.banner = request.FILES['banner']

        profile.save()

        has_track_data = any([
            profile.spotify_song.strip(),
            profile.spotify_artist.strip(),
            profile.music_url.strip(),
            bool(profile.music_audio_file),
            bool(profile.music_audio_url.strip()),
            bool(profile.music_cover_file),
            bool(profile.music_cover_url.strip()),
        ])
        if has_track_data and request.POST.get('save_to_library', '1') == '1':
            SavedMusic.objects.create(
                user=request.user,
                title=profile.spotify_song.strip(),
                artist=profile.spotify_artist.strip(),
                music_url=profile.music_url.strip(),
                audio_file=profile.music_audio_file,
                audio_url=profile.music_audio_url.strip(),
                cover_file=profile.music_cover_file,
                cover_url=profile.music_cover_url.strip(),
            )

        music_data = _build_music_payload(request, profile)
        return JsonResponse({
            'status': 'success',
            'message': 'Perfil atualizado com sucesso!',
            **music_data,
        })

    return JsonResponse({'status': 'error', 'message': 'Método inválido.'}, status=400)


@login_required
def get_user_profile(request, username):
    target_user = get_object_or_404(User, username=username)
    profile, _ = UserProfile.objects.get_or_create(user=target_user)
    music_data = _build_music_payload(request, profile)

    data = {
        'username': target_user.username,
        'bubble': profile.bubble,
        'pronouns': profile.pronouns,
        'bio': profile.bio,
        'spotify_song': profile.spotify_song,
        'spotify_artist': profile.spotify_artist,
        'accent_color': profile.accent_color or '#ff9de2',
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
        profile = friend.profile
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
    music_url = (request.POST.get('music_url') or '').strip()
    audio_url = (request.POST.get('audio_url') or '').strip()
    cover_url = (request.POST.get('cover_url') or '').strip()
    audio_file = request.FILES.get('audio_file')
    cover_file = request.FILES.get('cover_file')

    if not any([title, artist, music_url, audio_url, audio_file]):
        return JsonResponse({'status': 'error', 'message': 'Informe nome da música, link ou arquivo MP3.'}, status=400)

    m = SavedMusic.objects.create(
        user=request.user,
        title=title,
        artist=artist,
        music_url=music_url,
        audio_file=audio_file,
        audio_url='' if audio_file else audio_url,
        cover_file=cover_file,
        cover_url='' if cover_file else cover_url,
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
    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password')
        if username and password:
            if not User.objects.filter(username=username).exists():
                user = User.objects.create_user(username=username, password=password)
                login(request, user)
                return redirect('room')
    return render(request, 'chat/register.html')


def login_view(request):
    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('room')
    return render(request, 'chat/login.html')


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
        if request.content_type and 'application/json' in request.content_type:
            data = json.loads(request.body or '{}')
            receiver_username = (data.get('username') or '').strip()
        else:
            receiver_username = (request.POST.get('username') or '').strip()

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
        )

        return JsonResponse({'status': 'success', 'message': f'Pedido de amizade enviado para {receiver.username}!'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required
def get_friend_requests(request):
    requests = FriendRequest.objects.filter(receiver=request.user, status='pending').select_related('sender')
    data = [{'id': req.id, 'sender': req.sender.username} for req in requests]
    return JsonResponse({'status': 'success', 'requests': data})


@login_required
@require_POST
def accept_friend_request(request):
    try:
        if request.content_type and 'application/json' in request.content_type:
            data = json.loads(request.body or '{}')
            request_id = data.get('request_id')
        else:
            request_id = request.POST.get('request_id')

        friend_req = get_object_or_404(FriendRequest, id=request_id, receiver=request.user)
        friend_req.status = 'accepted'
        friend_req.save(update_fields=['status', 'updated_at'])

        if is_admin_user(request.user):
            owned = Server.objects.filter(owner=request.user)
            for srv in owned:
                srv.members.add(friend_req.sender)

        _notify_user_group(
            friend_req.sender.id,
            'friend_request_accepted',
            {'id': friend_req.id, 'accepter': request.user.username},
        )

        return JsonResponse({'status': 'success', 'message': 'Amizade aceite com sucesso!'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required
def get_friends_list(request):
    try:
        friend_users = _get_accepted_friend_users(request.user)
        friends = [
            {
                'username': u.username,
                'avatarUrl': request.build_absolute_uri(u.profile.avatar.url) if u.profile.avatar else '',
            }
            for u in friend_users
        ]
        return JsonResponse({'status': 'success', 'friends': friends})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@login_required
def get_dm_history(request, username):
    other_user = get_object_or_404(User, username=username)
    if other_user == request.user:
        return JsonResponse({'status': 'error', 'message': 'Não podes abrir DM contigo mesmo.'}, status=400)

    if not are_friends(request.user, other_user):
        return JsonResponse({'status': 'error', 'message': 'Precisam de ser amigos para conversar em privado.'}, status=403)

    msgs = PrivateMessage.objects.filter(
        models.Q(sender=request.user, receiver=other_user) |
        models.Q(sender=other_user, receiver=request.user)
    ).select_related('sender').order_by('timestamp')[:100]

    data = []
    for m in msgs:
        att_field = getattr(m, 'attachment', None) or getattr(m, 'file', None)
        att_url = request.build_absolute_uri(att_field.url) if att_field else ''
        data.append({
            'id': m.id,
            'sender': m.sender.username,
            'message': m.content,
            'attachment_url': att_url,
            'attachment_type': getattr(m, 'attachment_type', '') or '',
            'time': m.timestamp.strftime('%H:%M'),
        })
    return JsonResponse({'status': 'success', 'messages': data})


@login_required
@require_POST
def upload_attachment(request):
    file = request.FILES.get('file')
    if not file:
        return JsonResponse({'status': 'error', 'message': 'Nenhum ficheiro enviado.'}, status=400)

    if file.size > MAX_FILE_SIZE:
        return JsonResponse({'status': 'error', 'message': 'O ficheiro excede 10 MB.'}, status=400)

    ext = os.path.splitext(file.name)[1].lower()
    content_type = (file.content_type or '').lower()

    if ext in IMAGE_EXTS or content_type.startswith('image/'):
        att_type = 'image'
    elif ext in AUDIO_EXTS or content_type.startswith('audio/'):
        att_type = 'audio'
    elif ext in DOC_EXTS:
        att_type = 'document'
    else:
        return JsonResponse(
            {'status': 'error', 'message': 'Formato não suportado (use imagem/GIF, áudio ou documento).'},
            status=400,
        )

    target = (request.POST.get('target') or '').strip()
    channel_id = request.POST.get('channel_id')
    content = (request.POST.get('content') or '').strip()
    avatar_url = (request.POST.get('avatarUrl') or '').strip()

    try:
        channel_layer = get_channel_layer()
        if channel_id:
            channel = get_object_or_404(Channel, id=channel_id, server__members=request.user)
            msg = Message.objects.create(
                user=request.user,
                channel=channel,
                content=content,
                attachment=file,
                attachment_type=att_type,
            )
            att_url = request.build_absolute_uri(msg.attachment.url) if msg.attachment else ''
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f'chat_{channel.id}',
                    {
                        'type': 'chat_message',
                        'id': msg.id,
                        'message': msg.content,
                        'username': request.user.username,
                        'avatarUrl': avatar_url,
                        'attachment_url': att_url,
                        'attachment_type': att_type,
                    },
                )
        elif target:
            other_user = get_object_or_404(User, username=target)
            if not are_friends(request.user, other_user):
                return JsonResponse(
                    {'status': 'error', 'message': 'Precisam de ser amigos para enviar anexos em DM.'},
                    status=403,
                )

            private_kwargs = {
                'sender': request.user,
                'receiver': other_user,
                'content': content,
            }
            private_fields = {f.name for f in PrivateMessage._meta.get_fields()}
            if 'attachment' in private_fields:
                private_kwargs['attachment'] = file
            elif 'file' in private_fields:
                private_kwargs['file'] = file

            if 'attachment_type' in private_fields:
                private_kwargs['attachment_type'] = att_type

            msg = PrivateMessage.objects.create(**private_kwargs)
            att_field = getattr(msg, 'attachment', None) or getattr(msg, 'file', None)
            att_url = request.build_absolute_uri(att_field.url) if att_field else ''
            if channel_layer:
                ids = sorted([request.user.id, other_user.id])
                async_to_sync(channel_layer.group_send)(
                    f'dm_{ids[0]}_{ids[1]}',
                    {
                        'type': 'dm_message',
                        'id': msg.id,
                        'message': msg.content,
                        'sender': request.user.username,
                        'attachment_url': att_url,
                        'attachment_type': att_type,
                    },
                )
        else:
            return JsonResponse({'status': 'error', 'message': 'Selecione um amigo ou canal.'}, status=400)

        return JsonResponse({
            'status': 'success',
            'id': msg.id,
            'attachment_url': att_url,
            'attachment_type': att_type,
            'attachment_name': file.name,
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@login_required(login_url='login')
def profile_page_view(request):
    username = request.GET.get('user', request.user.username)
    target_user = get_object_or_404(User, username=username)
    UserProfile.objects.get_or_create(user=target_user)
    return render(request, 'chat/profile.html', {
        'target_username': target_user.username,
        'is_self': target_user == request.user,
    })


@login_required
@require_POST
def remove_friend(request):
    try:
        if request.content_type and 'application/json' in request.content_type:
            data = json.loads(request.body or '{}')
            target_username = (data.get('username') or '').strip()
        else:
            target_username = (request.POST.get('username') or '').strip()

        if not target_username:
            return JsonResponse({'status': 'error', 'message': 'Utilizador inválido.'}, status=400)

        other_user = get_object_or_404(User, username=target_username)
        deleted_count, _ = FriendRequest.objects.filter(
            (models.Q(sender=request.user, receiver=other_user) | models.Q(sender=other_user, receiver=request.user)) &
            models.Q(status='accepted')
        ).delete()

        if deleted_count == 0:
            return JsonResponse({'status': 'error', 'message': 'Vocês não são amigos.'}, status=400)

        return JsonResponse({'status': 'success', 'message': f'Amizade com {other_user.username} removida.'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)