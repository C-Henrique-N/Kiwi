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
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from .models import (
    Channel,
    DirectMessage,
    FriendRequest,
    Friendship,
    Message,
    MessageReaction,
    Profile,
    Server,
)

ADMIN_USERNAME = "AkiiraPW"

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


def _is_kiwi_admin(user):
    if not user or not user.is_authenticated:
        return False
    return (
        user.is_superuser
        or (user.username or "").strip().lower() == ADMIN_USERNAME.lower()
    )


def _format_user_label(user):
    try:
        profile = user.profile
        display = (profile.display_name or "").strip()
        if display:
            return display
    except Exception:
        pass
    return user.username


def _sanitize_accent_color(color_value, is_admin=False):
    raw = (color_value or "").strip()
    if raw.lower() == "#ffd700" and not is_admin:
        return "#7c3aed"
    if re.match(r"^#[0-9a-fA-F]{6}$", raw):
        lowered = raw.lower()
        if lowered in STANDARD_ACCENT_COLORS or is_admin:
            return lowered
    return "#7c3aed"


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
    if not data_url or not isinstance(data_url, str):
        return None
    if not data_url.startswith("data:"):
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
        import base64

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
    profile, _ = Profile.objects.get_or_create(user=user)
    is_admin = _is_kiwi_admin(user)
    changed = False

    if is_admin and not profile.is_admin_badge:
        profile.is_admin_badge = True
        changed = True
    if not profile.accent_color:
        profile.accent_color = "#7c3aed"
        changed = True
    if changed:
        profile.save()
    return profile


def _serialize_profile(user, profile):
    is_admin = _is_kiwi_admin(user) or bool(getattr(profile, "is_admin_badge", False))
    avatar_url = _safe_media_url(getattr(profile, "avatar", None))
    banner_url = _safe_media_url(getattr(profile, "banner", None))
    music_url = _safe_media_url(getattr(profile, "music_file", None))
    music_cover_url = _safe_media_url(getattr(profile, "music_cover", None))

    accent = _sanitize_accent_color(getattr(profile, "accent_color", "#7c3aed"), is_admin=is_admin)
    name_effect = _sanitize_name_effect(getattr(profile, "name_effect", "none"), is_admin=is_admin)
    avatar_frame = _sanitize_avatar_frame(getattr(profile, "avatar_frame", "none"), is_admin=is_admin)

    return {
        "username": user.username,
        "display_name": (getattr(profile, "display_name", "") or "").strip() or user.username,
        "bio": getattr(profile, "bio", "") or "",
        "pronouns": getattr(profile, "pronouns", "") or "",
        "status": _sanitize_status(getattr(profile, "status", "online")),
        "custom_status": getattr(profile, "custom_status", "") or "",
        "accent_color": accent,
        "name_effect": name_effect,
        "avatar_frame": avatar_frame,
        "avatar_url": avatar_url,
        "banner_url": banner_url,
        "music_title": getattr(profile, "music_title", "") or "",
        "music_artist": getattr(profile, "music_artist", "") or "",
        "music_url": music_url,
        "music_cover_url": music_cover_url,
        "is_admin": is_admin,
    }


def _are_friends(user_a, user_b):
    if not user_a or not user_b or user_a.id == user_b.id:
        return False
    return Friendship.objects.filter(
        Q(user1=user_a, user2=user_b) | Q(user1=user_b, user2=user_a)
    ).exists()


def _get_friends_list(user):
    friendships = Friendship.objects.filter(Q(user1=user) | Q(user2=user)).select_related(
        "user1", "user2", "user1__profile", "user2__profile"
    )
    friends = []
    seen_ids = set()
    for fs in friendships:
        other = fs.user2 if fs.user1_id == user.id else fs.user1
        if other.id in seen_ids:
            continue
        seen_ids.add(other.id)
        p = _get_or_create_profile(other)
        friends.append(_serialize_profile(other, p))
    return friends


def _ensure_default_workspace(user):
    server, _ = Server.objects.get_or_create(
        name="Kiwi Lounge",
        defaults={"owner": user},
    )
    if not server.owner_id:
        server.owner = user
        server.save()
    server.members.add(user)

    for ch_name in ("geral", "música", "clips"):
        Channel.objects.get_or_create(server=server, name=ch_name)

    servers_qs = Server.objects.filter(members=user).prefetch_related("channels").order_by("id")
    if not servers_qs.exists():
        servers_qs = Server.objects.prefetch_related("channels").order_by("id")
    return list(servers_qs)


def _serialize_message(msg, current_user=None):
    sender = msg.sender
    profile = _get_or_create_profile(sender)
    sender_data = _serialize_profile(sender, profile)

    reactions_summary = {}
    for rx in msg.reactions.all():
        emoji = rx.emoji
        if emoji not in reactions_summary:
            reactions_summary[emoji] = {"emoji": emoji, "count": 0, "me": False}
        reactions_summary[emoji]["count"] += 1
        if current_user and rx.user_id == current_user.id:
            reactions_summary[emoji]["me"] = True

    return {
        "id": msg.id,
        "channel_id": msg.channel_id,
        "sender": sender.username,
        "display_name": sender_data["display_name"],
        "avatar_url": sender_data["avatar_url"],
        "accent_color": sender_data["accent_color"],
        "is_admin": sender_data["is_admin"],
        "message": msg.content or "",
        "text": msg.content or "",
        "attachment_url": _safe_media_url(msg.attachment),
        "attachment_name": os.path.basename(msg.attachment.name) if msg.attachment else "",
        "attachment_type": msg.attachment_type or "",
        "timestamp": timezone.localtime(msg.created_at).strftime("%H:%M"),
        "time": timezone.localtime(msg.created_at).strftime("%H:%M"),
        "reactions": list(reactions_summary.values()),
    }


def _serialize_dm(dm, current_user=None):
    sender = dm.sender
    recipient = dm.recipient
    p_sender = _get_or_create_profile(sender)
    d_sender = _serialize_profile(sender, p_sender)

    return {
        "id": dm.id,
        "sender": sender.username,
        "recipient": recipient.username,
        "display_name": d_sender["display_name"],
        "avatar_url": d_sender["avatar_url"],
        "message": dm.content or "",
        "text": dm.content or "",
        "attachment_url": _safe_media_url(dm.attachment),
        "attachment_name": os.path.basename(dm.attachment.name) if dm.attachment else "",
        "attachment_type": dm.attachment_type or "",
        "timestamp": timezone.localtime(dm.created_at).strftime("%H:%M"),
        "time": timezone.localtime(dm.created_at).strftime("%H:%M"),
    }


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect("room")

    error_message = None
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            _get_or_create_profile(user)
            _ensure_default_workspace(user)
            return redirect("room")
        error_message = "Usuário ou senha inválidos."

    return render(request, "kiwi_chat/login.html", {"error_message": error_message})


@ensure_csrf_cookie
def register_view(request):
    if request.user.is_authenticated:
        return redirect("room")

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
                return redirect("room")
            except IntegrityError:
                error_message = "Não foi possível criar a conta agora."

    return render(request, "kiwi_chat/register.html", {"error_message": error_message})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
@ensure_csrf_cookie
def room_view(request):
    user = request.user
    profile = _get_or_create_profile(user)
    servers = _ensure_default_workspace(user)
    is_admin = _is_kiwi_admin(user)

    first_server = servers[0] if servers else None
    first_channel = None
    if first_server:
        first_channel = first_server.channels.order_by("id").first()

    servers_payload = []
    for srv in servers:
        ch_list = [
            {"id": ch.id, "name": ch.name, "server_id": srv.id}
            for ch in srv.channels.order_by("id")
        ]
        servers_payload.append({
            "id": srv.id,
            "name": srv.name,
            "icon_url": _safe_media_url(getattr(srv, "icon", None)),
            "channels": ch_list,
        })

    friends_payload = _get_friends_list(user)
    incoming_requests = FriendRequest.objects.filter(
        to_user=user, status="pending"
    ).select_related("from_user", "from_user__profile")

    pending_requests_payload = []
    for req in incoming_requests:
        p_from = _get_or_create_profile(req.from_user)
        pending_requests_payload.append({
            "id": req.id,
            "from_username": req.from_user.username,
            "display_name": p_from.display_name or req.from_user.username,
            "avatar_url": _safe_media_url(p_from.avatar),
        })

    profile_data = _serialize_profile(user, profile)

    context = {
        "username": user.username,
        "display_name": profile_data["display_name"],
        "is_admin": is_admin,
        "active_server_id": first_server.id if first_server else 1,
        "active_channel_id": first_channel.id if first_channel else 1,
        "active_channel_name": first_channel.name if first_channel else "geral",
        "servers_json": json.dumps(servers_payload),
        "profile_json": json.dumps(profile_data),
        "friends_json": json.dumps(friends_payload),
        "pending_requests_json": json.dumps(pending_requests_payload),
    }
    return render(request, "kiwi_chat/room.html", context)


@login_required
@require_GET
def api_bootstrap(request):
    user = request.user
    profile = _get_or_create_profile(user)
    servers = _ensure_default_workspace(user)
    is_admin = _is_kiwi_admin(user)

    servers_payload = []
    for srv in servers:
        servers_payload.append({
            "id": srv.id,
            "name": srv.name,
            "icon_url": _safe_media_url(getattr(srv, "icon", None)),
            "channels": [
                {"id": ch.id, "name": ch.name, "server_id": srv.id}
                for ch in srv.channels.order_by("id")
            ],
        })

    friends = _get_friends_list(user)
    incoming = FriendRequest.objects.filter(to_user=user, status="pending").select_related(
        "from_user", "from_user__profile"
    )
    requests_data = [
        {
            "id": r.id,
            "from_username": r.from_user.username,
            "display_name": _format_user_label(r.from_user),
        }
        for r in incoming
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
        .select_related("sender", "sender__profile")
        .prefetch_related("reactions", "reactions__user")
        .order_by("-created_at")[:60]
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
            Q(sender=request.user, recipient=target_user)
            | Q(sender=target_user, recipient=request.user)
        )
        .select_related("sender", "recipient", "sender__profile", "recipient__profile")
        .order_by("created_at")[:80]
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

    content = (payload.get("message") or payload.get("text") or "").strip()
    attachment_data = payload.get("attachment_url") or payload.get("attachment") or ""

    if not content and not attachment_data:
        return JsonResponse({"ok": False, "error": "Mensagem vazia."}, status=400)

    dm = DirectMessage.objects.create(
        sender=request.user,
        recipient=target_user,
        content=content[:2000],
    )

    if request.FILES.get("file"):
        dm.attachment = request.FILES["file"]
        dm.attachment_type = (request.FILES["file"].content_type or "file")[:64]
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

    channel = None
    if channel_id:
        channel = Channel.objects.filter(id=channel_id).first()
    if not channel:
        channel = Channel.objects.order_by("id").first()
    if not channel:
        servers = _ensure_default_workspace(request.user)
        channel = servers[0].channels.order_by("id").first()

    msg = Message.objects.create(
        channel=channel,
        sender=request.user,
        content=content[:2000],
    )
    if uploaded:
        msg.attachment = uploaded
        msg.attachment_type = (uploaded.content_type or "file")[:64]
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
    emoji = (payload.get("emoji") or "👍").strip()[:16]
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
    if msg.sender_id != request.user.id and not _is_kiwi_admin(request.user):
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
    if not name or len(name) > 80:
        return JsonResponse({"ok": False, "error": "Nome de servidor inválido."}, status=400)

    server = Server.objects.create(name=name, owner=request.user)
    server.members.add(request.user)
    default_ch = Channel.objects.create(server=server, name="geral")

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
    if not name or len(name) > 50:
        return JsonResponse({"ok": False, "error": "Nome de canal inválido."}, status=400)

    server = Server.objects.filter(id=server_id).first() if server_id else Server.objects.filter(members=request.user).first()
    if not server:
        servers = _ensure_default_workspace(request.user)
        server = servers[0]

    channel, _ = Channel.objects.get_or_create(server=server, name=name)
    return JsonResponse({
        "ok": True,
        "channel": {"id": channel.id, "name": channel.name, "server_id": server.id},
    })


@login_required
@require_GET
def api_user_profile(request, username):
    target_user = get_object_or_404(User, username__iexact=username)
    profile = _get_or_create_profile(target_user)
    data = _serialize_profile(target_user, profile)
    data["is_friend"] = _are_friends(request.user, target_user)
    data["is_self"] = request.user.id == target_user.id
    return JsonResponse({"ok": True, "profile": data})


@login_required
@require_POST
def api_update_profile(request):
    user = request.user
    profile = _get_or_create_profile(user)
    is_admin = _is_kiwi_admin(user)

    if request.content_type and "multipart/form-data" in request.content_type:
        data = request.POST
    else:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            data = request.POST

    if "display_name" in data:
        profile.display_name = (data.get("display_name") or "").strip()[:60]
    if "bio" in data:
        profile.bio = (data.get("bio") or "").strip()[:300]
    if "pronouns" in data:
        profile.pronouns = (data.get("pronouns") or "").strip()[:40]
    if "status" in data:
        profile.status = _sanitize_status(data.get("status"))
    if "custom_status" in data:
        profile.custom_status = (data.get("custom_status") or "").strip()[:120]
    if "accent_color" in data:
        profile.accent_color = _sanitize_accent_color(data.get("accent_color"), is_admin=is_admin)
    if "name_effect" in data:
        profile.name_effect = _sanitize_name_effect(data.get("name_effect"), is_admin=is_admin)
    if "avatar_frame" in data:
        profile.avatar_frame = _sanitize_avatar_frame(data.get("avatar_frame"), is_admin=is_admin)
    if "music_title" in data:
        profile.music_title = (data.get("music_title") or "").strip()[:120]
    if "music_artist" in data:
        profile.music_artist = (data.get("music_artist") or "").strip()[:120]

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

    if request.FILES.get("music_file"):
        profile.music_file = request.FILES["music_file"]
    elif isinstance(data.get("music_url"), str) and data.get("music_url").startswith("data:"):
        saved = _save_base64_media(data.get("music_url"), prefix="music")
        if saved:
            profile.music_file.name = saved.replace(settings.MEDIA_URL, "", 1).lstrip("/")

    if request.FILES.get("music_cover"):
        profile.music_cover = request.FILES["music_cover"]
    elif isinstance(data.get("music_cover_url"), str) and data.get("music_cover_url").startswith("data:"):
        saved = _save_base64_media(data.get("music_cover_url"), prefix="cover")
        if saved:
            profile.music_cover.name = saved.replace(settings.MEDIA_URL, "", 1).lstrip("/")

    if is_admin:
        profile.is_admin_badge = True

    profile.save()
    return JsonResponse({"ok": True, "profile": _serialize_profile(user, profile)})


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
        from_user=request.user,
        to_user=target_user,
        defaults={"status": "pending"},
    )
    if not created and req.status != "pending":
        req.status = "pending"
        req.save()

    return JsonResponse({
        "ok": True,
        "request_id": req.id,
        "to_username": target_user.username,
        "message": f"Solicitação enviada para {target_user.username}.",
    })


@login_required
@require_POST
def api_respond_friend_request(request, request_id):
    freq = get_object_or_404(FriendRequest, id=request_id, to_user=request.user)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    action = (payload.get("action") or "").strip().lower()
    if action in ("accept", "aceitar"):
        freq.status = "accepted"
        freq.save()
        u1, u2 = sorted([request.user, freq.from_user], key=lambda u: u.id)
        Friendship.objects.get_or_create(user1=u1, user2=u2)
        return JsonResponse({
            "ok": True,
            "accepted": True,
            "friend": _serialize_profile(freq.from_user, _get_or_create_profile(freq.from_user)),
        })
    elif action in ("reject", "decline", "recusar"):
        freq.status = "rejected"
        freq.save()
        return JsonResponse({"ok": True, "accepted": False})

    return JsonResponse({"ok": False, "error": "Ação inválida."}, status=400)


@login_required
@require_POST
def api_remove_friend(request, username):
    target_user = get_object_or_404(User, username__iexact=username)
    Friendship.objects.filter(
        Q(user1=request.user, user2=target_user) | Q(user1=target_user, user2=request.user)
    ).delete()
    return JsonResponse({"ok": True, "removed": target_user.username})
