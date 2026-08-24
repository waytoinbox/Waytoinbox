from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path('Sales-Outreach/inbox/',                       views.so_inbox,               name='so_inbox'),
    path('Sales-Outreach/inbox/conversations/',         views.so_inbox_conversations, name='so_inbox_conversations'),
    path('Sales-Outreach/inbox/<int:conversation_id>/', views.so_inbox_thread,        name='so_inbox_thread'),
    path('Sales-Outreach/inbox/reply/',                 views.so_inbox_reply,         name='so_inbox_reply'),
    path('Sales-Outreach/inbox/compose/',                views.so_inbox_compose,       name='so_inbox_compose'),
    path('Sales-Outreach/inbox/upload-image/',           views.so_inbox_upload_image,  name='so_inbox_upload_image'),
    path('Sales-Outreach/inbox/note/',                  views.so_inbox_note_add,      name='so_inbox_note_add'),
    path('Sales-Outreach/inbox/action/',                views.so_inbox_action,        name='so_inbox_action'),
]
