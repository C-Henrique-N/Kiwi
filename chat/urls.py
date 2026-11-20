from django.contrib.auth import views as auth_views
from django.urls import path
from . import views

urlpatterns = [
    path('chat/<str:room_name>/', views.room_view, name='room'),
    path('api/profile/update/', views.update_profile, name='update_profile'), # Corrigido aqui
    path('api/profile/<str:username>/', views.get_user_profile, name='get_user_profile'),
    path('', views.room_view, name='room'),
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('api/friend-request/send/', views.send_friend_request, name='send_friend_request'),
    path('api/friend-requests/list/', views.get_friend_requests, name='get_friend_requests'),
    path('api/friend-request/accept/', views.accept_friend_request, name='accept_friend_request'),
    path('api/friends/list/', views.get_friends_list, name='get_friends_list'),
    path('accounts/login/', views.login_view, name='accounts_login_fallback'),
    path('api/dm/<str:username>/', views.get_dm_history, name='dm_history'),
    path('api/upload-attachment/', views.upload_attachment, name='upload_attachment'),
    path('profile/', views.profile_page_view, name='profile_page'),
    path('api/friends/remove/', views.remove_friend, name='remove_friend'),
    path('api/musics/saved/', views.list_saved_musics, name='list_saved_musics'),
    path('api/musics/save/', views.create_saved_music, name='create_saved_music'),
    path('api/musics/delete/<int:music_id>/', views.delete_saved_music, name='delete_saved_music'),
]