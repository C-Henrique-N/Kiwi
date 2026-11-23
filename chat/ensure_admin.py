import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kiwi.settings")
django.setup()

from django.contrib.auth import get_user_model
from chat.models import UserProfile

User = get_user_model()
username = os.environ.get("AkiiraPW", "AkiiraPW").strip()
password = os.environ.get("Tiburu321@", "").strip()
email = os.environ.get("klebrianodograu@gmail.com", "").strip()

if username and password:
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"email": email},
    )
    if created:
        user.set_password(password)
    if email and user.email != email:
        user.email = email
    user.is_staff = True
    user.is_superuser = True
    user.save()
    UserProfile.objects.get_or_create(user=user)
    print(f"Superuser pronto: {user.username} (criado={created})")