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

    if is_admin and not getattr(profile, "is_admin_badge", False):
        try:
            profile.is_admin_badge = True
            changed = True
        except Exception:
            pass
    if not getattr(profile, "accent_color", None):
        profile.accent_color = "#7c3aed"
        changed = True
    if changed:
        profile.save()
    return profile


def _serialize_profile(user, profile):
    is_admin = _is_kiwi_admin(user) or bool(getattr(profile, "is_admin_badge", False))
    avatar_url = _safe_media_url(getattr(profile, "avatar", None))
    banner_url = _safe_media_url(getattr(profile, "banner", None))
    music_url = _safe_media_url(getattr(profile, "music_file", None)) or _safe_media_url(getattr(profile, "music_audio_file", None))
    music_cover_url = _safe_media_url(getattr(profile, "music_cover", None)) or _safe_media_url(getattr(profile, "music_cover_file", None))

    accent = _sanitize_accent_color(getattr(profile, "accent_color", "#7c3aed"), is_admin=is_admin)
    name_effect = _sanitize_name_effect(getattr(profile, "name_effect", "none"), is_admin=is_admin)
    avatar_frame = _sanitize_avatar_frame(getattr(profile, "avatar_frame", "none"), is_admin=is_admin)

    return {
        "username": user.username,
        "display_name": (getattr(profile, "display_name", "") or "").strip() or user.username,
        "bio": getattr(profile, "bio", "") or "",
        "pronouns": getattr(profile, "pronouns", "") or "",
        "status": _sanitize_status(getattr(profile, "status", "online")),
        "custom_status": getattr(profile, "custom_status", "") or getattr(profile, "bubble", "") or "",
        "accent_color": accent,
        "name_effect": name_effect,
        "avatar_frame": avatar_frame,
        "avatar_url": avatar_url,
        "banner_url": banner_url,
        "music_title": getattr(profile, "music_title", "") or getattr(profile, "spotify_song", "") or "",
        "music_artist": getattr(profile, "music_artist", "") or getattr(profile, "spotify_artist", "") or "",
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
    sender = getattr(msg, "sender", None) or getattr(msg, "user", None)
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

    created = getattr(msg, "created_at", None) or getattr(msg, "timestamp", None) or timezone.now()

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
        "timestamp": timezone.localtime(created).strftime("%H:%M"),
        "time": timezone.localtime(created).strftime("%H:%M"),
        "reactions": list(reactions_summary.values()),
    }


def _serialize_dm(dm, current_user=None):
    sender = getattr(dm, "sender", None)
    recipient = getattr(dm, "recipient", None) or getattr(dm, "receiver", None)
    p_sender = _get_or_create_profile(sender)
    d_sender = _serialize_profile(sender, p_sender)
    created = getattr(dm, "created_at", None) or getattr(dm, "timestamp", None) or timezone.now()

    return {
        "id": dm.id,
        "sender": sender.username,
        "recipient": recipient.username if recipient else "",
        "display_name": d_sender["display_name"],
        "avatar_url": d_sender["avatar_url"],
        "message": dm.content or "",
        "text": dm.content or "",
        "attachment_url": _safe_media_url(getattr(dm, "attachment", None) or getattr(dm, "attachment_url", "")),
        "attachment_name": os.path.basename(dm.attachment.name) if getattr(dm, "attachment", None) else "",
        "attachment_type": getattr(dm, "attachment_type", "") or "",
        "timestamp": timezone.localtime(created).strftime("%H:%M"),
        "time": timezone.localtime(created).strftime("%H:%M"),
    }


@ensure_csrf_cookie


def _render_first_existing(request, template_names, context):
    from django.template import TemplateDoesNotExist
    from django.template.loader import select_template
    try:
        tpl = select_template(template_names)
        return render(request, tpl.template.name, context)
    except TemplateDoesNotExist:
        from django.http import HttpResponse
        return HttpResponse(
            f"<h1>Kiwi Chat</h1><p>Template nao encontrado ({', '.join(template_names)}).</p>",
            status=200,
        )

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

    return _render_first_existing(request, ["chat/login.html", "kiwi_chat/login.html", "registration/login.html", "login.html"], {"error_message": error_message})


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

    return _render_first_existing(request, ["chat/register.html", "kiwi_chat/register.html", "registration/register.html", "register.html"], {"error_message": error_message})


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
        channels_sorted = list(selected_server.channels.order_by("id"))
        if channel_id:
            selected_channel = next((c for c in channels_sorted if c.id == int(channel_id)), None)
        if not selected_channel and room_name:
            selected_channel = next((c for c in channels_sorted if c.name.lower() == str(room_name).lower()), None)
        if not selected_channel and channels_sorted:
            selected_channel = channels_sorted[0]

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
        status="pending"
    ).filter(
        Q(to_user=user) | Q(receiver=user)
    ).select_related("from_user", "to_user", "sender", "receiver")

    pending_requests_payload = []
    for req in incoming_requests:
        origin = getattr(req, "from_user", None) or getattr(req, "sender", None)
        if origin and origin.id != user.id:
            p_from = _get_or_create_profile(origin)
            pending_requests_payload.append({
                "id": req.id,
                "from_username": origin.username,
                "display_name": getattr(p_from, "display_name", "") or origin.username,
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
    return _render_first_existing(
        request,
        ["chat/loja.html", "kiwi_chat/loja.html", "loja.html"],
        {
            "username": user.username,
            "profile": profile,
            "profile_data": profile_data,
            "profile_json": json.dumps(profile_data),
            "is_admin": _is_kiwi_admin(user),
        },
    )


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
    incoming = FriendRequest.objects.filter(status="pending").filter(
        Q(to_user=user) | Q(receiver=user)
    ).select_related("from_user", "sender")
    requests_data = []
    for r in incoming:
        origin = getattr(r, "from_user", None) or getattr(r, "sender", None)
        if origin and origin.id != user.id:
            requests_data.append({
                "id": r.id,
                "from_username": origin.username,
                "display_name": _format_user_label(origin),
            })

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
    order_col = "-created_at" if hasattr(Message, "created_at") else "-timestamp"
    msgs = (
        Message.objects.filter(channel=channel)
        .prefetch_related("reactions", "reactions__user")
        .order_by(order_col, "-id")[:60]
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

    order_col = "created_at" if hasattr(DirectMessage, "created_at") else "timestamp"
    dms = (
        DirectMessage.objects.filter(
            Q(sender=request.user, recipient=target_user) | Q(sender=target_user, recipient=request.user)
            | Q(sender=request.user, receiver=target_user) | Q(sender=target_user, receiver=request.user)
        )
        .order_by(order_col, "id")[:80]
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

    if not content and not attachment_data and not request.FILES.get("file"):
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

    create_kwargs = {"channel": channel, "content": content[:2000]}
    if hasattr(Message, "sender"):
        create_kwargs["sender"] = request.user
    else:
        create_kwargs["user"] = request.user

    msg = Message.objects.create(**create_kwargs)
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
    author_id = getattr(msg, "sender_id", None) or getattr(msg, "user_id", None)
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

    if is_admin and hasattr(profile, "is_admin_badge"):
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
    freq = get_object_or_404(FriendRequest, id=request_id)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = request.POST

    origin = getattr(freq, "from_user", None) or getattr(freq, "sender", None)
    action = (payload.get("action") or "").strip().lower()
    if action in ("accept", "aceitar"):
        freq.status = "accepted"
        freq.save()
        if origin:
            u1, u2 = sorted([request.user, origin], key=lambda u: u.id)
            Friendship.objects.get_or_create(user1=u1, user2=u2)
            return JsonResponse({
                "ok": True,
                "accepted": True,
                "friend": _serialize_profile(origin, _get_or_create_profile(origin)),
            })
        return JsonResponse({"ok": True, "accepted": True})
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
    return JsonResponse({"ok": True})


@login_required
def create_server_role(request, server_id):
    return JsonResponse({"ok": True})


@login_required
def delete_server_role(request, server_id, role_id):
    return JsonResponse({"ok": True})


@login_required
def search_users(request):
    q = (request.GET.get("q") or "").strip()
    qs = User.objects.filter(username__icontains=q).exclude(pk=request.user.pk)[:20] if q else User.objects.none()
    return JsonResponse({"ok": True, "users": [{"id": u.id, "username": u.username} for u in qs]})


@login_required
def get_friend_requests(request):
    incoming = FriendRequest.objects.filter(status="pending").filter(
        Q(to_user=request.user) | Q(receiver=request.user)
    ).select_related("from_user", "sender")
    items = []
    for r in incoming:
        origin = getattr(r, "from_user", None) or getattr(r, "sender", None)
        if origin:
            items.append({"id": r.id, "username": origin.username, "from_username": origin.username})
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
    recipient_username = (payload.get("recipient") or payload.get("to_username") or "").strip()
    if not content and not request.FILES.get("file") and not request.FILES.get("attachment"):
        return JsonResponse({"ok": False, "error": "Mensagem vazia."}, status=400)
    if recipient_username:
        recipient = get_object_or_404(User, username__iexact=recipient_username)
        dm = DirectMessage.objects.create(sender=request.user, recipient=recipient, content=content[:2000])
        return JsonResponse({"ok": True, "message": _serialize_dm(dm, current_user=request.user)})
    channel = get_object_or_404(Channel, pk=channel_id)
    kwargs = {"channel": channel, "content": content[:2000]}
    if hasattr(Message, "sender"):
        kwargs["sender"] = request.user
    else:
        kwargs["user"] = request.user
    msg = Message.objects.create(**kwargs)
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
    return JsonResponse({"ok": True, "musics": []})


@login_required
def create_saved_music(request):
    return JsonResponse({"ok": True})


@login_required
def delete_saved_music(request, music_id):
    return JsonResponse({"ok": True})


@login_required
def friend_music_feed(request):
    return JsonResponse({"ok": True, "feed": []})
