from django.urls import path
from . import views

urlpatterns = [
    path('', views.room_view, name='room_home'),
    path('chat/<str:room_name>/', views.room_view, name='room_chat'),
    path('server/<int:server_id>/', views.room_view, name='room_server'),
    path('server/<int:server_id>/channel/<int:channel_id>/', views.room_view, name='room_channel'),

    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('accounts/login/', views.login_view, name='accounts_login_fallback'),
    path('logout/', views.logout_view, name='logout'),

    path('profile/', views.profile_page_view, name='profile_page'),
    path('api/profile/update/', views.update_profile, name='update_profile'),
    path('api/profile/<str:username>/', views.get_user_profile, name='get_user_profile'),

    path('api/servers/create/', views.create_server, name='create_server'),
    path('api/servers/<int:server_id>/channels/create/', views.create_channel, name='create_channel'),

    path('api/users/search/', views.search_users, name='search_users'),
    path('api/friend-request/send/', views.send_friend_request, name='send_friend_request'),
    path('api/friend-requests/list/', views.get_friend_requests, name='get_friend_requests'),
    path('api/friend-request/accept/', views.accept_friend_request, name='accept_friend_request'),
    path('api/friends/list/', views.get_friends_list, name='get_friends_list'),
    path('api/friends/remove/', views.remove_friend, name='remove_friend'),

    path('api/dm/<str:username>/', views.get_dm_history, name='dm_history'),
    path('api/upload-attachment/', views.upload_attachment, name='upload_attachment'),

    path('api/musics/saved/', views.list_saved_musics, name='list_saved_musics'),
    path('api/musics/save/', views.create_saved_music, name='create_saved_music'),
    path('api/musics/delete/<int:music_id>/', views.delete_saved_music, name='delete_saved_music'),
    path('api/musics/friends-feed/', views.friend_music_feed, name='friend_music_feed'),
]