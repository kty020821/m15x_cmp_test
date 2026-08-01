"""
equipment/views_adhoc.py
════════════════════════════════════════════════════════════
1회성 임의 기간 조회 페이지 + API

  adhoc/                    페이지
  api/adhoc/submit/         조회 요청 등록
  api/adhoc/list/           요청 목록·상태 (화면이 주기적으로 갱신)
  api/adhoc/delete/         요청·결과 삭제
  api/adhoc/prefill/        기준정보에서 조회 조건 초안 채우기
  api/adhoc/opers/          기준정보에 등록된 공정 목록
  api/issue/context/        구간 지정 후보 (파라미터·LOT_ID·기간)
  api/issue/analyze/        이슈 구간 분석 (판정·변곡점·랏별)

  ※ 이슈 분석은 적재된 테이블이면 무엇이든 대상이 된다 —
    1회성 결과(ADHOC_*)도, 정기 적재분도 같은 API 를 쓴다.

  ※ 실행은 배치 서버의 run_adhoc.py 가 한다 — 웹은 Lake 를 못 읽는다.
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback

from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import adhoc_service as ah
from . import issue_service as iss


def _fail(msg, payload=None, exc=None):
    print(f'[adhoc] {msg}')
    if exc is not None:
        traceback.print_exc()
    out = dict(payload or {})
    out['ok'] = False
    out['error'] = msg
    return JsonResponse(out, status=200)


def _body(request):
    try:
        return json.loads(request.body) if request.body else {}
    except Exception:
        return {}


def _safe(v):
    return bool(v) and bool(re.match(r'^[0-9A-Za-z_\-]+$', str(v)))


def adhoc_page(request):
    return render(request, 'equipment/adhoc.html', {
        'max_range_days': ah.MAX_RANGE_DAYS,
        'keep_days': ah.KEEP_DAYS,
    })


@csrf_exempt
def adhoc_opers(request):
    """조회 조건을 채워 넣을 수 있는 등록 공정 목록"""
    try:
        from . import config_service as cfg
        opers = [{'oper_id': o['oper_id'],
                  'label': f"{o['oper_desc']} ({o['oper_id']})"
                           if o['oper_desc'] else o['oper_id']}
                 for o in cfg.list_opers()]
        return JsonResponse({'ok': True, 'opers': opers})
    except Exception as e:
        return _fail(f'공정 목록 조회 실패: {e}', {'opers': []}, exc=e)


@csrf_exempt
def adhoc_prefill(request):
    """
    기준정보에서 조회 조건 초안을 가져온다.
    파라미터 100개를 매번 손으로 넣을 수 없으므로, 등록 공정을 고르면
    채워 놓고 날짜·사전공정만 바꿔서 요청하게 한다.
    """
    oper_id = _body(request).get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류')
    try:
        d = ah.prefill(oper_id)
        if not d:
            return _fail(f'{oper_id} 기준정보를 찾을 수 없습니다')
        return JsonResponse({'ok': True, 'cond': d})
    except Exception as e:
        return _fail(f'조건 조회 실패: {e}', exc=e)


@csrf_exempt
def adhoc_submit(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        res = ah.submit(_body(request), user=_body(request).get('user', ''))
        return JsonResponse({'ok': True, **res})
    except ValueError as e:
        return _fail(str(e))                      # 입력 오류는 그대로 보여준다
    except Exception as e:
        return _fail(f'요청 등록 실패: {e}', exc=e)


@csrf_exempt
def adhoc_list(request):
    try:
        return JsonResponse({'ok': True, 'jobs': ah.list_jobs()})
    except Exception as e:
        return _fail(f'목록 조회 실패: {e}', {'jobs': []}, exc=e)


@csrf_exempt
def adhoc_delete(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    job_id = _body(request).get('job_id')
    try:
        ah.delete_job(int(job_id))
        return JsonResponse({'ok': True})
    except Exception as e:
        return _fail(f'삭제 실패: {e}', exc=e)


# ══════════════════════════════════════════════════════════
# 이슈 구간 분석
# ══════════════════════════════════════════════════════════
@csrf_exempt
def issue_context(request):
    """구간을 지정할 수 있게 후보(파라미터·LOT_ID·기간)를 준다"""
    b = _body(request)
    oper_id = b.get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류', {'params': [], 'lot_ids': []})
    try:
        res = iss.context(oper_id, b.get('lot_cd') or None)
        if not res.get('ok'):
            return _fail(res.get('error', '조회 실패'),
                         {'params': [], 'lot_ids': []})
        return JsonResponse(res)
    except Exception as e:
        return _fail(f'조회 실패: {e}', {'params': [], 'lot_ids': []}, exc=e)


@csrf_exempt
def issue_analyze(request):
    """
    이슈 구간 분석.
      sel.mode = range(기간) / lots(랏) / wafers(웨이퍼 id)
      unit     = wafer(세밀) / lot(랏 평균 — 큰 흐름)
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    b = _body(request)
    oper_id = b.get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류')
    try:
        res = iss.analyze(oper_id, b.get('lot_cd') or None,
                          b.get('param'), b.get('sel') or {},
                          unit=b.get('unit') or 'wafer')
        return JsonResponse({'ok': True, **res})
    except ValueError as e:
        return _fail(str(e))                 # 입력 오류는 그대로 안내
    except Exception as e:
        return _fail(f'분석 실패: {e}', exc=e)
