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
    path('loja/', views.loja_view, name='loja'),

    # Seguidores / Seguindo
    path('api/follow/<str:username>/', views.toggle_follow, name='toggle_follow_user'),
    path('api/follow/<str:username>/list/', views.list_user_follows, name='list_user_follows'),
    path('api/follow/', views.toggle_follow, name='toggle_follow'),

    # Postagens do Perfil
    path('api/profile/posts/', views.list_profile_posts, name='list_profile_posts'),
    path('api/profile/posts/create/', views.create_profile_post, name='create_profile_post'),
    path('api/posts/create/', views.create_profile_post, name='create_post_alias'),
    path('api/profile/posts/<int:post_id>/like/', views.toggle_like_profile_post, name='toggle_like_profile_post'),
    path('api/profile/posts/<int:post_id>/reply/', views.add_profile_post_reply, name='add_profile_post_reply'),
    path('api/profile/posts/<int:post_id>/delete/', views.delete_profile_post, name='delete_profile_post'),

    path('api/servers/create/', views.create_server, name='create_server'),
    path('api/servers/<int:server_id>/update/', views.update_server, name='update_server'),
    path('api/servers/<int:server_id>/delete/', views.delete_server, name='delete_server'),
    path('api/servers/<int:server_id>/channels/create/', views.create_channel, name='create_channel'),
    path('api/servers/<int:server_id>/channels/<int:channel_id>/delete/', views.delete_channel, name='delete_channel'),

    # Cargos e membros do servidor
    path('api/servers/<int:server_id>/members/', views.get_server_members_grouped, name='server_members_grouped'),
    path('api/servers/<int:server_id>/members/add/', views.add_server_member, name='add_server_member'),
    path('api/servers/<int:server_id>/members/<int:user_id>/kick/', views.kick_server_member, name='kick_server_member'),
    path('api/servers/<int:server_id>/members/<int:user_id>/role/', views.assign_member_role, name='assign_member_role'),
    path('api/servers/<int:server_id>/roles/create/', views.create_server_role, name='create_server_role'),
    path('api/servers/<int:server_id>/roles/<int:role_id>/delete/', views.delete_server_role, name='delete_server_role'),

    path('api/users/search/', views.search_users, name='search_users'),
    path('api/friend-request/send/', views.send_friend_request, name='send_friend_request'),
    path('api/friend-requests/list/', views.get_friend_requests, name='get_friend_requests'),
    path('api/friend-request/accept/', views.accept_friend_request, name='accept_friend_request'),
    path('api/friend-request/reject/', views.reject_friend_request, name='reject_friend_request'),
    path('api/friends/list/', views.get_friends_list, name='get_friends_list'),
    path('api/friends/remove/', views.remove_friend, name='remove_friend'),

    path('api/messages/send/', views.send_message, name='send_message'),
    path('api/messages/<int:message_id>/delete/', views.delete_message, name='delete_message'),
    path('api/dm/message/<int:message_id>/delete/', views.delete_dm_message, name='delete_dm_message'),
    path('api/dm/<str:username>/', views.get_dm_history, name='dm_history'),
    path('api/upload-attachment/', views.upload_attachment, name='upload_attachment'),

    path('api/musics/saved/', views.list_saved_musics, name='list_saved_musics'),
    path('api/musics/save/', views.create_saved_music, name='create_saved_music'),
    path('api/musics/delete/<int:music_id>/', views.delete_saved_music, name='delete_saved_music'),
    path('api/musics/friends-feed/', views.friend_music_feed, name='friend_music_feed'),

    path("api/shop/equip/", views.api_shop_equip, name="api_shop_equip"),
    path("api/shop/unequip/", views.api_shop_unequip, name="api_shop_unequip"),
]