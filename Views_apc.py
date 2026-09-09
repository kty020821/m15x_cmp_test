"""
equipment/views_apc.py
════════════════════════════════════════════════════════════
APC 설정 비교 화면·API
════════════════════════════════════════════════════════════
"""
import json
import traceback

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from . import apc_service as apc


def _fail(msg, extra=None, exc=None):
    if exc is not None:
        traceback.print_exc()
    print(f'[apc] {msg}')
    return JsonResponse({'ok': False, 'error': msg, **(extra or {})})


def _body(request):
    try:
        return json.loads(request.body or '{}')
    except Exception:
        return {}


def apc_page(request):
    return render(request, 'equipment/apc.html',
                  {'config_ok': apc.config_ok()})


@csrf_exempt
def apc_compare(request):
    """
    두 대상의 APC 설정을 비교한다.

    ★ 매번 API 를 호출한다 — 설정은 자주 바뀌지 않지만,
      쌓아두면 '지금 설정' 인지 '그때 설정' 인지 헷갈린다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    b = _body(request)
    ref = b.get('ref') or {}
    tgt = b.get('tgt') or {}

    for name, d in (('기준', ref), ('대상', tgt)):
        if not str(d.get('fab') or '').strip():
            return _fail(f'{name}의 FAB 을 입력하세요')
        if not str(d.get('eqp_id') or '').strip():
            return _fail(f'{name}의 EQP_ID 를 입력하세요')

    try:
        # 챔버 짝 묶기 — 화면에서 끌 수 있다
        pair = b.get('pair_ch')
        pair = True if pair is None else bool(pair)
        return JsonResponse({'ok': True, **apc.compare(ref, tgt, pair)})
    except ValueError as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f'조회 실패: {e}', exc=e)
