"""
equipment/views_monitor.py
════════════════════════════════════════════════════════════
Inline Monitoring 페이지 + API

  화면
    · [점검 시작] → 공정 목록을 받아 한 공정씩 순차 호출
      (한 요청에 전 공정을 몰면 512MiB 웹서버에서 타임아웃 위험)
    · 결과 테이블 — 심각도순 정렬, 행마다 30일 추이 미니차트
    · 행 클릭 → 웨이퍼 단위 상세 차트

  API
    monitor/                 페이지
    api/monitor/opers/       점검 대상 공정 목록
    api/monitor/run/         공정 1건 점검 (POST oper_id)
    api/monitor/results/     저장된 최근 결과
    api/monitor/clear/       저장된 결과 초기화
    api/monitor/detail/      웨이퍼 상세 (POST oper_id, lot_cd, param)

  ※ 읽기 API 는 500 을 내지 않는다 — 200 + error 필드로 응답해
    화면이 팝업 없이 계속 동작하게 한다 (분석 페이지와 동일 방침)
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback

from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import connections

from . import tech_map
from . import monitor_service as ms
from . import param_types as pt


def _fail(msg, payload=None, exc=None):
    print(f'[monitor] {msg}')
    if exc is not None:
        traceback.print_exc()
    out = dict(payload or {})
    out['error'] = msg
    return JsonResponse(out, status=200)


def _safe(v):
    return bool(v) and bool(re.match(r'^[0-9A-Za-z_\-]+$', str(v)))


# ══════════════════════════════════════════════════════════
def monitor_page(request):
    # 타입 필터 항목은 param_types.py 에서 온다 (분류 규칙과 같은 소재지)
    return render(request, 'equipment/monitor.html',
                  {'type_options': pt.options()})


@csrf_exempt
def monitor_opers(request):
    """
    점검 대상 공정 = 등록된 OPER_ID 중 테이블이 실제로 있는 것.
    (분석 페이지와 같은 규칙 — 테이블명 역산 금지)
    """
    try:
        with connections['analysis_db'].cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                ['cmp_analysis_%'])
            tables = {r[0] for r in cur.fetchall()}

        names = {}
        try:
            names = tech_map.oper_names()
        except AttributeError:
            print('[monitor] tech_map.oper_names() 가 없습니다 — '
                  'tech_map.py 를 갱신하면 공정명이 표시됩니다 (동작은 계속)')
        except Exception as e:
            print(f'[monitor] 공정명 조회 실패: {e.__class__.__name__}: {e}')

        out = []
        for oid, desc in names.items():
            if ms._table(oid) in tables:
                out.append({'oper_id': oid,
                            'label': f'{desc} ({oid})' if desc else oid})
        if out:
            out.sort(key=lambda o: o['label'])
            return JsonResponse({'opers': out})

        # OPER_NAME_MAP 미작성 시 화면이 비지 않도록 폴백
        return JsonResponse({
            'opers': [{'oper_id': t.replace('cmp_analysis_', '').upper(),
                       'label':   t.replace('cmp_analysis_', '').upper()}
                      for t in sorted(tables)],
            'note': 'tech_map.OPER_NAME_MAP 미등록 — 공정명 없이 표시합니다',
        })
    except Exception as e:
        return _fail(f'공정 목록 조회 실패: {e}', {'opers': []}, exc=e)


@csrf_exempt
def monitor_run(request):
    """공정 1건 점검"""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body)
    except Exception:
        return _fail('요청 형식 오류', {'results': []})

    oper_id = body.get('oper_id')
    label   = body.get('label', '')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류', {'results': []})

    try:
        return JsonResponse(ms.run_check(oper_id, label))
    except Exception as e:
        return _fail(f'{oper_id} 점검 실패: {e}',
                     {'oper_id': oper_id, 'results': []}, exc=e)


@csrf_exempt
def monitor_results(request):
    """저장된 최근 점검 결과"""
    try:
        return JsonResponse(ms.load_results())
    except Exception as e:
        return _fail(f'결과 조회 실패: {e}',
                     {'run_ts': None, 'results': []}, exc=e)


@csrf_exempt
def monitor_clear(request):
    """
    저장된 점검 결과 초기화.

    점검 대상 규칙을 바꾼 뒤에는 옛 결과가 남아 혼동을 주므로,
    화면에서 비우고 다시 점검할 수 있게 한다.
    연속일수 이력(cmp_monitor_history)은 남긴다 — 지우면 '며칠 연속'
    정보가 사라진다. 이력까지 지우려면 shell 에서
    monitor_service.clear_results(with_history=True) 를 쓴다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}

    oper_id = body.get('oper_id')
    if oper_id and not _safe(oper_id):
        return _fail('oper_id 형식 오류', {'deleted': 0})

    try:
        n = ms.clear_results(oper_id or None)
        return JsonResponse({'deleted': n})
    except Exception as e:
        return _fail(f'초기화 실패: {e}', {'deleted': 0}, exc=e)


@csrf_exempt
def monitor_detail(request):
    """웨이퍼 단위 상세"""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body)
    except Exception:
        return _fail('요청 형식 오류', {'points': []})

    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    param   = body.get('param')
    if not (_safe(oper_id) and _safe(param)):
        return _fail('요청 값 형식 오류', {'points': []})

    try:
        return JsonResponse(ms.wafer_detail(oper_id, lot_cd, param))
    except Exception as e:
        return _fail(f'상세 조회 실패: {e}', {'points': []}, exc=e)
