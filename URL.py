"""
equipment/urls.py 에 추가할 경로 4개
────────────────────────────────────────────────────────
name= 값은 monitor.html 의 url 태그와 정확히 일치해야 한다.
"""

from django.urls import path
from . import views_analysis as va
from . import views_monitor as vm

urlpatterns = [
    # ... 기존 경로 ...

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
    path('api/monitor/detail/',  vm.monitor_detail,  name='monitor-detail'),
]
