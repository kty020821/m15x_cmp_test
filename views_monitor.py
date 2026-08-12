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
    api/monitor/diag/        적재·점검 상태 진단 (shell 대신 화면에서 확인)
    monitor/report/          점검 결과를 한 파일 HTML 리포트로
                             (브라우저에서 Ctrl+P 하면 PDF 로 저장된다)
    api/monitor/detail/      웨이퍼 상세 (POST oper_id, lot_cd, param)

  ※ 읽기 API 는 500 을 내지 않는다 — 200 + error 필드로 응답해
    화면이 팝업 없이 계속 동작하게 한다 (분석 페이지와 동일 방침)
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback

from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
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
    점검 대상 공정 = 등록된 OPER_ID 중 적재 테이블이 실제로 있는 것.

    ★ 1순위는 기준정보(config_service.list_opers)다.
      기준정보를 자체 DB 로 옮긴 뒤에도 여기가 tech_map.OPER_NAME_MAP 만
      보고 있어서, 기준정보에 8개를 등록해도 tech_map 에 5개만 있으면
      5개만 점검되는 문제가 있었다. 등록 창구가 하나면 목록도 하나여야 한다.

    ★ 2순위 tech_map, 3순위 적재 테이블 그대로 — 어느 단계에서도
      목록이 비어 화면이 멈추지 않게 한다.
      (테이블명에서 OPER_ID 를 역산하는 건 마지막 폴백에서만.
       특수문자가 '_' 로 바뀌어 원래 ID 로 돌아오지 않는다)
    """
    try:
        with connections['analysis_db'].cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                ['cmp_analysis_%'])
            tables = {r[0] for r in cur.fetchall()}

        # ── 1순위: 기준정보 ──────────────────────────────
        out, source = [], ''
        try:
            from . import config_service as cs
            for o in cs.list_opers():
                if str(o.get('use_yn') or 'Y').upper() == 'N':
                    continue                       # 미사용 공정은 제외
                oid = o['oper_id']
                if ms._table(oid) in tables:
                    desc = o.get('oper_desc') or ''
                    out.append({'oper_id': oid,
                                'label': f'{desc} ({oid})' if desc else oid})
            if out:
                source = '기준정보'
        except Exception as e:
            print(f'[monitor] 기준정보 공정 목록 조회 실패: '
                  f'{e.__class__.__name__}: {e}')

        # ── 2순위: tech_map ──────────────────────────────
        if not out:
            names = {}
            try:
                names = tech_map.oper_names()
            except AttributeError:
                print('[monitor] tech_map.oper_names() 가 없습니다 (동작은 계속)')
            except Exception as e:
                print(f'[monitor] 공정명 조회 실패: {e.__class__.__name__}: {e}')
            for oid, desc in names.items():
                if ms._table(oid) in tables:
                    out.append({'oper_id': oid,
                                'label': f'{desc} ({oid})' if desc else oid})
            if out:
                source = 'tech_map'

        if out:
            out.sort(key=lambda o: o['label'])
            res = {'opers': out, 'source': source}
            # 등록에 없는데 테이블만 남은 것 — 점검에서 빠지므로 알려준다
            known = {ms._table(o['oper_id']) for o in out}
            orphan = sorted(t for t in tables if t not in known)
            if orphan:
                res['note'] = (f'{source} 에 없어 점검에서 제외된 테이블 '
                               f'{len(orphan)}개: {", ".join(orphan[:8])}')
            return JsonResponse(res)

        # ── 3순위: 적재 테이블 그대로 ────────────────────
        return JsonResponse({
            'opers': [{'oper_id': t.replace('cmp_analysis_', '').upper(),
                       'label':   t.replace('cmp_analysis_', '').upper()}
                      for t in sorted(tables)],
            'source': '적재테이블',
            'note': '기준정보에 등록된 공정이 없어 적재 테이블을 그대로 표시합니다',
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
def monitor_diag(request):
    """
    적재·점검 상태 진단.
    테이블 존재 / 행수 / 최근 DATE / 점검 대상 수 / 선정 방식을 한 번에 본다.
    """
    try:
        return JsonResponse(dict(ms.diagnose(), ok=True))
    except Exception as e:
        return _fail(f'진단 실패: {e}', {'items': [], 'orphans': []}, exc=e)


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


@csrf_exempt
def monitor_report(request):
    """
    점검 결과 리포트 (HTML 문서를 그대로 반환).

    ★ 점검을 다시 돌리지 않는다 — 저장된 최근 결과를 문서로 옮긴다.
      다시 돌리면 화면에서 본 것과 내용이 달라진다.
    """
    from datetime import datetime
    from . import monitor_report as mr

    g = request.GET if request.method == 'GET' else _body(request)
    only = str(g.get('only_issue', '1')).lower() not in ('0', 'false', 'no')
    download = str(g.get('download', '')).lower() in ('1', 'true', 'yes')
    oper_id = g.get('oper_id') or None

    try:
        html = mr.build_report(only_issue=only, oper_id=oper_id,
                               title=g.get('title') or '')
    except Exception as e:
        traceback.print_exc()
        html = f'<h3>리포트 생성 실패</h3><p>{e.__class__.__name__}: {e}</p>'

    resp = HttpResponse(html, content_type='text/html; charset=utf-8')
    if download:
        name = f'monitor_report_{datetime.now():%Y%m%d_%H%M}.html'
        resp['Content-Disposition'] = f'attachment; filename="{name}"'
    return resp
