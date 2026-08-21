"""
equipment/urls.py 에 추가할 경로 4개
────────────────────────────────────────────────────────
name= 값은 monitor.html 의 url 태그와 정확히 일치해야 한다.
"""

from django.urls import path
from . import views_analysis as va
from . import views_monitor as vm
from . import views_config as vc
from . import views_config2 as v2
from . import views_analysis2 as a2
from . import views_insight as ai
from . import views_adhoc as vh
from . import views_load as vl

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
    path('api/analysis/refresh/', va.analysis_refresh, name='analysis-refresh'),
    path('api/analysis/options/', va.analysis_options, name='analysis-options'),
    path('api/analysis/trend/',   va.analysis_trend,   name='analysis-trend'),
    path('api/analysis/corr/',    va.analysis_corr,    name='analysis-corr'),
    path('api/analysis/stats/',   va.analysis_stats,   name='analysis-stats'),
    path('api/analysis/insight/', va.analysis_insight, name='analysis-insight'),
    path('api/analysis/chat/',    va.analysis_chat,    name='analysis-chat'),

    # 통합 분석 페이지 (기존 analysis 와 병행)
    path('analysis2/',        a2.analysis2_page, name='analysis2'),
    path('api/an2/sources/',  a2.an2_sources,    name='an2-sources'),
    path('api/an2/load/',     a2.an2_load,       name='an2-load'),
    path('api/an2/chart/',    a2.an2_chart,      name='an2-chart'),
    path('api/an2/lots/',     a2.an2_lots,       name='an2-lots'),
    path('api/an2/ask/',      ai.an2_ask,        name='an2-ask'),
    path('api/an2/run/',      ai.an2_run,        name='an2-run'),
    path('api/an2/llm/',      ai.an2_llm,        name='an2-llm'),

    # 기준정보 v2 — 연계 공정 다중 등록 (기존 config 와 병행)
    path('config2/',              v2.config2_page,     name='config2'),
    path('api/config2/opers/',    v2.config2_opers,    name='config2-opers'),
    path('api/config2/oper/',     v2.config2_oper,     name='config2-oper'),
    path('api/config2/save/',     v2.config2_save,     name='config2-save'),
    path('api/config2/delete/',   v2.config2_delete,   name='config2-delete'),
    path('api/config2/overview/', v2.config2_overview, name='config2-overview'),
    path('api/config2/validate/', v2.config2_validate, name='config2-validate'),
    path('api/config2/import/',   v2.config2_import,   name='config2-import'),
    path('api/config2/classify/', v2.config2_classify, name='config2-classify'),
    path('api/config2/suggest/',  v2.config2_suggest,  name='config2-suggest'),

    # 1회성 임의 기간 조회 (신규) — 실행은 배치 run_adhoc.py
    path('adhoc/',             vh.adhoc_page,    name='adhoc'),
    path('api/adhoc/submit/',  vh.adhoc_submit,  name='adhoc-submit'),
    path('api/adhoc/list/',    vh.adhoc_list,    name='adhoc-list'),
    path('api/adhoc/delete/',  vh.adhoc_delete,  name='adhoc-delete'),
    path('api/adhoc/prefill/', vh.adhoc_prefill, name='adhoc-prefill'),
    path('api/adhoc/opers/',   vh.adhoc_opers,   name='adhoc-opers'),
    path('api/adhoc/run/',     vh.adhoc_run,     name='adhoc-run'),
    path('api/adhoc/reset/',   vh.adhoc_reset,   name='adhoc-reset'),
    path('api/issue/context/', vh.issue_context, name='issue-context'),
    path('api/issue/analyze/', vh.issue_analyze, name='issue-analyze'),
    path('api/issue/scan/',    vh.issue_scan,    name='issue-scan'),
    path('issue/report/',      vh.issue_report,  name='issue-report'),

    # DB 만들기(적재) — 모니터링·분석 화면 공용
    path('api/load/status/',   vl.load_status,   name='load-status'),
    path('api/load/run/',      vl.load_run,      name='load-run'),
    path('api/load/reset/',    vl.load_reset,    name='load-reset'),
    path('api/load/history/',  vl.load_history,  name='load-history'),

    # Inline Monitoring (신규)
    path('monitor/',             vm.monitor_page,    name='monitor'),
    path('api/monitor/opers/',   vm.monitor_opers,   name='monitor-opers'),
    path('api/monitor/run/',     vm.monitor_run,     name='monitor-run'),
    path('api/monitor/results/', vm.monitor_results, name='monitor-results'),
    path('api/monitor/clear/',   vm.monitor_clear,   name='monitor-clear'),
    path('api/monitor/diag/',    vm.monitor_diag,    name='monitor-diag'),
    path('monitor/report/',      vm.monitor_report,  name='monitor-report'),
    path('api/monitor/detail/',  vm.monitor_detail,  name='monitor-detail'),
    
    # ★ 추가: DB 백그라운드 구축 및 상태 조회 API
    path('api/monitor/build_db/', vm.monitor_build_db, name='monitor-build-db'),
    path('api/monitor/build_status/', vm.monitor_build_status, name='monitor-build-status'),
]
