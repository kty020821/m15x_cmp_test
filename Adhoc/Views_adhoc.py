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
  api/adhoc/run/            요청 실행 (웹에서 백그라운드로)
  api/adhoc/reset/          멈춘 '실행중' 요청을 대기로 되돌리기
  api/issue/context/        구간 지정 후보 (파라미터·LOT_ID·기간)
  api/issue/analyze/        이슈 구간 분석 — 파라미터 1건 상세
  api/issue/scan/           이슈 구간 전 파라미터 스캔 (어디에 변곡이 있었나)
  issue/report/             파라미터별 차트를 모은 한 파일 HTML 리포트
                            (브라우저에서 Ctrl+P 하면 PDF 로 저장된다)

  ※ 이슈 분석은 적재된 테이블이면 무엇이든 대상이 된다 —
    1회성 결과(ADHOC_*)도, 정기 적재분도 같은 API 를 쓴다.

  ※ 실행은 웹에서 백그라운드 스레드로 돈다 (Lake 가 웹에도 설치됨).
    대량 조회나 실패 건 일괄 재실행은 배치 러너(run_adhoc.py)도 쓸 수 있다.
    양쪽 다 claim_job 으로 선점하므로 중복 실행되지 않는다.
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback

from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from . import adhoc_service as ah
from . import issue_service as iss
from . import issue_report as rep


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
def adhoc_run(request):
    """
    요청을 웹에서 바로 실행한다.

    ★ 요청 안에서 조회를 끝내지 않는다 — 1년치는 몇 분 걸려
      게이트웨이 타임아웃에 걸린다. 백그라운드 스레드로 띄우고
      즉시 응답한 뒤, 화면이 목록을 폴링해 진행 상황을 보여준다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    job_id = _body(request).get('job_id')
    try:
        res = ah.run_job_async(int(job_id))
        if not res.get('ok'):
            return _fail(res.get('error', '실행 실패'))
        return JsonResponse({'ok': True, 'job_id': res['job_id']})
    except Exception as e:
        return _fail(f'실행 실패: {e}', exc=e)


@csrf_exempt
def adhoc_reset(request):
    """
    멈춘 '실행중' 요청을 대기로 되돌린다.
    워커가 재시작되면 스레드가 사라지는데 상태만 '실행중' 으로 남는다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        ids = ah.reset_stale()
        return JsonResponse({'ok': True, 'reset': ids})
    except Exception as e:
        return _fail(f'되돌리기 실패: {e}', {'reset': []}, exc=e)


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
def issue_scan(request):
    """
    지정한 이슈 구간에 대해 등록된 전 파라미터를 스캔한다.

    "이 구간에서 무엇이 변했나" 에 답하는 것이 목적 —
    파라미터를 하나씩 고를 필요 없이 변곡이 있었던 항목을 찾아 준다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    b = _body(request)
    oper_id = b.get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류', {'items': []})
    try:
        # params 를 주면 그 묶음만 — 화면이 나눠 호출해 진행률을 보여준다
        res = iss.scan_all(oper_id, b.get('lot_cd') or None,
                           b.get('sel') or {}, unit=b.get('unit') or 'lot',
                           params=b.get('params') or None)
        return JsonResponse({'ok': True, **res})
    except ValueError as e:
        return _fail(str(e), {'items': []})
    except Exception as e:
        return _fail(f'스캔 실패: {e}', {'items': []}, exc=e)


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


@csrf_exempt
def issue_report(request):
    """
    이슈 스캔 결과를 한 파일 HTML 리포트로 만든다.

    ★ JSON 이 아니라 HTML 문서를 그대로 돌려준다 —
      새 탭에서 열리고, 그 상태로 Ctrl+P 하면 PDF 가 된다.
      PDF 라이브러리(weasyprint 등)를 서버에 깔 필요가 없다.

    GET 으로도 받는다. 새 탭 열기(window.open)가 GET 이라 그렇다.
    """
    g = request.GET if request.method == 'GET' else _body(request)

    oper_id = g.get('oper_id')
    if not _safe(oper_id):
        return HttpResponse('<h3>oper_id 형식 오류</h3>', status=200,
                            content_type='text/html; charset=utf-8')

    sel = g.get('sel')
    if isinstance(sel, str):
        try:
            sel = json.loads(sel)
        except Exception:
            sel = {}
    sel = sel or {}

    params = g.get('params')
    if isinstance(params, str):
        params = [p for p in re.split(r'[,\s]+', params) if p]

    only = str(g.get('only_issue', '1')).lower() not in ('0', 'false', 'no')
    download = str(g.get('download', '')).lower() in ('1', 'true', 'yes')

    try:
        html = rep.build_report(
            oper_id, g.get('lot_cd') or None, sel,
            # ★ 기본값으로 덮지 않는다 — 없으면 build_report 가
            #   그 사실을 리포트에 표기하도록 그대로 넘긴다
            unit=g.get('unit'), params=params or None,
            only_issue=only, title=g.get('title') or '',
            points=g.get('points'))
    except ValueError as e:
        html = f'<h3>리포트를 만들지 못했습니다</h3><p>{e}</p>'
    except Exception as e:
        traceback.print_exc()
        html = f'<h3>리포트 생성 실패</h3><p>{e.__class__.__name__}: {e}</p>'

    resp = HttpResponse(html, content_type='text/html; charset=utf-8')
    if download:
        from datetime import datetime
        name = f'issue_report_{oper_id}_{datetime.now():%Y%m%d_%H%M}.html'
        resp['Content-Disposition'] = f'attachment; filename="{name}"'
    return resp
