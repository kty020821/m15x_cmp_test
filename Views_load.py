"""
equipment/views_load.py
════════════════════════════════════════════════════════════
DB 만들기(적재) API — 모니터링·분석 화면 공용

  api/load/status/    공정별 적재 상태 (실행중 여부·마지막 적재·데이터 최신일)
  api/load/run/       적재 실행 (백그라운드)
  api/load/reset/     멈춘 '실행중' 잠금 해제
  api/load/history/   최근 적재 이력

  ★ 실행 전에 status 를 먼저 보게 한다 —
    누가 방금 돌렸는데 또 돌리는 일과, 같은 공정을 동시에 돌려
    테이블이 섞이는 사고를 막기 위한 것.
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import load_service as ls


def _fail(msg, payload=None, exc=None):
    print(f'[load] {msg}')
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


def _opers():
    """등록된 공정 목록 (사용 중인 것만)"""
    from . import config_service as cfg
    return [o for o in cfg.list_opers()
            if str(o.get('use_yn') or 'Y').upper() != 'N']


@csrf_exempt
def load_status(request):
    """
    공정별 적재 상태.
    화면은 이걸 보고 버튼을 열지, 경고를 띄울지 정한다.
    """
    try:
        opers = _opers()
        ids = [o['oper_id'] for o in opers]
        st = ls.status(ids)

        rows = []
        for o in opers:
            d = st.get(o['oper_id'], {})
            rows.append({
                'oper_id': o['oper_id'],
                'oper_desc': o.get('oper_desc') or '',
                'n_lot': o.get('n_lot', 0), 'n_param': o.get('n_param', 0),
                'n_resp': o.get('n_resp', 0), 'n_defect': o.get('n_defect', 0),
                'loaded': d.get('loaded', False),
                'rows': d.get('rows', 0),
                'data_max': d.get('data_max'),
                'last_at': d.get('last_at', ''),
                'last_by': d.get('last_by', ''),
                'last_status': d.get('last_status', ''),
                'running': d.get('running'),
            })
        return JsonResponse({'ok': True, 'opers': rows,
                             'running': ls.any_running(),
                             'default_days': ls.DEFAULT_DAYS})
    except Exception as e:
        return _fail(f'상태 조회 실패: {e}', {'opers': []}, exc=e)


@csrf_exempt
def load_run(request):
    """
    적재 실행 (백그라운드).

      opers        공정 목록. 비우면 등록된 전 공정
      days         최근 며칠 (첫 적재에만 적용, 기본 45)
      incremental  마지막 적재일부터 지금까지만 (매일 돌리는 배치용)
      date_from/date_to  기간을 직접 지정할 때

    ★ 요청 안에서 끝내지 않는다 — 공정 하나에 몇 분씩 걸린다.
      스레드로 띄우고 화면이 status 를 폴링한다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    b = _body(request)

    ids = [str(o).upper().strip() for o in (b.get('opers') or [])
           if str(o).strip()]
    if not ids:
        ids = [o['oper_id'] for o in _opers()]
    bad = [i for i in ids if not _safe(i)]
    if bad:
        return _fail(f'공정 ID 형식 오류: {", ".join(bad[:3])}')
    if not ids:
        return _fail('등록된 공정이 없습니다. 기준정보를 먼저 등록하세요.')

    days = b.get('days') or ls.DEFAULT_DAYS
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = ls.DEFAULT_DAYS

    # 이미 돌고 있는 공정은 미리 알려 준다 (실행은 건너뛴다)
    st = ls.status(ids)
    busy = [{'oper_id': i, **(st.get(i, {}).get('running') or {})}
            for i in ids if st.get(i, {}).get('running')]

    # 기간 직접 지정은 공정 하나일 때만 의미가 있다
    #   (여러 공정을 같은 기간으로 돌릴 일은 드물고, 실수로 전 공정을
    #    긴 기간으로 돌리면 메모리·시간이 크게 든다)
    date_from = str(b.get('date_from') or '').strip() or None
    date_to   = str(b.get('date_to') or '').strip() or None
    if (date_from or date_to) and len(ids) > 1:
        return _fail('기간을 직접 지정할 때는 공정을 하나만 선택하세요')

    # 증분 — 마지막 적재일부터만. 기간을 직접 지정하면 그쪽이 우선한다.
    incremental = bool(b.get('incremental')) and not (date_from or date_to)

    try:
        res = ls.run_async(ids, days=days, user=b.get('user', ''),
                           date_from=date_from, date_to=date_to,
                           incremental=incremental)
        if not res.get('ok'):
            return _fail(res.get('error', '실행 실패'))
        return JsonResponse({'ok': True, 'opers': res['opers'],
                             'days': days, 'busy': busy,
                             'incremental': incremental,
                             'date_from': date_from, 'date_to': date_to})
    except Exception as e:
        return _fail(f'적재 실행 실패: {e}', exc=e)


@csrf_exempt
def load_reset(request):
    """워커가 죽어 '실행중' 으로 굳은 잠금 해제"""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        return JsonResponse({'ok': True, 'reset': ls.reset_stale()})
    except Exception as e:
        return _fail(f'잠금 해제 실패: {e}', {'reset': []}, exc=e)


@csrf_exempt
def load_history(request):
    try:
        return JsonResponse({'ok': True, 'jobs': ls.history()})
    except Exception as e:
        return _fail(f'이력 조회 실패: {e}', {'jobs': []}, exc=e)


@csrf_exempt
def load_refresh_max(request):
    """
    최신 데이터 날짜 다시 재기 — 적재 테이블을 실제로 훑는다.

    ★ 무거우므로 버튼을 눌렀을 때만 돈다.
      평소 화면은 적재할 때 기록해 둔 값을 읽는다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    b = _body(request)
    try:
        ids = b.get('opers') or [o['oper_id'] for o in _opers()]
        return JsonResponse({'ok': True, 'data_max': ls.refresh_data_max(ids)})
    except Exception as e:
        return _fail(f'최신일 조회 실패: {e}', exc=e)


@csrf_exempt
def load_queue(request):
    """
    적재 큐 상태 — 어느 화면에서든 진행 상황을 볼 수 있다.

    ★ 적재는 요청과 수명이 분리된 워커가 처리하므로,
      요청한 사람이 페이지를 떠나도 계속 돌아간다.
    """
    try:
        return JsonResponse({'ok': True, **ls.queue_status()})
    except Exception as e:
        return _fail(f'큐 조회 실패: {e}', {'items': []}, exc=e)


@csrf_exempt
def load_cancel(request):
    """대기 중인 작업 취소 (실행 중인 것은 멈출 수 없다)"""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    b = _body(request)
    try:
        done = ls.cancel(oper_id=b.get('oper_id'),
                         all_waiting=bool(b.get('all')))
        return JsonResponse({'ok': True, 'canceled': done})
    except Exception as e:
        return _fail(f'취소 실패: {e}', exc=e)


@csrf_exempt
def load_schedule(request):
    """
    정기 적재 예약 조회·저장.

    ★ 하루 1회다. 켜 두면 워커가 시각을 확인해 전 공정을 큐에 넣는다.
      사내 스케줄러와 병행해도 큐가 중복을 막는다.
    """
    if request.method != 'POST':
        try:
            return JsonResponse({'ok': True, 'schedule': ls.get_schedule()})
        except Exception as e:
            return _fail(f'예약 조회 실패: {e}', exc=e)

    b = _body(request)
    try:
        if b.get('save'):
            s = ls.save_schedule(b, user=b.get('user', ''))
            return JsonResponse({'ok': True, 'schedule': s})
        return JsonResponse({'ok': True, 'schedule': ls.get_schedule()})
    except Exception as e:
        return _fail(f'예약 저장 실패: {e}', exc=e)
