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
def update_profile(request):
    if request.method == 'POST':
        profile = request.user.profile
        profile.bubble = request.POST.get('bubble', profile.bubble)
        profile.pronouns = request.POST.get('pronouns', profile.pronouns)
        profile.bio = request.POST.get('bio', profile.bio)
        profile.spotify_song = request.POST.get('spotify_song', profile.spotify_song)
        profile.spotify_artist = request.POST.get('spotify_artist', profile.spotify_artist)
        profile.accent_color = request.POST.get('accent_color', profile.accent_color)

        if 'avatar' in request.FILES:
            profile.avatar = request.FILES['avatar']
        if 'banner' in request.FILES:
            profile.banner = request.FILES['banner']

        profile.save()
        return JsonResponse({'status': 'success', 'message': 'Perfil atualizado com sucesso!'})

    return JsonResponse({'status': 'error', 'message': 'Método inválido.'}, status=400)


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

    data = [
        {
            'id': m.id,
            'sender': m.sender.username,
            'message': m.content,
            'attachment_url': request.build_absolute_uri(m.attachment.url) if getattr(m, 'attachment', None) else '',
            'attachment_type': getattr(m, 'attachment_type', '') or '',
            'time': m.timestamp.strftime('%H:%M'),
        }
        for m in msgs
    ]
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
    else:
        return JsonResponse({'status': 'error', 'message': 'Formato não suportado (use imagem/GIF ou áudio).'}, status=400)

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
                return JsonResponse({'status': 'error', 'message': 'Precisam de ser amigos para enviar anexos em DM.'}, status=403)

            msg = PrivateMessage.objects.create(
                sender=request.user,
                receiver=other_user,
                content=content,
                attachment=file,
                attachment_type=att_type,
            )

        return JsonResponse({
            'status': 'success',
            'id': msg.id,
            'attachment_url': request.build_absolute_uri(msg.attachment.url),
            'attachment_type': att_type,
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)