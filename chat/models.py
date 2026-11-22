from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    banner = models.ImageField(upload_to='banners/', blank=True, null=True)
    bubble = models.CharField(max_length=255, blank=True, default='🌸 a testar o novo kiwi chat...')
    pronouns = models.CharField(max_length=100, blank=True, default='utilizador • 🥝')
    bio = models.TextField(blank=True, default='Perfil estético ativo.')
    location = models.CharField(max_length=120, blank=True, default='')
    spotify_song = models.CharField(max_length=150, blank=True, default='Drama')
    spotify_artist = models.CharField(max_length=150, blank=True, default='Lala Sadii')
    accent_color = models.CharField(max_length=50, default='#ff9de2')
    frame_effect = models.CharField(max_length=50, blank=True, default='none')
    profile_glow = models.BooleanField(default=False)
    avatar_pulse = models.BooleanField(default=False)
    name_style = models.CharField(max_length=50, blank=True, default='')
    music_url = models.URLField(blank=True, default='')
    music_audio_file = models.FileField(upload_to='music_audios/', blank=True, null=True)
    music_audio_url = models.TextField(blank=True, default='')
    music_cover_file = models.ImageField(upload_to='music_covers/', blank=True, null=True)
    music_cover_url = models.TextField(blank=True, default='')

    @property
    def is_admin_user(self):
        return bool(self.user.is_staff or self.user.is_superuser)

    @property
    def is_akiira_admin(self):
        return bool(
            self.user
            and self.user.username.lower() == 'akiirapw'
            and (self.user.is_staff or self.user.is_superuser)
        )

    def get_active_frame_effect(self):
        return (self.frame_effect or 'none') if self.is_akiira_admin else 'none'

    def has_active_profile_glow(self):
        return bool(self.profile_glow) if self.is_akiira_admin else False

    def has_active_avatar_pulse(self):
        return bool(self.avatar_pulse) if self.is_akiira_admin else False

    def get_active_name_style(self):
        return (self.name_style or '') if self.is_akiira_admin else ''

    def get_music_audio_src(self):
        if self.music_audio_file:
            return self.music_audio_file.url
        return self.music_audio_url or ''

    def get_music_cover_src(self):
        if self.music_cover_file:
            return self.music_cover_file.url
        return self.music_cover_url or ''

    def to_dict(self):
        return {
            'username': self.user.username,
            'avatar_url': self.avatar.url if self.avatar else '',
            'banner_url': self.banner.url if self.banner else '',
            'bubble': self.bubble,
            'pronouns': self.pronouns,
            'bio': self.bio,
            'location': self.location,
            'spotify_song': self.spotify_song,
            'spotify_artist': self.spotify_artist,
            'accent_color': self.accent_color,
            'frame_effect': self.get_active_frame_effect(),
            'profile_glow': self.has_active_profile_glow(),
            'avatar_pulse': self.has_active_avatar_pulse(),
            'name_style': self.get_active_name_style(),
            'music_url': self.music_url,
            'music_audio_url': self.get_music_audio_src(),
            'music_cover_url': self.get_music_cover_src(),
            'is_admin': self.is_admin_user,
            'is_akiira_admin': self.is_akiira_admin,
        }

    def __str__(self):
        return self.user.username


class Server(models.Model):
    name = models.CharField(max_length=120)
    icon = models.ImageField(upload_to='server_icons/', blank=True, null=True)
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='owned_servers',
    )
    members = models.ManyToManyField(
        User,
        related_name='joined_servers',
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and self.owner_id:
            self.members.add(self.owner)
            ServerMember.objects.get_or_create(server=self, user=self.owner)

    def __str__(self):
        return self.name


class ServerRole(models.Model):
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name='roles')
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=20, default='#8a63d2')
    position = models.PositiveIntegerField(default=0)
    can_manage_channels = models.BooleanField(default=False)
    can_kick_members = models.BooleanField(default=False)
    can_manage_roles = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position', 'id']
        unique_together = ('server', 'name')

    def to_dict(self):
        return {
            'id': self.id,
            'server_id': self.server_id,
            'name': self.name,
            'color': self.color,
            'position': self.position,
            'can_manage_channels': self.can_manage_channels,
            'can_kick_members': self.can_kick_members,
            'can_manage_roles': self.can_manage_roles,
        }

    def __str__(self):
        return f'{self.name} ({self.server.name})'


class ServerMember(models.Model):
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name='server_memberships')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='server_memberships')
    role = models.ForeignKey(
        ServerRole,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='members',
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['joined_at', 'id']
        unique_together = ('server', 'user')

    def __str__(self):
        role_name = self.role.name if self.role else 'Sem cargo'
        return f'{self.user.username} em {self.server.name} [{role_name}]'


class Channel(models.Model):
    CHANNEL_TYPES = [
        ('text', 'Texto'),
        ('audio', 'Áudio / Música'),
    ]

    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name='channels')
    name = models.CharField(max_length=100)
    channel_type = models.CharField(max_length=16, choices=CHANNEL_TYPES, default='text')
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position', 'id']
        unique_together = ('server', 'name')

    def __str__(self):
        return f'{self.server.name} / #{self.name}'


class FriendRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pendente'),
        ('accepted', 'Aceito'),
        ('rejected', 'Recusado'),
    ]

    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_friend_requests')
    receiver = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_friend_requests')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('sender', 'receiver')
        ordering = ['-updated_at', '-created_at']
        indexes = [
            models.Index(fields=['sender', 'status']),
            models.Index(fields=['receiver', 'status']),
        ]

    @staticmethod
    def are_friends(user_a, user_b):
        if not user_a or not user_b or user_a.pk == user_b.pk:
            return False
        return FriendRequest.objects.filter(
            status='accepted'
        ).filter(
            Q(sender=user_a, receiver=user_b) | Q(sender=user_b, receiver=user_a)
        ).exists()

    @staticmethod
    def get_friends_for_user(user):
        if not user or not user.pk:
            return []
        qs = FriendRequest.objects.filter(status='accepted').filter(
            Q(sender=user) | Q(receiver=user)
        ).select_related('sender', 'receiver')
        friends = []
        seen_ids = set()
        for req in qs:
            peer = req.receiver if req.sender_id == user.pk else req.sender
            if peer.id not in seen_ids:
                seen_ids.add(peer.id)
                friends.append({
                    'id': peer.id,
                    'username': peer.username,
                    'friendship_id': req.id,
                })
        return friends

    def __str__(self):
        return f'{self.sender} -> {self.receiver} ({self.status})'


class Message(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='messages')
    channel = models.ForeignKey(
        Channel,
        on_delete=models.CASCADE,
        related_name='messages',
        null=True,
        blank=True,
    )
    reply_to = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        related_name='replies',
        null=True,
        blank=True,
    )
    content = models.TextField(blank=True)
    attachment = models.FileField(upload_to='chat_attachments/', blank=True, null=True)
    attachment_type = models.CharField(max_length=20, blank=True)
    client_id = models.CharField(max_length=100, blank=True, default='', db_index=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp', 'id']
        indexes = [
            models.Index(fields=['channel', 'timestamp']),
        ]

    def to_dict(self):
        return {
            'id': self.id,
            'client_id': self.client_id,
            'kind': 'channel',
            'channel_id': self.channel_id,
            'server_id': self.channel.server_id if self.channel else None,
            'author_id': self.user_id,
            'author': self.user.username,
            'reply_to': self.reply_to_id,
            'content': self.content,
            'attachment_url': self.attachment.url if self.attachment else '',
            'attachment_name': self.attachment.name.split('/')[-1] if self.attachment else '',
            'attachment_type': self.attachment_type,
            'timestamp': self.timestamp.isoformat(),
        }

    def __str__(self):
        target = self.channel.name if self.channel else 'sem-canal'
        return f'[{target}] {self.user.username}: {self.content[:30]}'


class PrivateMessage(models.Model):
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_dm_messages')
    receiver = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_dm_messages')
    reply_to = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        related_name='dm_replies',
        null=True,
        blank=True,
    )
    content = models.TextField(blank=True)
    attachment = models.FileField(upload_to='chat_attachments/', blank=True, null=True)
    attachment_type = models.CharField(max_length=20, blank=True)
    client_id = models.CharField(max_length=100, blank=True, default='', db_index=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp', 'id']
        indexes = [
            models.Index(fields=['sender', 'receiver', 'timestamp']),
        ]

    def to_dict(self):
        return {
            'id': self.id,
            'client_id': self.client_id,
            'kind': 'dm',
            'author_id': self.sender_id,
            'author': self.sender.username,
            'recipient_id': self.receiver_id,
            'recipient': self.receiver.username,
            'reply_to': self.reply_to_id,
            'content': self.content,
            'attachment_url': self.attachment.url if self.attachment else '',
            'attachment_name': self.attachment.name.split('/')[-1] if self.attachment else '',
            'attachment_type': self.attachment_type,
            'timestamp': self.timestamp.isoformat(),
        }

    def __str__(self):
        return f'DM {self.sender.username} -> {self.receiver.username}: {self.content[:30]}'


class SavedMusic(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_musics')
    title = models.CharField(max_length=150, blank=True)
    artist = models.CharField(max_length=150, blank=True)
    music_url = models.URLField(blank=True)
    audio_file = models.FileField(upload_to='music_audios/', blank=True, null=True)
    audio_url = models.TextField(blank=True)
    cover_file = models.ImageField(upload_to='music_covers/', blank=True, null=True)
    cover_url = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def get_audio_src(self):
        if self.audio_file:
            return self.audio_file.url
        return self.audio_url or ''

    def get_cover_src(self):
        if self.cover_file:
            return self.cover_file.url
        return self.cover_url or ''

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'artist': self.artist,
            'music_url': self.music_url,
            'audio_url': self.get_audio_src(),
            'cover_url': self.get_cover_src(),
            'created_at': self.created_at.isoformat(),
        }

    def __str__(self):
        return f'{self.user.username}: {self.title or "Música"}'


class ProfilePost(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='profile_posts')
    content = models.TextField()
    image = models.ImageField(upload_to='profile_posts/', blank=True, null=True)
    image_url = models.TextField(blank=True, default='')
    liked_by = models.ManyToManyField(User, related_name='liked_profile_posts', blank=True)
    reposts_count = models.PositiveIntegerField(default=0)
    views_count = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def get_image_src(self):
        if self.image:
            return self.image.url
        return self.image_url or ''

    def to_dict(self, current_user=None):
        replies_qs = self.replies.select_related('author').all()
        return {
            'id': f'post-{self.id}',
            'db_id': self.id,
            'author': self.author.username,
            'handle': f'@{self.author.username.lower()}',
            'content': self.content,
            'image_url': self.get_image_src(),
            'likes': self.liked_by.count(),
            'liked': bool(current_user and current_user.is_authenticated and self.liked_by.filter(pk=current_user.pk).exists()),
            'reposts': self.reposts_count,
            'views': str(self.views_count),
            'replies': replies_qs.count(),
            'reply_items': [
                {
                    'id': r.id,
                    'author': r.author.username,
                    'text': r.content,
                    'created_at': r.created_at.isoformat(),
                }
                for r in replies_qs
            ],
            'created_at': self.created_at.isoformat(),
        }

    def __str__(self):
        return f'Post #{self.id} por {self.author.username}'


class ProfilePostReply(models.Model):
    post = models.ForeignKey(ProfilePost, on_delete=models.CASCADE, related_name='replies')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='profile_post_replies')
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'Resposta de {self.author.username} no Post #{self.post_id}'


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


@receiver(m2m_changed, sender=Server.members.through)
def sync_server_memberships(sender, instance, action, pk_set, **kwargs):
    if action == 'post_add' and pk_set:
        existing = set(
            ServerMember.objects.filter(server=instance, user_id__in=pk_set).values_list('user_id', flat=True)
        )
        missing_ids = [uid for uid in pk_set if uid not in existing]
        if missing_ids:
            ServerMember.objects.bulk_create([
                ServerMember(server=instance, user_id=uid) for uid in missing_ids
            ])
    elif action == 'post_remove' and pk_set:
        ServerMember.objects.filter(server=instance, user_id__in=pk_set).delete()
    elif action == 'post_clear':
        ServerMember.objects.filter(server=instance).delete()
        if instance.owner_id:
            instance.members.add(instance.owner)
            ServerMember.objects.get_or_create(server=instance, user=instance.owner)


# === COMPATIBILIDADE E MODELOS COMPLEMENTARES DO KIWI ===

DirectMessage = PrivateMessage


class Friendship(models.Model):
    user1 = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="friendships_as_user1",
    )
    user2 = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="friendships_as_user2",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("user1", "user2")

    def __str__(self):
        return f"{self.user1.username} <-> {self.user2.username}"


class MessageReaction(models.Model):
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="reactions",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="message_reactions",
    )
    emoji = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        unique_together = ("message", "user", "emoji")

    def __str__(self):
        return f"{self.user.username} {self.emoji} on msg #{self.message_id}"


Profile = UserProfile