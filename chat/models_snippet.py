from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    banner = models.ImageField(upload_to='banners/', blank=True, null=True)
    bubble = models.CharField(max_length=255, blank=True, default='🌸 a testar o novo kiwi chat...')
    pronouns = models.CharField(max_length=100, blank=True, default='utilizador • 🥝')
    bio = models.TextField(blank=True, default='Perfil estético ativo.')
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
            ServerMemberRole.objects.get_or_create(
                server=self,
                user_id=self.owner_id,
                defaults={'role': 'owner'},
            )

    def __str__(self):
        return self.name


class ServerMemberRole(models.Model):
    ROLE_CHOICES = [
        ('owner', 'Owner'),
        ('admin', 'Moderador'),
        ('member', 'Membro'),
    ]

    server = models.ForeignKey(
        Server,
        on_delete=models.CASCADE,
        related_name='member_roles',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='server_roles',
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='member',
    )

    class Meta:
        unique_together = ('server', 'user')

    def __str__(self):
        return f'{self.user.username} @ {self.server.name} ({self.get_role_display()})'