"""
equipment/llm_service.py
════════════════════════════════════════════════════════════
분석 AI — LLM 이 도구를 골라 부르고, 그 결과를 해석한다

  insight_service 가 계산한 근거표를 LLM 이 읽고
  우선순위를 매기고 공정 맥락으로 설명한다.

────────────────────────────────────────────────────────────
동작

  사용자 질문
    → LLM 이 필요한 도구를 고른다 (compare_groups 등)
    → 서버가 도구를 실행해 근거표를 만든다
    → 표를 LLM 에 돌려준다
    → 필요하면 도구를 더 부른다 (최대 MAX_ROUNDS 회)
    → 최종 답변 + 근거표를 함께 화면에 낸다

★ 원시 데이터는 절대 LLM 에 넘기지 않는다.
  도구가 돌려주는 요약표만 오간다.

★ 이슈 구간(spans)은 화면이 정한 것을 서버가 들고 있다.
  LLM 이 인자로 바꿔 보내도 무시한다 — 사용자가 화면에서 지정한 것과
  분석 대상이 달라지면 안 된다.

★ 도구 결과에 없는 수치는 말하지 못하게 한다.
  환각으로 숫자를 지어내면 그게 가장 나쁘다.

────────────────────────────────────────────────────────────
설정 (config/settings.py)

  이미 쓰고 있는 이름이 있으면 그대로 쓴다. 아래 중 아무 형태나 된다.

  ① 개별 변수 (기존 방식)
     LLM_URL     = os.getenv('LLM_URL', '')      # OpenAI 호환 엔드포인트
     LLM_API_KEY = os.getenv('LLM_API_KEY', '')
     LLM_MODEL   = os.getenv('LLM_MODEL', 'gpt-4o')

  ② 묶음
     CMP_LLM = {'base_url': ..., 'api_key': ..., 'model': ..., 'timeout': 120}

  ★ base_url 은 /chat/completions 를 뺀 주소다.
    'https://host/v1' 처럼 넣으면 되고, 끝에 /chat/completions 가
    붙어 있어도 알아서 떼어 낸다 — 흔한 실수라 여기서 흡수한다.
════════════════════════════════════════════════════════════
"""

import json
import time
import traceback

from django.conf import settings

from . import insight_service as ins

# 도구 호출 왕복 상한 — 무한 루프와 비용 폭주를 막는다
MAX_ROUNDS = 5

# 대화 기록에 남길 최근 턴 수
MAX_HISTORY = 8


SYSTEM_PROMPT = """\
당신은 반도체 CMP 공정의 산포 분석을 돕는 분석가입니다.
사용자는 CMP 공정 엔지니어이며, 한국어로 간결하게 답합니다.

[데이터]
웨이퍼 한 장이 한 행입니다. 컬럼 이름 규칙:
  · 접두어 없음  본공정(CMP) 측정값·설비 파라미터
  · SRC_<별칭>_<PARAM>   연계 공정의 측정값
  · REP_<별칭>_<PARAM>   Response 계측
  · DEF_<별칭>_<PARAM>   Defect 계수
  · <별칭>_EQP / <별칭>_CH   연계 공정의 장비·챔버
  · EQP_ID, EQP_CH_ID, RECIPE_ID, PRE_EQP_ID, PRE_EQP_CH  본공정 설비 정보
  · REWORK_N  1보다 크면 재작업된 웨이퍼입니다

[도구 사용 — 이슈 웨이퍼 수에 따라 다르게]

★ 반도체 공정의 이상은 대개 산발적입니다. 30일치에서 몇 장만 튀는 일이
  흔하고, 그 몇 장이 분석 대상입니다. 표본이 적다고 분석을 포기하지 마세요.
  적은 표본에 맞는 도구가 따로 있습니다.

· 이슈 웨이퍼가 적을 때 (대략 30장 미만) — 이 순서로 쓰세요
    ① common_traits    그 몇 장이 공유하는 조건 (같은 챔버? 시간대?)
    ② wafer_fingerprint 한 장씩 어느 파라미터에서 벗어났나
    ③ rule_search       조건 조합에서 이상률이 튀는 지점
  이 셋은 평균 비교가 아니라 '몰림' 과 '개별 이탈' 을 보므로
  표본이 적어도 유효합니다. 5장이 전부 같은 챔버면 그건 강한 신호입니다.

· 이슈 웨이퍼가 많을 때 (대략 30장 이상)
    ① compare_groups → ② feature_importance → ③ interaction

· 장비·챔버 차이는 distribution, 언제부터 달라졌는지는 timeline.
· 한 번에 필요한 도구만 부르고, 결과를 본 뒤 다음을 정하세요.
· compare_groups 나 feature_importance 가 '표본이 부족하다' 고 하면
  실패로 보지 말고 common_traits / wafer_fingerprint 로 바꿔 부르세요.

[답변 규칙]
· 도구 결과에 없는 수치는 절대 말하지 마세요. 숫자를 지어내면 안 됩니다.
· 표본이 적다는 이유만으로 '분석할 수 없다' 고 하지 마세요.
  소표본 도구(common_traits·wafer_fingerprint·rule_search)의 결과는
  장수가 적어도 유효합니다. 그 도구들이 낸 lift·p·q 를 근거로 말하세요.
· 다만 소표본에서는 '가능성' 의 수준으로 표현하고, 확인 방법을 함께
  제안하세요 (예: 해당 챔버의 다음 랏을 지켜보기).
· 모델 성능이 낮으면(confidence 낮음, AUC 낮음) 그 사실을 먼저 알리세요.
· significant 가 False 인 항목은 '통계적으로 확인되지 않았다' 고 하세요.
· 상관은 인과가 아닙니다. '원인' 이라고 단정하지 말고
  '가능성이 높다', '함께 움직인다' 처럼 쓰세요.
· 마지막에 다음에 확인해 볼 것을 1~2개 제안하세요.
· 답변은 500자 안팎으로 짧게. 표가 필요하면 3~5행만.
"""


def _s(v):
    return str(v or '').strip()


def _cfg():
    """
    설정을 읽는다. 기존에 쓰던 개별 변수를 먼저 보고, 없으면 CMP_LLM 을 본다.

    ★ 이름을 강요하지 않는다 — 이미 LLM_URL 로 운영 중인데 새 이름을
      요구하면 환경변수·배포 설정까지 다 손봐야 한다.
    """
    c = getattr(settings, 'CMP_LLM', None) or {}

    url = (_s(getattr(settings, 'LLM_URL', ''))
           or _s(getattr(settings, 'LLM_BASE_URL', ''))
           or _s(c.get('base_url')))
    key = (_s(getattr(settings, 'LLM_API_KEY', ''))
           or _s(getattr(settings, 'LLM_KEY', ''))
           or _s(getattr(settings, 'OPENAI_API_KEY', ''))
           or _s(c.get('api_key')))
    model = (_s(getattr(settings, 'LLM_MODEL', ''))
             or _s(c.get('model')) or 'gpt-4o')
    timeout = getattr(settings, 'LLM_TIMEOUT', None) or c.get('timeout') or 120

    # 끝에 /chat/completions 가 붙어 있으면 떼어 낸다 (흔한 실수)
    url = url.rstrip('/')
    for tail in ('/chat/completions', '/completions'):
        if url.endswith(tail):
            url = url[:-len(tail)]

    return {'base_url': url, 'api_key': key, 'model': model,
            'timeout': int(timeout)}


def available():
    """
    쓸 수 있는 상태인지 — 화면이 안내 문구를 정할 때 쓴다.
    ★ 주소만 있으면 된다. 사내 게이트웨이는 키 없이 열려 있기도 하다.
    """
    return bool(_cfg()['base_url'])


def config_info():
    """
    설정 상태 확인용 (키는 가린다).

    ★ 로컬은 되는데 배포에서 안 되는 경우가 대부분 여기서 갈린다.
      settings 에는 값이 있는데 환경변수가 안 넘어와 빈 문자열이 되거나,
      컨테이너에서 그 주소로 나갈 수 없거나(방화벽), 둘 중 하나다.
      어느 쪽인지 화면에서 바로 알 수 있게 출처까지 낸다.
    """
    import os as _os

    c = _cfg()
    key = c['api_key']

    # 어디서 읽었는지 — 환경변수인지 llm_config.py 인지
    src = []
    for name in ('LLM_URL', 'LLM_BASE_URL'):
        if _s(getattr(settings, name, '')):
            src.append(f'settings.{name}')
    if getattr(settings, 'CMP_LLM', None):
        src.append('settings.CMP_LLM')

    env = [k for k in ('LLM_URL', 'LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL')
           if _s(_os.environ.get(k))]

    # llm_config.py 가 실제로 읽혔는지 — 배포에서 파일 누락이 흔하다
    #   settings 모듈 경로는 환경변수에 있다 (예: 'config.settings')
    cfg_file = '없음'
    try:
        import importlib
        pkg = _os.environ.get('DJANGO_SETTINGS_MODULE', '')
        if pkg and '.' in pkg:
            importlib.import_module(pkg.rsplit('.', 1)[0] + '.llm_config')
            cfg_file = '있음'
    except Exception:
        pass

    return {
        'base_url': c['base_url'] or '(없음)',
        'model': c['model'],
        'api_key': (key[:4] + '…' + key[-4:]) if len(key) > 8
                   else ('설정됨' if key else '(없음)'),
        'timeout': c['timeout'],
        'ready': bool(c['base_url']),
        'source': ', '.join(src) or '(없음)',
        'env_vars': ', '.join(env) or '(없음)',
        'config_file': cfg_file,
    }


def check_connection():
    """
    실제로 연결이 되는지 확인한다.

    ★ 설정이 있어도 컨테이너에서 그 주소로 못 나가는 경우가 있다
      (방화벽·프록시·DNS). 설정 문제와 통신 문제를 구분해야
      어디를 고칠지 알 수 있다.
    """
    import socket
    import urllib.parse

    c = _cfg()
    out = {'config': config_info()}

    if not c['base_url']:
        out['stage'] = '설정'
        out['error'] = ('LLM_URL 이 비어 있습니다 — 배포 환경에 환경변수가 '
                        '전달됐는지 확인하세요 (docker-compose 의 environment, '
                        '또는 .env 파일)')
        return out

    u = urllib.parse.urlparse(c['base_url'])
    host = u.hostname or ''
    port = u.port or (443 if u.scheme == 'https' else 80)

    # ① DNS
    try:
        ip = socket.gethostbyname(host)
        out['dns'] = f'{host} → {ip}'
    except Exception as e:
        out['stage'] = 'DNS'
        out['error'] = (f'{host} 주소를 찾을 수 없습니다 — 컨테이너의 DNS 를 '
                        f'확인하세요: {e}')
        return out

    # ② TCP 연결
    try:
        sock = socket.create_connection((host, port), timeout=8)
        sock.close()
        out['tcp'] = f'{host}:{port} 연결됨'
    except Exception as e:
        out['stage'] = '네트워크'
        out['error'] = (f'{host}:{port} 에 연결할 수 없습니다 — 컨테이너에서 '
                        f'외부로 나가는 경로(방화벽·프록시)를 확인하세요: {e}')
        return out

    # ③ 실제 호출
    try:
        res = _post([{'role': 'user', 'content': 'ping'}])
        msg = ((res.get('choices') or [{}])[0].get('message') or {})
        out['stage'] = 'OK'
        out['reply'] = (msg.get('content') or '')[:80]
    except Exception as e:
        out['stage'] = 'API'
        out['error'] = str(e)
    return out


def _post(messages, tools=None):
    """
    OpenAI 호환 chat/completions 호출.

    ★ requests 는 사내망에서 프록시 설정이 걸릴 수 있어
      urllib 로 직접 부른다 (표준 라이브러리라 의존이 없다).
    """
    import urllib.request
    import urllib.error

    c = _cfg()
    if not c['base_url']:
        raise RuntimeError('LLM 주소가 없습니다 — settings.py 의 LLM_URL 을 '
                           '확인하세요')

    body = {'model': c['model'], 'messages': messages, 'temperature': 0.2}
    if tools:
        body['tools'] = tools
        body['tool_choice'] = 'auto'

    headers = {'Content-Type': 'application/json'}
    # 사내 게이트웨이는 키 없이 열려 있는 경우도 있어 조건부로 넣는다
    if c['api_key']:
        headers['Authorization'] = f"Bearer {c['api_key']}"

    req = urllib.request.Request(
        f"{c['base_url']}/chat/completions",
        data=json.dumps(body).encode('utf-8'),
        headers=headers, method='POST')

    try:
        with urllib.request.urlopen(req, timeout=c['timeout']) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:400]
        except Exception:
            pass
        raise RuntimeError(f'LLM 호출 실패 ({e.code}): {detail}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'LLM 서버에 연결하지 못했습니다: {e.reason}')


def _context_note(oper_id, spans, lot_cd, columns=None):
    """지금 화면 상태를 LLM 에게 알려 준다"""
    lines = [f'분석 대상: {oper_id}']
    if lot_cd:
        lines.append(f'LOT_CD: {lot_cd}')
    if spans:
        for i, s in enumerate(spans):
            mode = {'range': '기간', 'lots': '랏', 'wafers': '웨이퍼'}.get(
                s.get('mode'), s.get('mode'))
            if s.get('mode') == 'range':
                detail = f"{s.get('date_from', '')} ~ {s.get('date_to', '')}"
            elif s.get('mode') == 'lots':
                detail = f"{len(s.get('lot_ids') or [])}개 랏"
            else:
                detail = f"{len(s.get('wafers') or [])}장"
            lines.append(f"구간 {i}: {s.get('name') or ''} ({mode}, {detail})")
    else:
        lines.append('정의된 이슈 구간이 없습니다 — '
                     '구간이 필요한 도구는 쓸 수 없습니다.')
    if columns:
        head = ', '.join(columns[:40])
        more = f' 외 {len(columns) - 40}개' if len(columns) > 40 else ''
        lines.append(f'사용 가능한 파라미터: {head}{more}')
    return '\n'.join(lines)


def ask(question, oper_id, spans, lot_cd=None, history=None, columns=None,
        on_step=None):
    """
    질문 하나를 처리한다.

    반환: {'ok', 'answer', 'evidence':[...], 'rounds', 'elapsed'}
      evidence 는 실제로 실행된 도구와 그 결과 — 화면이 근거로 함께 보여 준다.
    """
    t0 = time.time()

    msgs = [{'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'system', 'content': _context_note(oper_id, spans,
                                                        lot_cd, columns)}]

    # 이전 대화 — 도구 호출 기록은 빼고 사람이 주고받은 것만
    for h in (history or [])[-MAX_HISTORY:]:
        role = h.get('role')
        if role in ('user', 'assistant') and h.get('content'):
            msgs.append({'role': role, 'content': str(h['content'])[:4000]})

    msgs.append({'role': 'user', 'content': question})

    tools = ins.tool_specs()
    evidence = []

    for rnd in range(MAX_ROUNDS):
        res = _post(msgs, tools=tools)
        choice = (res.get('choices') or [{}])[0]
        m = choice.get('message') or {}
        calls = m.get('tool_calls') or []

        if not calls:
            return {
                'ok': True,
                'answer': (m.get('content') or '').strip()
                          or '답변을 만들지 못했습니다.',
                'evidence': evidence,
                'rounds': rnd + 1,
                'elapsed': round(time.time() - t0, 1),
            }

        msgs.append({'role': 'assistant', 'content': m.get('content') or '',
                     'tool_calls': calls})

        for call in calls:
            fn = (call.get('function') or {})
            name = fn.get('name') or ''
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except Exception:
                args = {}

            # ★ 화면이 정한 대상으로 고정한다. LLM 이 바꿔 보내도 무시.
            #   run_tool 이 oper_id 를 따로 꺼내므로 여기서 넣어 준다.
            args['oper_id'] = oper_id
            if lot_cd:
                args['lot_cd'] = lot_cd

            if on_step:
                on_step(name, args)

            out = ins.run_tool(name, args, spans)
            evidence.append({'tool': name, 'args': args, 'result': out})

            msgs.append({
                'role': 'tool',
                'tool_call_id': call.get('id'),
                'name': name,
                'content': json.dumps(out, ensure_ascii=False)[:12000],
            })

    # 상한까지 갔으면 지금까지의 근거로 정리시킨다
    msgs.append({'role': 'user',
                 'content': '지금까지 확인한 내용으로 정리해 주세요. '
                            '추가 도구 호출은 하지 마세요.'})
    res = _post(msgs)
    m = (res.get('choices') or [{}])[0].get('message') or {}
    return {
        'ok': True,
        'answer': (m.get('content') or '').strip() or '정리하지 못했습니다.',
        'evidence': evidence,
        'rounds': MAX_ROUNDS,
        'elapsed': round(time.time() - t0, 1),
        'note': f'도구 호출이 {MAX_ROUNDS}회에 도달해 정리했습니다.',
    }


def summarize_evidence(evidence):
    """
    LLM 없이 근거표만 요약한다.

    ★ LLM 설정이 없거나 호출이 실패해도 분석 자체는 쓸 수 있어야 한다.
      계산은 서버가 이미 했으므로 그 결과를 그대로 보여 준다.
    ★ 키 이름은 insight_service 의 실제 반환에 맞춘다
      (tool / items / n_a·n_b / n_in·n_out / cohens_d).
    """
    CONF = {'high': '높음', 'mid': '보통', 'low': '낮음'}
    out = []

    for e in evidence or []:
        r = e.get('result') or {}
        tool = r.get('tool') or e.get('tool')
        if not r.get('ok'):
            out.append(f"· {tool}: {r.get('error', '실패')}")
            continue

        conf = CONF.get(r.get('confidence'), r.get('confidence') or '?')

        if tool == 'compare_groups':
            out.append(f"■ 구간 비교 — 대상 {r.get('n_a', 0)}장 vs "
                       f"{r.get('n_b', 0)}장 · 파라미터 {r.get('n_param', 0)}개 중 "
                       f"유의 {r.get('n_significant', 0)}건 (신뢰도 {conf})")
            if r.get('note'):
                out.append(f"   {r['note']}")
            for it in (r.get('items') or [])[:6]:
                mark = '유의' if it.get('significant') else '참고'
                out.append(f"   [{mark}] {it.get('param')}  "
                           f"d={it.get('cohens_d')}  "
                           f"{it.get('mean_a')} vs {it.get('mean_b')}  "
                           f"(q={it.get('q')})")

        elif tool == 'feature_importance':
            out.append(f"■ 요인 순위 — 구간 {r.get('n_in', 0)}장 / "
                       f"나머지 {r.get('n_out', 0)}장 · AUC {r.get('auc')} "
                       f"(신뢰도 {conf})")
            if r.get('note'):
                out.append(f"   {r['note']}")
            for it in (r.get('items') or [])[:6]:
                out.append(f"   {it.get('feature')}: {it.get('importance')}")

        elif tool == 'interaction':
            out.append(f"■ 교호작용 — {r.get('target')} 기준 · "
                       f"조합 {r.get('n_pair', 0)}건 (신뢰도 {conf})")
            if r.get('note'):
                out.append(f"   {r['note']}")
            for it in (r.get('items') or [])[:5]:
                flip = ' · 방향 반대' if it.get('sign_flip') else ''
                out.append(f"   {it.get('category')} × {it.get('param')}  "
                           f"점수 {it.get('score')}{flip}")

        elif tool == 'distribution':
            out.append(f"■ 범주별 분포 — {r.get('param')}")
            for g in (r.get('groups') or [])[:2]:
                out.append(f"   [{g.get('key')}]")
                for lv in (g.get('levels') or [])[:5]:
                    out.append(f"     {lv.get('level')}: 평균 {lv.get('mean')} "
                               f"({lv.get('n')}장)")

        elif tool == 'common_traits':
            out.append(f"■ 공통 조건 — 이상 {r.get('n_wafer', 0)}장 / "
                       f"전체 {r.get('n_total', 0)}장 · "
                       f"유의 {r.get('n_significant', 0)}건")
            if r.get('note'):
                out.append(f"   {r['note']}")
            for it in (r.get('items') or [])[:6]:
                mark = '유의' if it.get('significant') else '참고'
                out.append(f"   [{mark}] {it.get('key')}={it.get('level')}  "
                           f"{it.get('k')}/{it.get('n')}장 "
                           f"(전체 비중 {it.get('base_ratio')}) "
                           f"lift {it.get('lift')}배 · q={it.get('q')}")

        elif tool == 'wafer_fingerprint':
            out.append(f"■ 웨이퍼 지문 — {r.get('n_wafer', 0)}장 "
                       f"(기준 {r.get('n_base', 0)}장)")
            if r.get('note'):
                out.append(f"   {r['note']}")
            for it in (r.get('shared') or [])[:5]:
                out.append(f"   공통: {it.get('param')} — "
                           f"{it.get('n_wafer')}장에서 {it.get('direction')} "
                           f"(평균 z {it.get('avg_z')})")
            for w in (r.get('wafers') or [])[:3]:
                tops = ', '.join(f"{h.get('param')} z={h.get('z')}"
                                 for h in (w.get('top') or [])[:3])
                out.append(f"   {w.get('wafer')}: {tops or '뚜렷한 이탈 없음'}")

        elif tool == 'rule_search':
            out.append(f"■ 조건 조합 — 이상 {r.get('n_wafer', 0)}장 · "
                       f"기본 이상률 {r.get('base_rate')}")
            if r.get('note'):
                out.append(f"   {r['note']}")
            for it in (r.get('items') or [])[:5]:
                mark = '유의' if it.get('significant') else '참고'
                out.append(f"   [{mark}] {it.get('rule')}  "
                           f"이상률 {it.get('rate')} (lift {it.get('lift')}배) · "
                           f"{it.get('k')}/{it.get('K')}장")

        elif tool == 'timeline':
            out.append(f"■ 시간 추이 — {r.get('param')} · {r.get('n', 0)}점")

        else:
            out.append(f"■ {tool}")

    return '\n'.join(out) or '분석 결과가 없습니다.'


def run_without_llm(oper_id, spans, lot_cd=None, span_a=0):
    """
    LLM 없이 기본 분석 3종을 돌린다.
    설정이 없을 때도 화면에서 쓸 수 있게 하는 경로다.
    """
    # ★ 이슈 웨이퍼가 적으면 소표본 도구를 쓴다.
    #   평균 비교·트리 모델은 몇 장짜리 구간에서 '표본 부족' 만 내놓는다.
    n_in = _span_size(oper_id, spans, lot_cd, span_a)
    small = (n_in is not None and n_in < 30)

    if small:
        plan = (('common_traits', {'span_a': span_a}),
                ('wafer_fingerprint', {'span_a': span_a}),
                ('rule_search', {'span_a': span_a}))
    else:
        plan = (('compare_groups', {'span_a': span_a}),
                ('feature_importance', {'span_a': span_a}),
                ('interaction', {'span_a': span_a}))

    evidence = []
    for name, args in plan:
        a = dict(args)
        a['oper_id'] = oper_id
        if lot_cd:
            a['lot_cd'] = lot_cd
        try:
            out = ins.run_tool(name, a, spans)
        except Exception as e:
            traceback.print_exc()
            out = {'ok': False, 'error': f'{e.__class__.__name__}: {e}'}
        evidence.append({'tool': name, 'args': a, 'result': out})

    head = ''
    if small:
        head = (f'이슈 웨이퍼가 {n_in}장으로 적어 소표본 분석을 했습니다 — '
                f'평균 비교 대신 "공통 조건" 과 "개별 이탈" 을 봅니다.\n\n')

    return {
        'ok': True,
        'answer': head + summarize_evidence(evidence),
        'evidence': evidence,
        'llm': False,
        'n_in': n_in,
        'note': 'LLM 설정이 없어 계산 결과만 표시합니다.',
    }


def _span_size(oper_id, spans, lot_cd=None, span_a=0):
    """
    지정 구간의 웨이퍼 수 — 어떤 분석을 쓸지 정하는 데 쓴다.
    실패하면 None 을 돌려 기존 경로를 타게 한다.
    """
    try:
        df, _num, _cat = ins.load_frame(oper_id, spans, lot_cd)
        return int((df['__span'] == span_a).sum())
    except Exception as e:
        print(f'[llm] 구간 크기 확인 실패: {e.__class__.__name__}: {e}')
        return None
