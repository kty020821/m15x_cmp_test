"""
equipment/views_config.py
════════════════════════════════════════════════════════════
기준정보 셋업 페이지 + API

  구닥스가 불안정해 기준정보를 자체 DB 로 옮겼다.
  이 페이지가 유일한 수정 창구다.

  API
    config/                     페이지
    api/config/opers/           공정 목록
    api/config/oper/            공정 1건 조회 (POST oper_id)
    api/config/save/            공정 저장 (POST 전체 구성)
    api/config/delete/          공정 삭제
    api/config/import/          구닥스에서 가져오기
    api/config/validate/        적재 데이터와 대조 검증
    api/config/suggest/         적재 테이블의 숫자 컬럼을 후보로 제안
    api/config/suggest-lot/     적재 테이블의 LOT_CD 를 후보로 제안
    api/config/classify/        파라미터 이름 → 타입 자동 분류

  ※ 조회 API 는 200 + error 필드로 응답한다(화면이 안 죽게).
    저장/삭제는 결과를 화면에 명확히 보여줘야 하므로 성공 여부를
    ok 필드로 돌려준다.
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback

from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import config_service as cs
from . import param_types as pt


def _fail(msg, payload=None, exc=None):
    print(f'[config] {msg}')
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


def config_page(request):
    # 타입 목록·표시명은 param_types.py 가 단일 소재지다
    return render(request, 'equipment/config.html', {
        'param_types': pt.TYPES,
        'type_options': pt.options(),
    })


@csrf_exempt
def config_opers(request):
    try:
        return JsonResponse({'ok': True, 'opers': cs.list_opers()})
    except Exception as e:
        return _fail(f'목록 조회 실패: {e}', {'opers': []}, exc=e)


@csrf_exempt
def config_oper(request):
    oper_id = _body(request).get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류')
    try:
        d = cs.get_oper(oper_id)
        if d is None:
            return _fail(f'{oper_id} 등록 정보가 없습니다')
        return JsonResponse({'ok': True, 'oper': d})
    except Exception as e:
        return _fail(f'조회 실패: {e}', exc=e)


@csrf_exempt
def config_save(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    d = _body(request)
    try:
        res = cs.save_oper(d, user=d.get('user', ''))
        return JsonResponse({'ok': True, 'saved': res})
    except ValueError as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f'저장 실패: {e}', exc=e)


@csrf_exempt
def config_delete(request):
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
def config_import(request):
    """
    구닥스에서 가져오기.
    ★ 웹 프로세스는 사내 모듈을 못 쓰는 경우가 많다. 실패하면 사유를
      그대로 돌려주고, 배치 서버 shell 에서 돌리도록 안내한다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    overwrite = bool(_body(request).get('overwrite'))
    try:
        res = cs.import_from_gooddocs(overwrite=overwrite)
        if not res.get('ok'):
            res['hint'] = ('웹에서 구닥스에 접근하지 못할 수 있습니다. '
                           '배치 서버 shell 에서 아래를 실행하세요.\n'
                           'from equipment import config_service as cs\n'
                           'cs.import_from_gooddocs()')
        return JsonResponse(res)
    except Exception as e:
        return _fail(f'가져오기 실패: {e}', {'imported': 0}, exc=e)


@csrf_exempt
def config_validate(request):
    oper_id = _body(request).get('oper_id')
    if oper_id and not _safe(oper_id):
        return _fail('oper_id 형식 오류', {'results': []})
    try:
        return JsonResponse({'ok': True,
                             'results': cs.validate(oper_id or None)})
    except Exception as e:
        return _fail(f'검증 실패: {e}', {'results': []}, exc=e)


@csrf_exempt
def config_suggest(request):
    oper_id = _body(request).get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류', {'params': []})
    try:
        return JsonResponse({'ok': True, 'params': cs.suggest_params(oper_id)})
    except Exception as e:
        return _fail(f'후보 조회 실패: {e}', {'params': []}, exc=e)


@csrf_exempt
def config_classify(request):
    """
    파라미터 이름 목록을 타입으로 자동 분류한다.
    셋업 페이지의 '타입 자동 분류' 버튼이 쓴다.
    분류 규칙은 param_types.py 에 있으므로 서버에서 판정해
    화면·배치·점검이 같은 규칙을 쓰게 한다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    params = _body(request).get('params', [])
    try:
        return JsonResponse({'ok': True, 'result': cs.classify_params(params)})
    except Exception as e:
        return _fail(f'분류 실패: {e}', {'result': []}, exc=e)


@csrf_exempt
def config_suggest_lot(request):
    """적재 테이블에 실제로 있는 LOT_CD — device 등록 시 후보"""
    oper_id = _body(request).get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류', {'lots': []})
    try:
        return JsonResponse({'ok': True, 'lots': cs.suggest_lots(oper_id)})
    except Exception as e:
        return _fail(f'후보 조회 실패: {e}', {'lots': []}, exc=e)
