"""
equipment/urls.py
════════════════════════════════════════════════════════════
CMP 산포 분석 플랫폼 — URL 경로

  ★ name= 값은 템플릿의 {% url %} 과 정확히 일치해야 한다.
    하나라도 어긋나면 그 페이지가 통째로 렌더에 실패한다
    ("Reverse for '...' not found") — 화면이 안 뜨는 원인 1순위다.

  ★ 맨 위 '기본 페이지' 구역은 기존 views.py 의 경로다.
    지금 쓰고 있는 것을 그대로 두고, 아래 구역만 맞추면 된다.

  ★ config2(기준정보 v2)는 폐기됐다. config 하나로 통합했으므로
    config2 경로와 views_config2.py 는 함께 지운다 —
    한쪽만 지우면 import 에러로 서버가 안 뜬다.
════════════════════════════════════════════════════════════
"""

from django.urls import path

from . import views                    # 기존 페이지 (home·equipment·wip 등)
from . import views_config as vc       # 기준정보 (통합)
from . import views_analysis as va     # 산포 분석 (기존)
from . import views_analysis2 as a2    # 통합 분석
from . import views_insight as ai      # 분석 AI
from . import views_adhoc as vh        # 1회성 조회 · 이슈 분석
from . import views_load as vl         # DB 적재
from . import views_monitor as vm      # Inline Monitoring

urlpatterns = [

    # ── 기본 페이지 (기존 views.py) ──────────────────────
    #   지금 쓰고 있는 경로를 그대로 두세요.
    path('',           views.home,           name='home'),
    path('equipment/', views.equipment_page, name='equipment'),
    path('wip/',       views.wip_page,       name='wip'),
    path('rtd/',       views.rtd_page,       name='rtd'),
    path('llm/',       views.llm_page,       name='llm'),
    path('api/llm/',   views.llm_api,        name='llm-api'),
    path('api/equipment/', views.equipment_api, name='equipment-api'),
    path('capa/',          views.capa_page,     name='capa'),
    path('capa/settings/', views.capa_settings, name='capa-settings'),
    path('api/capa/save/',       views.capa_save_api,    name='capa-save'),
    path('api/capa/update-row/', views.capa_update_row,  name='capa-update-row'),
    path('api/capa/delete/',     views.capa_delete_api,  name='capa-delete'),
    path('api/capa/update-tat/', views.capa_update_tat,  name='capa-update-tat'),
    path('api/capa/update-eq/',  views.capa_update_eq,   name='capa-update-eq'),
    path('api/capa/delete-eq/',  views.capa_delete_eq,   name='capa-delete-eq'),

    # ── 기준정보 ────────────────────────────────────────
    #   공정·Device·파라미터·Response·Defect·연계 공정
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

    # ── 산포 분석 (기존 화면) ───────────────────────────
    path('analysis/',             va.analysis_page,    name='analysis'),
    path('api/analysis/refresh/', va.analysis_refresh, name='analysis-refresh'),
    path('api/analysis/options/', va.analysis_options, name='analysis-options'),
    path('api/analysis/trend/',   va.analysis_trend,   name='analysis-trend'),
    path('api/analysis/corr/',    va.analysis_corr,    name='analysis-corr'),
    path('api/analysis/stats/',   va.analysis_stats,   name='analysis-stats'),
    path('api/analysis/insight/', va.analysis_insight, name='analysis-insight'),
    path('api/analysis/chat/',    va.analysis_chat,    name='analysis-chat'),

    # ── 통합 분석 + 분석 AI ─────────────────────────────
    path('analysis2/',        a2.analysis2_page, name='analysis2'),
    path('api/an2/sources/',  a2.an2_sources,    name='an2-sources'),
    path('api/an2/load/',     a2.an2_load,       name='an2-load'),
    path('api/an2/chart/',    a2.an2_chart,      name='an2-chart'),
    path('api/an2/lots/',     a2.an2_lots,       name='an2-lots'),
    path('api/an2/ask/',      ai.an2_ask,        name='an2-ask'),
    path('api/an2/run/',      ai.an2_run,        name='an2-run'),
    path('api/an2/llm/',      ai.an2_llm,        name='an2-llm'),
    path('api/an2/llmcheck/', ai.an2_llm_check,  name='an2-llm-check'),

    # ── 1회성 조회 · 이슈 분석 ──────────────────────────
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

    # ── DB 적재 (모니터링 화면에서 조작) ────────────────
    #   queue·cancel·schedule 은 작업 큐 구조로 바꾸면서 추가됐다.
    #   이 셋이 없으면 monitor.html 이 렌더에 실패한다.
    path('api/load/status/',   vl.load_status,   name='load-status'),
    path('api/load/run/',      vl.load_run,      name='load-run'),
    path('api/load/reset/',    vl.load_reset,    name='load-reset'),
    path('api/load/history/',  vl.load_history,  name='load-history'),
    path('api/load/queue/',    vl.load_queue,    name='load-queue'),
    path('api/load/refreshmax/', vl.load_refresh_max, name='load-refresh-max'),
    path('api/load/cancel/',   vl.load_cancel,   name='load-cancel'),
    path('api/load/schedule/', vl.load_schedule, name='load-schedule'),

    # ── Inline Monitoring ───────────────────────────────
    path('monitor/',             vm.monitor_page,    name='monitor'),
    path('api/monitor/opers/',   vm.monitor_opers,   name='monitor-opers'),
    path('api/monitor/run/',     vm.monitor_run,     name='monitor-run'),
    path('api/monitor/results/', vm.monitor_results, name='monitor-results'),
    path('api/monitor/clear/',   vm.monitor_clear,   name='monitor-clear'),
    path('api/monitor/diag/',    vm.monitor_diag,    name='monitor-diag'),
    path('monitor/report/',      vm.monitor_report,  name='monitor-report'),
    path('api/monitor/detail/',  vm.monitor_detail,  name='monitor-detail'),
]
