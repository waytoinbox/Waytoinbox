from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path('Sales-Outreach/lists/',                            views.so_lists,                 name='so_lists'),
    path('Sales-Outreach/lists/delete/',                     views.so_list_delete,           name='so_list_delete'),
    path('Sales-Outreach/list/<int:list_id>/rename/',        views.so_list_rename,           name='so_list_rename'),
    path('Sales-Outreach/list/<int:list_id>/duplicate/',     views.so_list_duplicate,        name='so_list_duplicate'),
    path('Sales-Outreach/list/<int:list_id>/download/',      views.so_list_download,         name='so_list_download'),
    path('Sales-Outreach/list/<int:list_id>/toggle-status/', views.so_list_toggle_status,    name='so_list_toggle_status'),
    path('Sales-Outreach/list/<int:list_id>/check/',         views.so_list_check,            name='so_list_check'),
    path('Sales-Outreach/list/<int:list_id>/prospects/',     views.so_list_detail,           name='so_list_detail'),
    path('Sales-Outreach/list/<int:list_id>/prospects/page/', views.so_list_prospects_page,  name='so_list_prospects_page'),
    path('Sales-Outreach/list/<int:list_id>/add-prospect/',  views.so_list_add_prospect,     name='so_list_add_prospect'),
    path('Sales-Outreach/list/<int:list_id>/action/',        views.so_list_detail_action,    name='so_list_detail_action'),
    path('Sales-Outreach/list/<int:list_id>/parse-file/',    views.so_list_parse_file,       name='so_list_parse_file'),
    path('Sales-Outreach/list/<int:list_id>/import/',        views.so_list_import_prospects, name='so_list_import_prospects'),
    path('Sales-Outreach/prospects/<int:prospect_id>/detail/', views.so_prospect_detail,     name='so_prospect_detail'),
]
