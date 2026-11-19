from django.contrib.auth.models import User
from django.db import models
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

    def __str__(self):
        return self.user.username


class FriendRequest(models.Model):
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_friend_requests')
    receiver = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_friend_requests')
    status = models.CharField(max_length=20, default='pending')  # pending, accepted, rejected
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.sender} -> {self.receiver} ({self.status})'


class Message(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    room = models.CharField(max_length=255, default='geral')
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username}: {self.content[:30]}'


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
        
class PrivateMessage(models.Model):
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_dm_messages')
    receiver = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_dm_messages')
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f'DM {self.sender.username} -> {self.receiver.username}: {self.content[:30]}'
    
class Message(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='messages')
    content = models.TextField(blank=True)
    attachment = models.FileField(upload_to='chat_attachments/', blank=True, null=True)
    attachment_type = models.CharField(max_length=20, blank=True)  # 'image' ou 'audio'
    timestamp = models.DateTimeField(auto_now_add=True)