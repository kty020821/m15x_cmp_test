"""
equipment/views_config2.py
════════════════════════════════════════════════════════════
기준정보 v2 API — 연계 공정 다중 등록

  config2/                    페이지
  api/config2/opers/          공정 목록
  api/config2/oper/           공정 1건
  api/config2/save/           저장
  api/config2/delete/         삭제
  api/config2/overview/       전체 셋업 현황
  api/config2/validate/       검증
  api/config2/import/         기존 기준정보(v1)에서 가져오기
  api/config2/classify/       파라미터 이름 → 타입 자동 분류
  api/config2/suggest/        적재 테이블에서 파라미터 후보
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback

from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import connections

from . import config2_service as cs
from . import param_types as pt


def _fail(msg, payload=None, exc=None):
    print(f'[config2] {msg}')
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


def config2_page(request):
    return render(request, 'equipment/config2.html', {
        'type_options': pt.options(),
        'kinds':  [(k, cs.KIND_LABEL[k]) for k in cs.KINDS],
        'scopes': [(s, cs.SCOPE_LABEL[s]) for s in cs.SCOPES],
        'max_links': cs.MAX_LINKS,
    })


@csrf_exempt
def config2_opers(request):
    try:
        return JsonResponse({'ok': True, 'opers': cs.list_opers()})
    except Exception as e:
        return _fail(f'목록 조회 실패: {e}', {'opers': []}, exc=e)


@csrf_exempt
def config2_oper(request):
    oper_id = _body(request).get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류')
    try:
        d = cs.get_oper(oper_id)
        if not d:
            return _fail(f'{oper_id} 를 찾을 수 없습니다')
        return JsonResponse({'ok': True, 'oper': d})
    except Exception as e:
        return _fail(f'조회 실패: {e}', exc=e)


@csrf_exempt
def config2_save(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    d = _body(request)
    try:
        return JsonResponse({'ok': True,
                             'saved': cs.save_oper(d, user=d.get('user', ''))})
    except ValueError as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f'저장 실패: {e}', exc=e)


@csrf_exempt
def config2_delete(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    oper_id = _body(request).get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류')
    try:
        cs.delete_oper(oper_id)
        return JsonResponse({'ok': True})
    except Exception as e:
        return _fail(f'삭제 실패: {e}', exc=e)


@csrf_exempt
def config2_overview(request):
    """전체 셋업 현황 — 공정별 연계 구성과 만들어질 컬럼 수"""
    try:
        return JsonResponse({'ok': True, **cs.overview()})
    except Exception as e:
        return _fail(f'현황 조회 실패: {e}', {'rows': []}, exc=e)


@csrf_exempt
def config2_validate(request):
    oper_id = _body(request).get('oper_id')
    try:
        return JsonResponse({'ok': True,
                             'issues': cs.validate(oper_id or None)})
    except Exception as e:
        return _fail(f'검증 실패: {e}', {'issues': []}, exc=e)


@csrf_exempt
def config2_import(request):
    """
    기존 기준정보(cmp_cfg_*)에서 가져온다.
    사전공정 → SRC, Response → REP, Defect → DEF 로 옮겨진다.
    기존 데이터는 읽기만 하고 건드리지 않는다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    b = _body(request)
    try:
        res = cs.import_from_v1(oper_id=b.get('oper_id') or None,
                                overwrite=bool(b.get('overwrite')))
        return JsonResponse({'ok': True, **res})
    except Exception as e:
        return _fail(f'가져오기 실패: {e}', {'imported': []}, exc=e)


@csrf_exempt
def config2_classify(request):
    """파라미터 이름 → 타입 (서버 규칙으로 판정해 화면과 어긋나지 않게)"""
    names = _body(request).get('params') or []
    try:
        return JsonResponse({'ok': True,
                             'types': {p: pt.classify(p) for p in names}})
    except Exception as e:
        return _fail(f'분류 실패: {e}', {'types': {}}, exc=e)


@csrf_exempt
def config2_suggest(request):
    """
    적재 테이블에서 파라미터 후보를 읽어 온다.
    본공정(oper_id) 또는 연계 공정(link_id) 어느 쪽이든 조회할 수 있다.
    """
    target = _body(request).get('oper_id')
    if not _safe(target):
        return _fail('oper_id 형식 오류', {'params': []})
    table = f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(target)).lower()}"
    meta = {'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
            'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
            'WF_ID', 'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY',
            'REWORK_N', 'MEAS_N'}
    try:
        with connections['analysis_db'].cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
            if not cur.fetchone()[0]:
                return JsonResponse({
                    'ok': True, 'params': [],
                    'note': f'{table} 이 없습니다 — 아직 적재되지 않은 공정입니다'})
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s ORDER BY ordinal_position
            """, [table])
            cols = [c.upper() for (c,) in cur.fetchall()]
        out = [c for c in cols if c not in meta
               and not c.endswith(('_OFFSET', '_FORMULA'))]
        return JsonResponse({'ok': True, 'params': out})
    except Exception as e:
        return _fail(f'후보 조회 실패: {e}', {'params': []}, exc=e)
