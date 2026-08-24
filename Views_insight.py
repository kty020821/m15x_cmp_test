"""
equipment/views_insight.py
════════════════════════════════════════════════════════════
분석 AI API

  api/an2/ask/      질문 → LLM 이 도구를 부르고 해석
  api/an2/run/      LLM 없이 기본 분석 3종만 실행
  api/an2/llm/      LLM 설정 여부 확인 (화면 안내용)

★ 이슈 구간(spans)은 화면이 보낸 것을 서버가 들고 있다가
  도구에 직접 넘긴다. LLM 이 인자로 바꿔 보내도 무시된다 —
  사용자가 화면에서 지정한 것과 분석 대상이 달라지면 안 된다.
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import llm_service as llm


def _fail(msg, payload=None, exc=None):
    print(f'[insight] {msg}')
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


@csrf_exempt
def an2_llm(request):
    """
    LLM 설정 여부 — 화면이 안내 문구를 정할 때 쓴다.
    설정 상태도 함께 준다 (키는 가려서). 주소를 잘못 넣었을 때
    화면에서 바로 확인할 수 있어야 한다.
    """
    try:
        return JsonResponse({'ok': True, 'llm': llm.available(),
                             'config': llm.config_info()})
    except Exception as e:
        return _fail(f'설정 확인 실패: {e}', {'llm': False}, exc=e)


@csrf_exempt
def an2_llm_check(request):
    """
    LLM 연결 진단 — 배포 환경에서 안 될 때 어디가 막혔는지 본다.

    ★ 설정 / DNS / 네트워크 / API 를 순서대로 확인해
      '설정이 없다' 와 '나갈 수 없다' 를 구분해 준다.
    """
    try:
        return JsonResponse({'ok': True, **llm.check_connection()})
    except Exception as e:
        return _fail(f'진단 실패: {e.__class__.__name__}: {e}', exc=e)


@csrf_exempt
def an2_run(request):
    """
    LLM 없이 기본 분석만 실행.
    설정이 없거나, 계산 결과만 빠르게 보고 싶을 때.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    b = _body(request)

    oper_id = b.get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류')

    spans = b.get('spans') or []
    if not spans:
        return _fail('이슈 구간을 먼저 정의하세요 — 비교할 대상이 없습니다')

    try:
        res = llm.run_without_llm(
            oper_id, spans,
            lot_cd=b.get('lot_cd') or None,
            span_a=int(b.get('span_a') or 0))
        return JsonResponse(res)
    except Exception as e:
        return _fail(f'분석 실패: {e.__class__.__name__}: {e}', exc=e)


@csrf_exempt
def an2_ask(request):
    """
    질문 → LLM 이 도구를 부르고 해석한다.

    ★ LLM 설정이 없거나 호출이 실패하면 계산 결과만이라도 돌려준다.
      분석 자체는 서버가 이미 할 수 있으므로 아무것도 못 주는 것보다 낫다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    b = _body(request)

    question = str(b.get('question') or '').strip()
    if not question:
        return _fail('질문을 입력하세요')

    oper_id = b.get('oper_id')
    if not _safe(oper_id):
        return _fail('oper_id 형식 오류')

    spans = b.get('spans') or []
    lot_cd = b.get('lot_cd') or None

    if not llm.available():
        # 설정이 없으면 계산만 하고 그 사실을 알린다
        if not spans:
            return _fail('LLM 설정이 없고 이슈 구간도 없습니다 — '
                         'settings.py 의 CMP_LLM 을 설정하거나 '
                         '이슈 구간을 정의하세요')
        try:
            res = llm.run_without_llm(oper_id, spans, lot_cd=lot_cd,
                                      span_a=int(b.get('span_a') or 0))
            res['note'] = ('LLM 설정이 없어 계산 결과만 표시합니다 '
                           '(settings.py 의 CMP_LLM)')
            return JsonResponse(res)
        except Exception as e:
            return _fail(f'분석 실패: {e.__class__.__name__}: {e}', exc=e)

    try:
        res = llm.ask(
            question, oper_id, spans,
            lot_cd=lot_cd,
            history=b.get('history') or [],
            columns=b.get('columns') or [])
        res['llm'] = True
        return JsonResponse(res)
    except Exception as e:
        traceback.print_exc()
        # LLM 이 죽어도 계산 결과는 준다
        fallback = None
        if spans:
            try:
                fallback = llm.run_without_llm(
                    oper_id, spans, lot_cd=lot_cd,
                    span_a=int(b.get('span_a') or 0))
            except Exception:
                pass
        if fallback:
            fallback['ok'] = True
            fallback['llm'] = False
            fallback['note'] = (f'AI 응답에 실패해 계산 결과만 표시합니다 — '
                                f'{e.__class__.__name__}: {e}')
            return JsonResponse(fallback)
        return _fail(f'분석 실패: {e.__class__.__name__}: {e}', exc=e)
