"""
equipment/urls.py 에 추가할 경로 4개
────────────────────────────────────────────────────────
name= 값은 monitor.html 의 url 태그와 정확히 일치해야 한다.
"""

from django.urls import path
from . import views_analysis as va
from . import views_monitor as vm
from . import views_config as vc

urlpatterns = [
    # ... 기존 경로 ...

    # 기준정보 셋업 (신규) — 구닥스를 대체하는 원본 관리 화면
    path('config/',              vc.config_page,     name='config'),
    path('api/config/opers/',    vc.config_opers,    name='config-opers'),
    path('api/config/oper/',     vc.config_oper,     name='config-oper'),
    path('api/config/save/',     vc.config_save,     name='config-save'),
    path('api/config/delete/',   vc.config_delete,   name='config-delete'),
    path('api/config/import/',   vc.config_import,   name='config-import'),
    path('api/config/validate/', vc.config_validate, name='config-validate'),
    path('api/config/suggest/',     vc.config_suggest,     name='config-suggest'),
    path('api/config/suggest-lot/', vc.config_suggest_lot, name='config-suggest-lot'),
    path('api/config/classify/',    vc.config_classify,    name='config-classify'),
    path('api/config/reclassify/',  vc.config_reclassify,  name='config-reclassify'),
    path('api/config/mismatch/',    vc.config_mismatch,    name='config-mismatch'),

    # 산포 분석 (기존)
    path('analysis/',             va.analysis_page,    name='analysis'),
    path('api/analysis/options/', va.analysis_options, name='analysis-options'),
    path('api/analysis/trend/',   va.analysis_trend,   name='analysis-trend'),
    path('api/analysis/corr/',    va.analysis_corr,    name='analysis-corr'),
    path('api/analysis/stats/',   va.analysis_stats,   name='analysis-stats'),
    path('api/analysis/insight/', va.analysis_insight, name='analysis-insight'),
    path('api/analysis/chat/',    va.analysis_chat,    name='analysis-chat'),

    # Inline Monitoring (신규)
    path('monitor/',             vm.monitor_page,    name='monitor'),
    path('api/monitor/opers/',   vm.monitor_opers,   name='monitor-opers'),
    path('api/monitor/run/',     vm.monitor_run,     name='monitor-run'),
    path('api/monitor/results/', vm.monitor_results, name='monitor-results'),
    path('api/monitor/clear/',   vm.monitor_clear,   name='monitor-clear'),
    path('api/monitor/diag/',    vm.monitor_diag,    name='monitor-diag'),
    path('api/monitor/detail/',  vm.monitor_detail,  name='monitor-detail'),
]
