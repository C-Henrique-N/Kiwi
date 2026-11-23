import base64
import json
import os
import re
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template import TemplateDoesNotExist
from django.template.loader import select_template
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import (
    Channel,
    DirectMessage,
    Follow,
    FriendRequest,
    Friendship,
    Message,
    MessageReaction,
    PrivateMessage,
    Profile,
    ProfilePost,
    ProfilePostReply,
    SavedMusic,
    Server,
    ServerMember,
    ServerRole,
    UserProfile,
)

ADMIN_USERNAME = "AkiiraPW"
EXCLUSIVE_TESTER_USERNAME = "AkiiraPW"

STANDARD_ACCENT_COLORS = {
    "#7c3aed",
    "#ffffff",
    "#2b2d31",
    "#ff9de2",
    "#e74c3c",
    "#23a55a",
    "#5865f2",
    "#95a5a6",
}

ALLOWED_STATUSES = {"online", "idle", "dnd", "offline"}
ALLOWED_NAME_EFFECTS = {"none", "neon", "gradient", "gold"}
ALLOWED_AVATAR_FRAMES = {"none", "gold_crown", "neon_ring", "kiwi_leaf"}

SHOP_CATALOG = {
    "crepusculo_berilo": {
        "id": "crepusculo_berilo",
        "name": "Crepúsculo (Berilo)",
        "category": "frame",
        "slot": "frame_effect",
        "value": "crepusculo_berilo",
        "art_url": "",
        "exclusive_to": EXCLUSIVE_TESTER_USERNAME,
    },
    "ondas_lavanda": {
        "id": "ondas_lavanda",
        "name": "Ondas Ambientais (Lavanda)",
        "category": "frame",
        "slot": "frame_effect",
        "value": "ondas_lavanda",
        "art_url": "",
        "exclusive_to": EXCLUSIVE_TESTER_USERNAME,
    },
    "miragem_rosa": {
        "id": "miragem_rosa",
        "name": "Miragem",
        "category": "frame",
        "slot": "frame_effect",
        "value": "miragem_rosa",
        "art_url": "",
        "exclusive_to": EXCLUSIVE_TESTER_USERNAME,
    },
    "sakura_encantada": {
        "id": "sakura_encantada",
        "name": "Sakura Encantada",
        "category": "card_effect",
        "slot": "card_effect",
        "value": "sakura_encantada",
        "art_url": "",
        "exclusive_to": EXCLUSIVE_TESTER_USERNAME,
    },
    "chamas_negras": {
        "id": "chamas_negras",
        "name": "Chamas Negras",
        "category": "card_effect",
        "slot": "card_effect",
        "value": "chamas_negras",
        "art_url": "",
        "exclusive_to": EXCLUSIVE_TESTER_USERNAME,
    },
    "brilho_estelar": {
        "id": "brilho_estelar",
        "name": "Brilho Estelar",
        "category": "card_effect",
        "slot": "card_effect",
        "value": "brilho_estelar",
        "art_url": "",
        "exclusive_to": EXCLUSIVE_TESTER_USERNAME,
    },
    "aura_neon": {
        "id": "aura_neon",
        "name": "Neon Kiwi",
        "category": "name_style",
        "slot": "name_style",
        "value": "neon",
        "art_url": "",
        "exclusive_to": EXCLUSIVE_TESTER_USERNAME,
    },
}


def _can_use_exclusive_shop_item(user, item):
    exclusive_to = (item.get("exclusive_to") or "").strip().lower()
    if not exclusive_to:
        return True
    return bool(user and user.is_authenticated and (user.username or "").strip().lower() == exclusive_to)


def _is_kiwi_admin(user):
    if not user or not user.is_authenticated:
        return False
    return bool(
        user.is_superuser
        or user.is_staff
        or (user.username or "").strip().lower() == ADMIN_USERNAME.lower()
    )


def _format_user_label(user):
    if not user:
        return ""
    profile = _get_or_create_profile(user)
    display = (getattr(profile, "display_name", "") or "").strip()
    return display or user.username


def _sanitize_accent_color(color_value, is_admin=False):
    raw = (color_value or "").strip()
    if raw.lower() == "#ffd700" and not is_admin:
        return "#7c3aed"
    if re.match(r"^#[0-9a-fA-F]{6}$", raw):
        lowered = raw.lower()
        if lowered in STANDARD_ACCENT_COLORS or is_admin:
            return lowered
    return "#ff9de2" if not raw else "#7c3aed"


def _sanitize_status(status_value):
    val = (status_value or "").strip().lower()
    return val if val in ALLOWED_STATUSES else "online"


def _sanitize_name_effect(effect_value, is_admin=False):
    val = (effect_value or "").strip().lower()
    if val not in ALLOWED_NAME_EFFECTS:
        return "none"
    if val == "gold" and not is_admin:
        return "none"
    return val


def _sanitize_avatar_frame(frame_value, is_admin=False):
    val = (frame_value or "").strip().lower()
    if val not in ALLOWED_AVATAR_FRAMES:
        return "none"
    if val == "gold_crown" and not is_admin:
        return "none"
    return val


def _safe_media_url(field_file):
    if not field_file:
        return ""
    try:
        return field_file.url
    except Exception:
        return ""


def _save_base64_media(data_url, prefix="upload"):
    if not data_url or not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    match = re.match(
        r"^data:([a-zA-Z0-9.+/-]+);base64,([A-Za-z0-9+/=\r\n]+)$",
        data_url,
    )
    if not match:
        return None
    mime_type = match.group(1).lower()
    b64_data = match.group(2)

    ext_map = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/ogg": "ogg",
        "audio/wav": "wav",
        "audio/webm": "webm",
    }
    ext = ext_map.get(mime_type)
    if not ext:
        return None

    try:
        raw_bytes = base64.b64decode(b64_data, validate=True)
    except Exception:
        return None

    max_bytes = 8 * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        return None

    filename = f"{prefix}_{timezone.now().strftime('%Y%m%d_%H%M%S_%f')}.{ext}"
    rel_path = os.path.join("kiwi_media", filename)
    saved_path = default_storage.save(rel_path, ContentFile(raw_bytes))
    return default_storage.url(saved_path)


def _get_or_create_profile(user):
    profile, created = UserProfile.objects.get_or_create(user=user)
    if created and not profile.accent_color:
        profile.accent_color = "#ff9de2"
        profile.save(update_fields=["accent_color"])
    return profile


def _attach_follow_stats(payload, target_user, viewer_user=None):
    followers_count = Follow.followers_count_for(target_user)
    following_count = Follow.following_count_for(target_user)
    is_following = (
        Follow.is_following(viewer_user, target_user)
        if viewer_user and viewer_user.is_authenticated
        else False
    )
    payload["followers_count"] = followers_count
    payload["following_count"] = following_count
    payload["is_following"] = is_following
    return payload


def _serialize_profile(user, profile):
    is_admin = _is_kiwi_admin(user)
    avatar_url = _safe_media_url(getattr(profile, "avatar", None))
    banner_url = _safe_media_url(getattr(profile, "banner", None))
    music_url = (
        _safe_media_url(getattr(profile, "music_audio_file", None))
        or getattr(profile, "music_audio_url", "")
        or getattr(profile, "music_url", "")
        or ""
    )
    music_cover_url = (
        _safe_media_url(getattr(profile, "music_cover_file", None))
        or getattr(profile, "music_cover_url", "")
        or ""
    )

    accent = _sanitize_accent_color(getattr(profile, "accent_color", "#ff9de2"), is_admin=is_admin)
    name_effect = _sanitize_name_effect(getattr(profile, "name_effect", "none"), is_admin=is_admin)
    avatar_frame = _sanitize_avatar_frame(getattr(profile, "avatar_frame", "none"), is_admin=is_admin)

    data = {
        "account_id": user.id,
        "user_id": user.id,
        "username": user.username,
        "display_name": (getattr(profile, "display_name", "") or "").strip() or user.username,
        "bio": getattr(profile, "bio", "") or "",
        "pronouns": getattr(profile, "pronouns", "") or "",
        "location": getattr(profile, "location", "") or "",
        "status": _sanitize_status(getattr(profile, "status", "online")),
        "custom_status": getattr(profile, "custom_status", "") or getattr(profile, "bubble", "") or "",
        "bubble": getattr(profile, "bubble", "") or "",
        "accent_color": accent,
        "name_effect": name_effect,
        "avatar_frame": avatar_frame,
        "frame_effect": getattr(profile, "frame_effect", "none") or "none",
        "card_effect": getattr(profile, "card_effect", "none") or "none",
        "profile_glow": bool(getattr(profile, "profile_glow", False)),
        "avatar_pulse": bool(getattr(profile, "avatar_pulse", False)),
        "name_style": getattr(profile, "name_style", "") or "",
        "avatar_url": avatar_url,
        "banner_url": banner_url,
        "music_title": getattr(profile, "spotify_song", "") or "",
        "music_artist": getattr(profile, "spotify_artist", "") or "",
        "spotify_song": getattr(profile, "spotify_song", "") or "",
        "spotify_artist": getattr(profile, "spotify_artist", "") or "",
        "music_url": music_url,
        "music_cover_url": music_cover_url,
        "is_admin": is_admin,
    }
    return _attach_follow_stats(data, user, viewer_user=user)


def _are_friends(user_a, user_b):
    if not user_a or not user_b or user_a.id == user_b.id:
        return False
    if FriendRequest.are_friends(user_a, user_b):
        return True
    return Friendship.objects.filter(
        Q(user1=user_a, user2=user_b) | Q(user1=user_b, user2=user_a)
    ).exists()


def _get_friends_list(user):
    friends = []
    seen_ids = set()

    for item in FriendRequest.get_friends_for_user(user):
        uid = item["id"]
        if uid in seen_ids:
            continue
        seen_ids.add(uid)
        peer = User.objects.filter(id=uid).first()
        if peer:
            friends.append(_serialize_profile(peer, _get_or_create_profile(peer)))

    friendships = Friendship.objects.filter(Q(user1=user) | Q(user2=user)).select_related("user1", "user2")
    for fs in friendships:
        other = fs.user2 if fs.user1_id == user.id else fs.user1
        if other.id in seen_ids:
            continue
        seen_ids.add(other.id)
        friends.append(_serialize_profile(other, _get_or_create_profile(other)))

    return friends


def _ensure_default_workspace(user):
    server, _ = Server.objects.get_or_create(
        name="Kiwi Lounge",
        defaults={"owner": user},
    )
    server.members.add(user)
    ServerMember.objects.get_or_create(server=server, user=user)

    for ch_name in ("geral", "música", "clips"):
        Channel.objects.get_or_create(server=server, name=ch_name)

    servers_qs = Server.objects.filter(members=user).prefetch_related("channels").order_by("created_at", "id")
    if not servers_qs.exists():
        servers_qs = Server.objects.prefetch_related("channels").order_by("created_at", "id")
    return list(servers_qs)


def _message_author(msg):
    return getattr(msg, "user", None) or getattr(msg, "sender", None)


def _format_local_hhmm(msg):
    dt = getattr(msg, "timestamp", None) or getattr(msg, "created_at", None) or timezone.now()
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local_dt.strftime("%H:%M")


def _serialize_message(msg, current_user=None):
    author = _message_author(msg)
    profile = _get_or_create_profile(author)
    sender_data = _serialize_profile(author, profile)

    reactions_summary = {}
    for rx in msg.reactions.all():
        emoji = rx.emoji
        if emoji not in reactions_summary:
            reactions_summary[emoji] = {"emoji": emoji, "count": 0, "me": False, "users": []}
        reactions_summary[emoji]["count"] += 1
        reactions_summary[emoji]["users"].append(rx.user.username)
        if current_user and rx.user_id == current_user.id:
            reactions_summary[emoji]["me"] = True

    ts = _format_local_hhmm(msg)
    return {
        "id": msg.id,
        "client_id": getattr(msg, "client_id", "") or "",
        "channel_id": msg.channel_id,
        "sender": author.username,
        "author": author.username,
        "display_name": sender_data["display_name"],
        "avatar_url": sender_data["avatar_url"],
        "accent_color": sender_data["accent_color"],
        "is_admin": sender_data["is_admin"],
        "message": msg.content or "",
        "content": msg.content or "",
        "text": msg.content or "",
        "attachment_url": _safe_media_url(msg.attachment),
        "attachment_name": os.path.basename(msg.attachment.name) if msg.attachment else "",
        "attachment_type": msg.attachment_type or "",
        "timestamp": ts,
        "time": ts,
        "reactions": list(reactions_summary.values()),
    }


def _serialize_dm(dm, current_user=None):
    sender = getattr(dm, "sender", None) or getattr(dm, "user", None)
    recipient = getattr(dm, "receiver", None) or getattr(dm, "recipient", None)
    p_sender = _get_or_create_profile(sender)
    d_sender = _serialize_profile(sender, p_sender)
    ts = _format_local_hhmm(dm)

    return {
        "id": dm.id,
        "client_id": getattr(dm, "client_id", "") or "",
        "sender": sender.username if sender else "",
        "author": sender.username if sender else "",
        "receiver": recipient.username if recipient else "",
        "recipient": recipient.username if recipient else "",
        "display_name": d_sender["display_name"],
        "avatar_url": d_sender["avatar_url"],
        "message": dm.content or "",
        "content": dm.content or "",
        "text": dm.content or "",
        "attachment_url": _safe_media_url(getattr(dm, "attachment", None)),
        "attachment_name": os.path.basename(dm.attachment.name) if getattr(dm, "attachment", None) else "",
        "attachment_type": getattr(dm, "attachment_type", "") or "",
        "timestamp": ts,
        "time": ts,
    }


def _render_first_existing(request, template_names, context):
    try:
        tpl = select_template(template_names)
        return render(request, tpl.template.name, context)
    except TemplateDoesNotExist:
        return HttpResponse(
            f"<h1>Kiwi Chat</h1><p>Template nao encontrado ({ ', '.join(template_names) }).</p>",
            status=200,
        )


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect("room_home")

    error_message = None
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            _get_or_create_profile(user)
            _ensure_default_workspace(user)
            return redirect("room_home")
        error_message = "Usuário ou senha inválidos."

    return _render_first_existing(
        request,
        ["chat/login.html", "kiwi_chat/login.html", "registration/login.html", "login.html"],
        {"error_message": error_message},
    )


@ensure_csrf_cookie
def register_view(request):
    if request.user.is_authenticated:
        return redirect("room_home")

    error_message = None
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        password_confirm = request.POST.get("password_confirm") or ""

        if not username or len(username) < 2 or len(username) > 32:
            error_message = "O nome de usuário deve ter entre 2 e 32 caracteres."
        elif not re.match(r"^[a-zA-Z0-9_.-]+$", username):
            error_message = "Use apenas letras, números, ponto, traço ou sublinhado."
        elif len(password) < 4:
            error_message = "A senha deve ter pelo menos 4 caracteres."
        elif password_confirm and password != password_confirm:
            error_message = "As senhas não coincidem."
        elif User.objects.filter(username__iexact=username).exists():
            error_message = "Esse nome de usuário já está em uso."
        else:
            try:
                user = User.objects.create_user(username=username, password=password)
                _get_or_create_profile(user)
                _ensure_default_workspace(user)
                login(request, user)
                return redirect("room_home")
            except IntegrityError:
                error_message = "Não foi possível criar a conta agora."

    return _render_first_existing(
        request,
        ["chat/register.html", "kiwi_chat/register.html", "registration/register.html", "register.html"],
        {"error_message": error_message},
    )


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
@ensure_csrf_cookie
def room_view(request, room_name=None, server_id=None, channel_id=None):
    user = request.user
    profile = _get_or_create_profile(user)
    servers = _ensure_default_workspace(user)
    is_admin = _is_kiwi_admin(user)

    selected_server = None
    if server_id:
        selected_server = next((s for s in servers if s.id == int(server_id)), None)
    if not selected_server:
        selected_server = servers[0] if servers else None

    selected_channel = None
    if selected_server:
        channels_sorted = list(selected_server.channels.order_by("position", "id"))
        if channel_id:
            selected_channel = next((c for c in channels_sorted if c.id == int(channel_id)), None)
        if not selected_channel and room_name:
            selected_channel = next((c for c in channels_sorted if c.name.lower() == str(room_name).lower()), None)
        if not selected_channel and channels_sorted:
            selected_channel = channels_sorted[0]

    servers_payload = []
    for srv in servers:
        ch_list = [
            {"id": ch.id, "name": ch.name, "server_id": srv.id, "channel_type": ch.channel_type}
            for ch in srv.channels.order_by("position", "id")
        ]
        servers_payload.append({
            "id": srv.id,
            "name": srv.name,
            "icon_url": _safe_media_url(getattr(srv, "icon", None)),
            "channels": ch_list,
        })

    friends_payload = _get_friends_list(user)
    incoming_requests = FriendRequest.objects.filter(
        status="pending",
        receiver=user,
    ).select_related("sender", "receiver")

    pending_requests_payload = []
    for req in incoming_requests:
        origin = req.sender
        if origin and origin.id != user.id:
            p_from = _get_or_create_profile(origin)
            pending_requests_payload.append({
                "id": req.id,
                "from_username": origin.username,
                "display_name": _format_user_label(origin),
                "avatar_url": _safe_media_url(p_from.avatar),
            })

    profile_data = _serialize_profile(user, profile)

    context = {
        "username": user.username,
        "display_name": profile_data["display_name"],
        "is_admin": is_admin,
        "active_server_id": selected_server.id if selected_server else 1,
        "active_channel_id": selected_channel.id if selected_channel else 1,
        "active_channel_name": selected_channel.name if selected_channel else (room_name or "geral"),
        "servers_json": json.dumps(servers_payload),
        "profile_json": json.dumps(profile_data),
        "friends_json": json.dumps(friends_payload),
        "pending_requests_json": json.dumps(pending_requests_payload),
    }
    return _render_first_existing(request, ["chat/room.html", "kiwi_chat/room.html", "room.html"], context)


@login_required
@ensure_csrf_cookie
def profile_page_view(request):
    user = request.user
    profile = _get_or_create_profile(user)
    profile_data = _serialize_profile(user, profile)
    return _render_first_existing(
        request,
        ["chat/profile.html", "kiwi_chat/profile.html", "profile.html"],
        {
            "username": user.username,
            "profile": profile,
            "profile_data": profile_data,
            "profile_json": json.dumps(profile_data),
            "is_admin": _is_kiwi_admin(user),
        },
    )


@login_required
@ensure_csrf_cookie
def loja_view(request):
    user = request.user
    profile = _get_or_create_profile(user)
    profile_data = _serialize_profile(user, profile)
    is_exclusive_tester = (user.username or "").strip().lower() == EXCLUSIVE_TESTER_USERNAME.lower()
    return _render_first_existing(
        request,
        ["chat/loja.html", "kiwi_chat/loja.html", "loja.html"],
        {
            "username": user.username,
            "profile": profile,
            "profile_data": profile_data,
            "profile_json": json.dumps(profile_data),
            "is_admin": _is_kiwi_admin(user),
            "is_exclusive_tester": is_exclusive_tester,
        },
    )


@login_required
@require_GET
def api_bootstrap(request):
    user = request.user
    profile = _get_or_create_profile(user)
    servers = _ensure_default_workspace(user)
    is_admin = _is_kiwi_admin(user)

    servers_payload = [
        {
            "id": srv.id,
            "name": srv.name,
            "icon_url": _safe_media_url(getattr(srv, "icon", None)),
            "channels": [
                {"id": ch.id, "name": ch.name, "server_id": srv.id, "channel_type": ch.channel_type}
                for ch in srv.channels.order_by("position", "id")
            ],
        }
        for srv in servers
    ]

    friends = _get_friends_list(user)
    incoming = FriendRequest.objects.filter(status="pending", receiver=user).select_related("sender", "receiver")
    requests_data = [
        {
            "id": r.id,
            "from_username": r.sender.username,
            "display_name": _format_user_label(r.sender),
        }
        for r in incoming
        if r.sender and r.sender.id != user.id
    ]

    return JsonResponse({
        "ok": True,
        "username": user.username,
        "is_admin": is_admin,
        "profile": _serialize_profile(user, profile),
        "servers": servers_payload,
        "friends": friends,
        "pending_requests": requests_data,
    })


@login_required
@require_GET
def api_channel_messages(request, channel_id):
    channel = get_object_or_404(Channel, id=channel_id)
    channel.server.members.add(request.user)
    msgs = (
        Message.objects.filter(channel=channel)
        .select_related("user", "channel")
        .prefetch_related("reactions", "reactions__user")
        .order_by("-timestamp", "-id")[:60]
    )
    ordered = list(reversed(msgs))
    return JsonResponse({
        "ok": True,
        "channel_id": channel.id,
        "channel_name": channel.name,
        "messages": [_serialize_message(m, current_user=request.user) for m in ordered],
    })


@login_required
@require_GET
def api_dm_history(request, username):
    target_user = get_object_or_404(User, username__iexact=username)
    if target_user.id == request.user.id:
        return JsonResponse({"ok": False, "error": "Você não pode abrir DM consigo mesmo."}, status=400)

    dms = (
        DirectMessage.objects.filter(
            Q(sender=request.user, receiver=target_user) | Q(sender=target_user, receiver=request.user)
        )
        .select_related("sender", "receiver")
        .order_by("timestamp", "id")[:80]
    )
    return JsonResponse({
        "ok": True,
        "peer": target_user.username,
        "messages": [_serialize_dm(m, current_user=request.user) for m in dms],
    })


@login_required
@require_POST
def api_send_dm(request, username):
    target_user = get_object_or_404(User, username__iexact=username)
    if target_user.id == request.user.id:
        return JsonResponse({"ok": False, "error": "Destinatário inválido."}, status=400)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    content = (payload.get("message") or payload.get("text") or payload.get("content") or "").strip()
    attachment_data = payload.get("attachment_url") or payload.get("attachment") or ""
    client_id = (payload.get("client_id") or "")[:100]

    if not content and not attachment_data and not request.FILES.get("file"):
        return JsonResponse({"ok": False, "error": "Mensagem vazia."}, status=400)

    dm = DirectMessage.objects.create(
        sender=request.user,
        receiver=target_user,
        content=content[:2000],
        client_id=client_id,
    )

    if request.FILES.get("file"):
        dm.attachment = request.FILES["file"]
        dm.attachment_type = (request.FILES["file"].content_type or "file")[:20]
        dm.save()
    elif isinstance(attachment_data, str) and attachment_data.startswith("data:"):
        saved_url = _save_base64_media(attachment_data, prefix="dm")
        if saved_url:
            rel_name = saved_url.replace(settings.MEDIA_URL, "", 1).lstrip("/")
            dm.attachment.name = rel_name
            dm.attachment_type = "image"
            dm.save()

    return JsonResponse({"ok": True, "message": _serialize_dm(dm, current_user=request.user)})


@login_required
@require_POST
def api_upload_attachment(request):
    channel_id = request.POST.get("channel_id")
    content = (request.POST.get("content") or request.POST.get("message") or "").strip()
    uploaded = request.FILES.get("file") or request.FILES.get("attachment")

    if not uploaded and not content:
        return JsonResponse({"ok": False, "error": "Nenhum arquivo ou mensagem enviado."}, status=400)

    channel = Channel.objects.filter(id=channel_id).first() if channel_id else Channel.objects.order_by("id").first()
    if not channel:
        servers = _ensure_default_workspace(request.user)
        channel = servers[0].channels.order_by("id").first()

    msg = Message.objects.create(
        channel=channel,
        user=request.user,
        content=content[:2000],
    )
    if uploaded:
        msg.attachment = uploaded
        msg.attachment_type = (uploaded.content_type or "file")[:20]
        msg.save()

    serialized = _serialize_message(msg, current_user=request.user)
    return JsonResponse({
        "ok": True,
        "id": msg.id,
        "channel_id": channel.id,
        "message": serialized,
        "attachment_url": serialized["attachment_url"],
        "attachment_name": serialized["attachment_name"],
        "attachment_type": serialized["attachment_type"],
    })


@login_required
@require_POST
def api_toggle_reaction(request, message_id):
    msg = get_object_or_404(Message, id=message_id)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    emoji = (payload.get("emoji") or "👍").strip()[:32]
    if not emoji:
        return JsonResponse({"ok": False, "error": "Emoji inválido."}, status=400)

    existing = MessageReaction.objects.filter(message=msg, user=request.user, emoji=emoji).first()
    if existing:
        existing.delete()
    else:
        MessageReaction.objects.create(message=msg, user=request.user, emoji=emoji)

    return JsonResponse({"ok": True, "message": _serialize_message(msg, current_user=request.user)})


@login_required
@require_POST
def api_delete_message(request, message_id):
    msg = get_object_or_404(Message, id=message_id)
    author = _message_author(msg)
    author_id = author.id if author else None
    if author_id != request.user.id and not _is_kiwi_admin(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para excluir esta mensagem."}, status=403)
    msg.delete()
    return JsonResponse({"ok": True, "deleted_id": message_id})


@login_required
@require_POST
def api_create_server(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    name = (payload.get("name") or "").strip()
    if not name or len(name) > 120:
        return JsonResponse({"ok": False, "error": "Nome de servidor inválido."}, status=400)

    server = Server.objects.create(name=name, owner=request.user)
    default_ch, _ = Channel.objects.get_or_create(server=server, name="geral")

    return JsonResponse({
        "ok": True,
        "server": {
            "id": server.id,
            "name": server.name,
            "icon_url": "",
            "channels": [{"id": default_ch.id, "name": default_ch.name, "server_id": server.id}],
        },
    })


@login_required
@require_POST
def api_create_channel(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    server_id = payload.get("server_id")
    name = (payload.get("name") or "").strip().lower().replace(" ", "-")
    channel_type = "audio" if (payload.get("channel_type") == "audio") else "text"
    if not name or len(name) > 100:
        return JsonResponse({"ok": False, "error": "Nome de canal inválido."}, status=400)

    server = Server.objects.filter(id=server_id).first() if server_id else Server.objects.filter(members=request.user).first()
    if not server:
        servers = _ensure_default_workspace(request.user)
        server = servers[0]

    channel, _ = Channel.objects.get_or_create(
        server=server,
        name=name,
        defaults={"channel_type": channel_type},
    )
    return JsonResponse({
        "ok": True,
        "channel": {
            "id": channel.id,
            "name": channel.name,
            "server_id": server.id,
            "channel_type": channel.channel_type,
        },
    })


@login_required
@require_GET
def api_user_profile(request, username):
    target_user = get_object_or_404(User, username__iexact=username)
    profile = _get_or_create_profile(target_user)
    data = _serialize_profile(target_user, profile)
    _attach_follow_stats(data, target_user, viewer_user=request.user)
    data["is_friend"] = _are_friends(request.user, target_user)
    data["is_self"] = request.user.id == target_user.id
    return JsonResponse({"ok": True, "profile": data})


@login_required
@require_POST
def api_update_profile(request):
    if request.content_type and "multipart/form-data" in request.content_type:
        data = request.POST
    else:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            data = request.POST

    target_user = request.user
    raw_target_id = data.get("account_id") or data.get("user_id")
    if raw_target_id not in (None, ""):
        try:
            target_id = int(raw_target_id)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "ID de conta inválido."}, status=400)

        if target_id != request.user.id and not request.user.is_superuser:
            return JsonResponse(
                {"ok": False, "error": "Você só pode alterar o perfil da sua própria conta."},
                status=403,
            )

        if target_id != request.user.id:
            target_user = User.objects.filter(pk=target_id).first()
            if not target_user:
                return JsonResponse({"ok": False, "error": "Conta de destino não encontrada."}, status=404)

    profile = _get_or_create_profile(target_user)
    is_admin = _is_kiwi_admin(target_user) or request.user.is_superuser

    if "bio" in data:
        profile.bio = (data.get("bio") or "").strip()[:500]
    if "pronouns" in data:
        profile.pronouns = (data.get("pronouns") or "").strip()[:100]
    if "location" in data:
        profile.location = (data.get("location") or "").strip()[:120]
    if "bubble" in data or "custom_status" in data:
        val = data.get("bubble") if "bubble" in data else data.get("custom_status")
        profile.bubble = (val or "").strip()[:255]
    if "accent_color" in data:
        profile.accent_color = _sanitize_accent_color(data.get("accent_color"), is_admin=is_admin)
    if "frame_effect" in data:
        profile.frame_effect = (data.get("frame_effect") or "none").strip()[:50]
    if "name_style" in data:
        profile.name_style = (data.get("name_style") or "").strip()[:50]
    if "profile_glow" in data:
        profile.profile_glow = str(data.get("profile_glow")).lower() in ("true", "1", "on")
    if "avatar_pulse" in data:
        profile.avatar_pulse = str(data.get("avatar_pulse")).lower() in ("true", "1", "on")
    if "spotify_song" in data or "music_title" in data:
        val = data.get("spotify_song") if "spotify_song" in data else data.get("music_title")
        profile.spotify_song = (val or "").strip()[:150]
    if "spotify_artist" in data or "music_artist" in data:
        val = data.get("spotify_artist") if "spotify_artist" in data else data.get("music_artist")
        profile.spotify_artist = (val or "").strip()[:150]
    if "username" in data and hasattr(profile, "display_name"):
        new_disp = (data.get("username") or "").strip()[:50]
        if new_disp:
            profile.display_name = new_disp
    if "music_url" in data:
        profile.music_url = (data.get("music_url") or "").strip()[:200]
    if "music_audio_url" in data and hasattr(profile, "music_audio_url"):
        profile.music_audio_url = (data.get("music_audio_url") or "").strip()[:300]
    if "music_cover_url" in data and hasattr(profile, "music_cover_url"):
        profile.music_cover_url = (data.get("music_cover_url") or "").strip()[:300]

    if request.FILES.get("avatar"):
        profile.avatar = request.FILES["avatar"]
    elif isinstance(data.get("avatar_url"), str) and data.get("avatar_url").startswith("data:"):
        saved = _save_base64_media(data.get("avatar_url"), prefix="avatar")
        if saved:
            profile.avatar.name = saved.replace(settings.MEDIA_URL, "", 1).lstrip("/")

    if request.FILES.get("banner"):
        profile.banner = request.FILES["banner"]
    elif isinstance(data.get("banner_url"), str) and data.get("banner_url").startswith("data:"):
        saved = _save_base64_media(data.get("banner_url"), prefix="banner")
        if saved:
            profile.banner.name = saved.replace(settings.MEDIA_URL, "", 1).lstrip("/")

    f_audio = (
        request.FILES.get("music_audio_file")
        or request.FILES.get("music_file")
        or request.FILES.get("music_audio")
    )
    if f_audio:
        profile.music_audio_file = f_audio
    elif isinstance(data.get("music_url"), str) and data.get("music_url").startswith("data:"):
        saved = _save_base64_media(data.get("music_url"), prefix="music")
        if saved:
            profile.music_audio_file.name = saved.replace(settings.MEDIA_URL, "", 1).lstrip("/")

    f_cover = (
        request.FILES.get("music_cover_file")
        or request.FILES.get("music_cover")
    )
    if f_cover:
        profile.music_cover_file = f_cover
    elif isinstance(data.get("music_cover_url"), str) and data.get("music_cover_url").startswith("data:"):
        saved = _save_base64_media(data.get("music_cover_url"), prefix="cover")
        if saved:
            profile.music_cover_file.name = saved.replace(settings.MEDIA_URL, "", 1).lstrip("/")

    profile.save()
    serialized_profile = _serialize_profile(target_user, profile)

    if "_broadcast_feed_event" in globals() and callable(_broadcast_feed_event):
        _broadcast_feed_event(
            "profile_updated",
            {
                "username": target_user.username,
                "profile": serialized_profile,
            },
        )

    return JsonResponse({
        "ok": True,
        "status": "success",
        "username": target_user.username,
        "profile": serialized_profile,
    })


def _broadcast_feed_event(event_type, payload):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            "kiwi_feed",
            {
                "type": event_type,
                "payload": payload,
            },
        )
    except Exception:
        pass


@login_required
@require_POST
def api_friend_request(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    target_username = (payload.get("username") or payload.get("to_username") or "").strip()
    if not target_username:
        return JsonResponse({"ok": False, "error": "Informe um nome de usuário."}, status=400)

    target_user = User.objects.filter(username__iexact=target_username).first()
    if not target_user:
        return JsonResponse({"ok": False, "error": "Usuário não encontrado."}, status=404)
    if target_user.id == request.user.id:
        return JsonResponse({"ok": False, "error": "Você não pode adicionar a si mesmo."}, status=400)
    if _are_friends(request.user, target_user):
        return JsonResponse({"ok": True, "already_friends": True, "message": "Vocês já são amigos!"})

    req, created = FriendRequest.objects.get_or_create(
        sender=request.user,
        receiver=target_user,
        defaults={"status": "pending"},
    )
    if not created and req.status != "pending":
        req.status = "pending"
        req.save(update_fields=["status", "updated_at"])

    return JsonResponse({
        "ok": True,
        "request_id": req.id,
        "to_username": target_user.username,
        "message": f"Solicitação enviada para {target_user.username}.",
    })


@login_required
@require_POST
def api_respond_friend_request(request, request_id=None):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    if request_id:
        freq = get_object_or_404(FriendRequest, id=request_id)
    else:
        sender_name = (payload.get("username") or payload.get("from") or "").strip()
        freq = get_object_or_404(
            FriendRequest,
            sender__username__iexact=sender_name,
            receiver=request.user,
            status="pending",
        )

    origin = freq.sender
    default_action = "reject" if "reject" in request.path else "accept"
    action = (payload.get("action") or default_action).strip().lower()
    if action in ("accept", "aceitar"):
        freq.status = "accepted"
        freq.save(update_fields=["status", "updated_at"])
        if origin:
            u1, u2 = sorted([request.user, origin], key=lambda u: u.id)
            Friendship.objects.get_or_create(user1=u1, user2=u2)
            return JsonResponse({
                "ok": True,
                "accepted": True,
                "friend": _serialize_profile(origin, _get_or_create_profile(origin)),
            })
        return JsonResponse({"ok": True, "accepted": True})
    if action in ("reject", "decline", "recusar"):
        freq.status = "rejected"
        freq.save(update_fields=["status", "updated_at"])
        return JsonResponse({"ok": True, "accepted": False})

    return JsonResponse({"ok": False, "error": "Ação inválida."}, status=400)


@login_required
@require_POST
def api_remove_friend(request, username):
    target_user = get_object_or_404(User, username__iexact=username)
    FriendRequest.objects.filter(
        Q(sender=request.user, receiver=target_user) | Q(sender=target_user, receiver=request.user)
    ).delete()
    Friendship.objects.filter(
        Q(user1=request.user, user2=target_user) | Q(user1=target_user, user2=request.user)
    ).delete()
    return JsonResponse({"ok": True, "removed": target_user.username})


# === SEGUIDORES / SEGUINDO ===

@login_required
@require_POST
def toggle_follow(request, username=None):
    if not username:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = request.POST
        username = (payload.get("username") or payload.get("target_username") or "").strip()

    if not username:
        return JsonResponse({"ok": False, "error": "Informe o nome de usuário."}, status=400)

    target_user = get_object_or_404(User, username__iexact=username)
    if target_user.id == request.user.id:
        return JsonResponse({"ok": False, "error": "Você não pode seguir a si mesmo."}, status=400)

    relation = Follow.objects.filter(follower=request.user, following=target_user).first()
    if relation:
        relation.delete()
        is_following = False
    else:
        Follow.objects.create(follower=request.user, following=target_user)
        is_following = True

    target_profile = _get_or_create_profile(target_user)
    target_payload = _serialize_profile(target_user, target_profile)
    _attach_follow_stats(target_payload, target_user, viewer_user=request.user)

    actor_profile = _get_or_create_profile(request.user)
    actor_payload = _serialize_profile(request.user, actor_profile)
    _attach_follow_stats(actor_payload, request.user, viewer_user=request.user)

    return JsonResponse({
        "ok": True,
        "following": is_following,
        "is_following": is_following,
        "target_username": target_user.username,
        "followers_count": target_payload["followers_count"],
        "following_count": target_payload["following_count"],
        "actor_followers_count": actor_payload["followers_count"],
        "actor_following_count": actor_payload["following_count"],
        "profile": target_payload,
    })


@login_required
@require_GET
def list_user_follows(request, username):
    target_user = get_object_or_404(User, username__iexact=username)
    followers_qs = Follow.objects.filter(following=target_user).select_related("follower")
    following_qs = Follow.objects.filter(follower=target_user).select_related("following")

    followers = []
    for rel in followers_qs:
        u = rel.follower
        p = _get_or_create_profile(u)
        followers.append({
            "username": u.username,
            "display_name": _format_user_label(u),
            "avatar_url": _safe_media_url(p.avatar),
            "bio": p.bio or "",
        })

    following = []
    for rel in following_qs:
        u = rel.following
        p = _get_or_create_profile(u)
        following.append({
            "username": u.username,
            "display_name": _format_user_label(u),
            "avatar_url": _safe_media_url(p.avatar),
            "bio": p.bio or "",
        })

    return JsonResponse({
        "ok": True,
        "username": target_user.username,
        "followers_count": len(followers),
        "following_count": len(following),
        "followers": followers,
        "following": following,
    })


# === POSTAGENS DO PERFIL (ProfilePost & ProfilePostReply) ===

@login_required
@require_GET
def list_profile_posts(request):
    username = (request.GET.get("username") or "").strip()
    qs = ProfilePost.objects.select_related("author").prefetch_related("liked_by", "replies", "replies__author")
    if username:
        qs = qs.filter(author__username__iexact=username)
    posts = []
    for p in qs[:60]:
        item = p.to_dict(current_user=request.user)
        author_prof = _get_or_create_profile(p.author)
        item["author_avatar"] = _safe_media_url(author_prof.avatar)
        posts.append(item)
    return JsonResponse({"ok": True, "posts": posts})


@login_required
@require_POST
def create_profile_post(request):
    if request.content_type and "multipart/form-data" in request.content_type:
        data = request.POST
    else:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            data = request.POST

    content = (data.get("content") or data.get("text") or "").strip()
    image_url = (data.get("image_url") or "").strip()
    uploaded_image = request.FILES.get("image") or request.FILES.get("file")

    if not content and not uploaded_image and not image_url:
        return JsonResponse({"ok": False, "error": "A postagem não pode estar vazia."}, status=400)

    post = ProfilePost.objects.create(
        author=request.user,
        content=content[:2000],
        image_url=image_url if not str(image_url).startswith("data:") else "",
    )

    if uploaded_image:
        post.image = uploaded_image
        post.save()
    elif isinstance(image_url, str) and image_url.startswith("data:"):
        saved = _save_base64_media(image_url, prefix="post")
        if saved:
            post.image.name = saved.replace(settings.MEDIA_URL, "", 1).lstrip("/")
            post.save()

    return JsonResponse({"ok": True, "post": post.to_dict(current_user=request.user)})


@login_required
@require_POST
def toggle_like_profile_post(request, post_id):
    post = get_object_or_404(ProfilePost, pk=post_id)
    if post.liked_by.filter(pk=request.user.pk).exists():
        post.liked_by.remove(request.user)
        liked = False
    else:
        post.liked_by.add(request.user)
        liked = True
    return JsonResponse({
        "ok": True,
        "liked": liked,
        "likes": post.liked_by.count(),
        "post": post.to_dict(current_user=request.user),
    })


@login_required
@require_POST
def add_profile_post_reply(request, post_id):
    post = get_object_or_404(ProfilePost, pk=post_id)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    content = (payload.get("content") or payload.get("text") or "").strip()
    if not content:
        return JsonResponse({"ok": False, "error": "Resposta vazia."}, status=400)

    reply = ProfilePostReply.objects.create(
        post=post,
        author=request.user,
        content=content[:1000],
    )
    return JsonResponse({
        "ok": True,
        "reply": {
            "id": reply.id,
            "author": reply.author.username,
            "text": reply.content,
            "created_at": reply.created_at.isoformat(),
        },
        "post": post.to_dict(current_user=request.user),
    })


@login_required
@require_POST
def delete_profile_post(request, post_id):
    post = get_object_or_404(ProfilePost, pk=post_id)
    if post.author_id != request.user.id and not _is_kiwi_admin(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para excluir esta postagem."}, status=403)
    post.delete()
    return JsonResponse({"ok": True, "deleted_id": post_id})


# === ALIASES DE COMPATIBILIDADE COM chat/urls.py ===
update_profile = api_update_profile
get_user_profile = api_user_profile
create_server = api_create_server
create_channel = api_create_channel
send_friend_request = api_friend_request
accept_friend_request = api_respond_friend_request
reject_friend_request = api_respond_friend_request
remove_friend = api_remove_friend
delete_message = api_delete_message
get_dm_history = api_dm_history
upload_attachment = api_upload_attachment


@login_required
def update_server(request, server_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    server = get_object_or_404(Server, pk=server_id)
    if server.owner_id and server.owner_id != request.user.id and not _is_kiwi_admin(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão."}, status=403)
    if request.content_type and "multipart/form-data" in request.content_type:
        data = request.POST
    else:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            data = request.POST
    name = (data.get("name") or "").strip()
    if name:
        server.name = name
    if request.FILES.get("icon"):
        server.icon = request.FILES["icon"]
    server.save()
    return JsonResponse({"ok": True, "id": server.id, "name": server.name, "icon_url": _safe_media_url(server.icon)})


@login_required
def delete_server(request, server_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    server = get_object_or_404(Server, pk=server_id)
    if server.owner_id and server.owner_id != request.user.id and not _is_kiwi_admin(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão."}, status=403)
    server.delete()
    return JsonResponse({"ok": True})


@login_required
def delete_channel(request, server_id, channel_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    channel = get_object_or_404(Channel, pk=channel_id, server_id=server_id)
    channel.delete()
    return JsonResponse({"ok": True})


@login_required
def get_server_members_grouped(request, server_id):
    server = get_object_or_404(Server, pk=server_id)
    members = []
    for u in server.members.all():
        p = _get_or_create_profile(u)
        ser = _serialize_profile(u, p)
        members.append({
            "id": u.id,
            "username": u.username,
            "display_name": ser["display_name"],
            "avatar_url": ser["avatar_url"],
            "is_admin": ser["is_admin"],
        })
    return JsonResponse({"ok": True, "members": members})


@login_required
def add_server_member(request, server_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    server = get_object_or_404(Server, pk=server_id)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST
    username = (payload.get("username") or "").strip()
    target = get_object_or_404(User, username__iexact=username)
    server.members.add(target)
    return JsonResponse({"ok": True, "id": target.id, "username": target.username})


@login_required
def kick_server_member(request, server_id, user_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    server = get_object_or_404(Server, pk=server_id)
    target = get_object_or_404(User, pk=user_id)
    server.members.remove(target)
    return JsonResponse({"ok": True})


@login_required
def assign_member_role(request, server_id, user_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    server = get_object_or_404(Server, pk=server_id)
    target = get_object_or_404(User, pk=user_id)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST
    role_id = payload.get("role_id")
    membership, _ = ServerMember.objects.get_or_create(server=server, user=target)
    membership.role = ServerRole.objects.filter(pk=role_id, server=server).first() if role_id else None
    membership.save()
    return JsonResponse({"ok": True, "role": membership.role.to_dict() if membership.role else None})


@login_required
def create_server_role(request, server_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    server = get_object_or_404(Server, pk=server_id)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST
    name = (payload.get("name") or "").strip()[:50]
    if not name:
        return JsonResponse({"ok": False, "error": "Nome de cargo inválido."}, status=400)
    role, _ = ServerRole.objects.get_or_create(
        server=server,
        name=name,
        defaults={
            "color": (payload.get("color") or "#8a63d2")[:20],
            "can_manage_channels": bool(payload.get("can_manage_channels", False)),
            "can_kick_members": bool(payload.get("can_kick_members", False)),
            "can_manage_roles": bool(payload.get("can_manage_roles", False)),
        },
    )
    return JsonResponse({"ok": True, "role": role.to_dict()})


@login_required
def delete_server_role(request, server_id, role_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    role = get_object_or_404(ServerRole, pk=role_id, server_id=server_id)
    role.delete()
    return JsonResponse({"ok": True})


@login_required
def search_users(request):
    q = (request.GET.get("q") or "").strip()
    qs = User.objects.filter(username__icontains=q).exclude(pk=request.user.pk)[:20] if q else User.objects.none()
    return JsonResponse({"ok": True, "users": [{"id": u.id, "username": u.username} for u in qs]})


@login_required
def get_friend_requests(request):
    incoming = FriendRequest.objects.filter(status="pending", receiver=request.user).select_related("sender", "receiver")
    items = [
        {"id": r.id, "username": r.sender.username, "from_username": r.sender.username}
        for r in incoming
        if r.sender
    ]
    return JsonResponse({"ok": True, "incoming": items, "requests": items})


@login_required
def get_friends_list(request):
    return JsonResponse({"ok": True, "friends": _get_friends_list(request.user)})


@login_required
def send_message(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    content = (payload.get("content") or payload.get("message") or payload.get("text") or "").strip()
    channel_id = payload.get("channel_id")
    channel_name = (payload.get("channel_name") or "").strip()
    server_id = payload.get("server_id")
    recipient_username = (payload.get("recipient") or payload.get("receiver") or payload.get("to_username") or "").strip()
    client_id = (payload.get("client_id") or "")[:100]
    attachment_url = (payload.get("attachment_url") or "").strip()

    uploaded = request.FILES.get("file") or request.FILES.get("attachment")
    if not content and not uploaded and not attachment_url:
        return JsonResponse({"ok": False, "error": "Mensagem vazia."}, status=400)

    if recipient_username:
        recipient = get_object_or_404(User, username__iexact=recipient_username)
        dm = DirectMessage.objects.create(
            sender=request.user,
            receiver=recipient,
            content=content[:2000],
            client_id=client_id,
        )
        if uploaded:
            dm.attachment = uploaded
            dm.attachment_type = (uploaded.content_type or "file")[:20]
            dm.save()
        elif attachment_url.startswith("data:"):
            saved_url = _save_base64_media(attachment_url, prefix="dm")
            if saved_url:
                dm.attachment.name = saved_url.replace(settings.MEDIA_URL, "", 1).lstrip("/")
                dm.attachment_type = "image"
                dm.save()
        return JsonResponse({"ok": True, "message": _serialize_dm(dm, current_user=request.user)})

    channel = None
    if channel_id not in (None, "", "null"):
        channel = get_object_or_404(Channel, pk=channel_id)
    elif channel_name:
        qs = Channel.objects.filter(name__iexact=channel_name)
        if server_id and str(server_id).isdigit():
            qs = qs.filter(server_id=int(server_id))
        channel = qs.first()
        if not channel:
            servers = _ensure_default_workspace(request.user)
            channel = servers[0].channels.filter(name__iexact=channel_name).first() or servers[0].channels.order_by("id").first()
    else:
        servers = _ensure_default_workspace(request.user)
        channel = servers[0].channels.order_by("id").first()

    msg = Message.objects.create(
        channel=channel,
        user=request.user,
        content=content[:2000],
        client_id=client_id,
    )
    if uploaded:
        msg.attachment = uploaded
        msg.attachment_type = (uploaded.content_type or "file")[:20]
        msg.save()
    elif attachment_url.startswith("data:"):
        saved_url = _save_base64_media(attachment_url, prefix="msg")
        if saved_url:
            msg.attachment.name = saved_url.replace(settings.MEDIA_URL, "", 1).lstrip("/")
            msg.attachment_type = "image"
            msg.save()

    return JsonResponse({"ok": True, "message": _serialize_message(msg, current_user=request.user)})


@login_required
def delete_dm_message(request, message_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    dm = get_object_or_404(DirectMessage, pk=message_id, sender=request.user)
    dm.delete()
    return JsonResponse({"ok": True})


@login_required
def list_saved_musics(request):
    items = [
        {
            "id": m.id,
            "title": m.title,
            "artist": m.artist,
            "music_url": m.music_url,
            "audio_url": m.get_audio_src(),
            "cover_url": m.get_cover_src(),
        }
        for m in SavedMusic.objects.filter(user=request.user)
    ]
    return JsonResponse({"ok": True, "musics": items})


@login_required
def create_saved_music(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST
    m = SavedMusic.objects.create(
        user=request.user,
        title=(payload.get("title") or "")[:150],
        artist=(payload.get("artist") or "")[:150],
        music_url=(payload.get("music_url") or "")[:200],
        audio_url=(payload.get("audio_url") or ""),
        cover_url=(payload.get("cover_url") or ""),
    )
    if request.FILES.get("audio_file"):
        m.audio_file = request.FILES["audio_file"]
    if request.FILES.get("cover_file"):
        m.cover_file = request.FILES["cover_file"]
    m.save()
    return JsonResponse({"ok": True, "id": m.id})


@login_required
def delete_saved_music(request, music_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    m = get_object_or_404(SavedMusic, pk=music_id, user=request.user)
    m.delete()
    return JsonResponse({"ok": True})


@login_required
def friend_music_feed(request):
    friends = _get_friends_list(request.user)
    usernames = [f["username"] for f in friends]
    qs = UserProfile.objects.filter(user__username__in=usernames).select_related("user")
    feed = [
        {
            "username": p.user.username,
            "song": p.spotify_song,
            "artist": p.spotify_artist,
            "audio_url": p.get_music_audio_src(),
            "cover_url": p.get_music_cover_src(),
        }
        for p in qs
    ]
    return JsonResponse({"ok": True, "feed": feed})


@login_required
@require_POST
def api_shop_equip(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    item_id = (payload.get("item_id") or "").strip()
    item = SHOP_CATALOG.get(item_id)
    if not item:
        return JsonResponse({"ok": False, "error": "Item não encontrado."}, status=404)

    if not _can_use_exclusive_shop_item(request.user, item):
        return JsonResponse(
            {"ok": False, "error": "Catálogo em preparação para esta conta."},
            status=403,
        )

    profile = _get_or_create_profile(request.user)
    slot = item["slot"]
    value = item["value"]

    if slot == "frame_effect":
        profile.frame_effect = value
        profile.avatar_pulse = True
    elif slot == "card_effect":
        profile.profile_glow = True
        if hasattr(profile, "card_effect"):
            profile.card_effect = value
    elif slot == "name_style":
        profile.name_style = value

    profile.save()
    return JsonResponse({
        "ok": True,
        "equipped": {
            "item_id": item["id"],
            "slot": slot,
            "value": value,
            "name": item["name"],
        },
        "profile": _serialize_profile(request.user, profile),
    })


@login_required
@require_POST
def api_shop_unequip(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    slot = (payload.get("slot") or "").strip()
    profile = _get_or_create_profile(request.user)

    if slot == "frame_effect":
        profile.frame_effect = "none"
        profile.avatar_pulse = False
    elif slot == "card_effect":
        profile.profile_glow = False
        if hasattr(profile, "card_effect"):
            profile.card_effect = "none"
    elif slot == "name_style":
        profile.name_style = "none"
    else:
        profile.frame_effect = "none"
        profile.profile_glow = False
        profile.name_style = "none"
        profile.avatar_pulse = False
        if hasattr(profile, "card_effect"):
            profile.card_effect = "none"

    profile.save()
    return JsonResponse({
        "ok": True,
        "slot": slot or "all",
        "profile": _serialize_profile(request.user, profile),
    })