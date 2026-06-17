from django.urls import path
from . import views

urlpatterns = [
    path('',                     views.home,            name='home'),
    path('equipment/',           views.equipment_page,  name='equipment'),
    path('wip/',                 views.wip_page,        name='wip'),
    path('llm/',                 views.llm_page,        name='llm'),
    path('rtd/',                 views.rtd_page,        name='rtd'),
    path('api/llm/',             views.llm_api,         name='llm-api'),
    path('api/equipment/',       views.equipment_api,   name='equipment-api'),
    path('capa/',                views.capa_page,       name='capa'),
    path('capa/settings/',       views.capa_settings,   name='capa-settings'),
    path('api/capa/save/',       views.capa_save_api,   name='capa-save-api'),
    path('api/capa/update-row/', views.capa_update_row, name='capa-update-row'),
    path('api/capa/delete/',     views.capa_delete_api, name='capa-delete-api'),
    path('api/capa/update-tat/', views.capa_update_tat, name='capa-update-tat'),
    path('api/capa/update-eq/',  views.capa_update_eq,  name='capa-update-eq'),
    path('api/capa/delete-eq/',  views.capa_delete_eq,  name='capa-delete-eq'),
]
