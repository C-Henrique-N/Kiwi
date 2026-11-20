import json
import os
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import models
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from .models import UserProfile, FriendRequest, Message, PrivateMessage

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
AUDIO_EXTS = {'.mp3', '.wav', '.ogg', '.webm', '.m4a'}
DOC_EXTS = {'.pdf', '.doc', '.docx', '.txt', '.zip', '.rar', '.csv', '.xls', '.xlsx', '.ppt', '.pptx'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def are_friends(user1, user2):
    return FriendRequest.objects.filter(
        (models.Q(sender=user1, receiver=user2) | models.Q(sender=user2, receiver=user1)) &
        models.Q(status='accepted')
    ).exists()


@login_required(login_url='login')
def room_view(request, room_name='geral'):
    UserProfile.objects.get_or_create(user=request.user)
    messages = Message.objects.select_related('user', 'user__profile').order_by('timestamp')[:50]

    return render(
        request, 'chat/room.html', {
            'room_name': room_name,
            'username': request.user.username,
            'messages': messages
        }
    )


@login_required
from .models import UserProfile, FriendRequest, Message, PrivateMessage, SavedMusic


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
        profile = request.user.profile
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

        # Guarda automaticamente no acervo de músicas do utilizador se houver faixa/áudio/capa
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
    profile, created = UserProfile.objects.get_or_create(user=target_user)
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

@login_required
def get_user_profile(request, username):
    target_user = get_object_or_404(User, username=username)
    profile, created = UserProfile.objects.get_or_create(user=target_user)

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
    }
    return JsonResponse(data)


def register_view(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        if username and password:
            if not User.objects.filter(username=username).exists():
                user = User.objects.create_user(username=username, password=password)
                login(request, user)
                return redirect('room')
    return render(request, 'chat/register.html')


def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username')
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
def send_friend_request(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            receiver_username = data.get('username')
            receiver = get_object_or_404(User, username=receiver_username)

            if receiver == request.user:
                return JsonResponse({'status': 'error', 'message': 'Não podes enviar pedido para ti mesmo.'}, status=400)

            existing = FriendRequest.objects.filter(sender=request.user, receiver=receiver, status='pending').exists()
            if not existing:
                FriendRequest.objects.create(sender=request.user, receiver=receiver)

            return JsonResponse({'status': 'success', 'message': f'Pedido de amizade enviado para {receiver_username}!'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    return JsonResponse({'status': 'error', 'message': 'Método inválido.'}, status=400)


@login_required
def get_friend_requests(request):
    requests = FriendRequest.objects.filter(receiver=request.user, status='pending')
    data = [{'id': req.id, 'sender': req.sender.username} for req in requests]
    return JsonResponse({'status': 'success', 'requests': data})


@login_required
def accept_friend_request(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            request_id = data.get('request_id')
            friend_req = get_object_or_404(FriendRequest, id=request_id, receiver=request.user)
            friend_req.status = 'accepted'
            friend_req.save()
            return JsonResponse({'status': 'success', 'message': 'Amizade aceite com sucesso!'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    return JsonResponse({'status': 'error', 'message': 'Método inválido.'}, status=400)


@login_required
def get_friends_list(request):
    try:
        accepted_requests = FriendRequest.objects.filter(
            (models.Q(sender=request.user) | models.Q(receiver=request.user)) &
            models.Q(status='accepted')
        ).distinct()

        friends_set = set()
        friends = []
        for req in accepted_requests:
            friend_user = req.receiver if req.sender == request.user else req.sender
            if friend_user.username not in friends_set:
                friends_set.add(friend_user.username)
                profile, _ = UserProfile.objects.get_or_create(user=friend_user)
                friends.append({
                    'username': friend_user.username,
                    'avatarUrl': request.build_absolute_uri(profile.avatar.url) if profile.avatar else ''
                })

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
            status=400
        )

    target = request.POST.get('target', 'geral').strip()
    content = request.POST.get('content', '').strip()

    try:
        if target == 'geral' or not target:
            msg = Message.objects.create(
                user=request.user,
                content=content,
                attachment=file,
                attachment_type=att_type,
            )
        else:
            other_user = get_object_or_404(User, username=target)
            if not are_friends(request.user, other_user):
                return JsonResponse(
                    {'status': 'error', 'message': 'Precisam de ser amigos para enviar anexos em DM.'},
                    status=403
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
        data = json.loads(request.body)
        target_username = (data.get('username') or '').strip()
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