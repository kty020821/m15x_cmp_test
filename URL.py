"""
equipment/urls.py 에 추가할 경로 7개
────────────────────────────────────────────────────────
name= 값은 analysis.html 의 url 태그와 정확히 일치해야 한다.
"""

from django.urls import path
from . import views_analysis as va

urlpatterns = [
    # ... 기존 경로 ...

    path('analysis/',             va.analysis_page,    name='analysis'),
    path('api/analysis/options/', va.analysis_options, name='analysis-options'),
    path('api/analysis/trend/',   va.analysis_trend,   name='analysis-trend'),
    path('api/analysis/corr/',    va.analysis_corr,    name='analysis-corr'),
    path('api/analysis/stats/',   va.analysis_stats,   name='analysis-stats'),
    path('api/analysis/insight/', va.analysis_insight, name='analysis-insight'),
    path('api/analysis/chat/',    va.analysis_chat,    name='analysis-chat'),
]
