"""
════════════════════════════════════════════════════════════
equipment/views.py 추가분  —  Part 수명 / 편중도 / LLM 분석
════════════════════════════════════════════════════════════
  ① PART_LIFE_COLS, PART_PATTERNS   Part 수명 컬럼 지정
  ② analysis_part_life              Part 수명 구간 API
  ③ analysis_insight                편중도 계산 + LLM 해석 API
  ④ urls.py 경로 2개 추가

  [설계 원칙]
    LLM 에는 원본 행을 절대 넘기지 않는다.
    코드가 통계·편중도를 계산하고, LLM 은 그 요약을 해석만 한다.
    → 토큰이 적게 들고, 수치를 지어내지 못한다.
════════════════════════════════════════════════════════════
"""

import json
import re
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import connections


# ══════════════════════════════════════════════════════════
# ① Part 수명 컬럼
#    ★ 실제 컬럼명을 알면 PART_LIFE_COLS 에 직접 넣는 것이 가장 정확하다.
#      비워두면 PART_PATTERNS 로 자동 탐색한다.
# ══════════════════════════════════════════════════════════
PART_LIFE_COLS = [
    # ('PAD_COUNT',  'Pad'),
    # ('HEAD_COUNT', 'Head'),
    # ('DISK_COUNT', 'Disk'),
]

# 자동 탐색: 부품 키워드 AND 수명 키워드를 모두 포함하는 컬럼만
# (PAD_PRESSURE 같은 공정 조건이 섞이는 것을 막기 위함)
PART_PATTERNS = [
    ('Pad',  ['PAD'],          ['CNT', 'COUNT', 'LIFE', 'USE', 'USAGE', 'AGE', 'TIME']),
    ('Head', ['HEAD', 'MEMB'], ['CNT', 'COUNT', 'LIFE', 'USE', 'USAGE', 'AGE', 'TIME']),
    ('Disk', ['DISK', 'COND'], ['CNT', 'COUNT', 'LIFE', 'USE', 'USAGE', 'AGE', 'TIME']),
]


def _resolve_part_cols(table):
    """Part 수명 컬럼 → [(컬럼, 표시명), ...]"""
    numeric = _fetch_numeric_cols(table)

    if PART_LIFE_COLS:
        have = set(numeric)
        return [(c, label) for c, label in PART_LIFE_COLS if c in have]

    out, used = [], set()
    for label, part_keys, life_keys in PART_PATTERNS:
        for c in numeric:
            up = c.upper()
            if c in used:
                continue
            if any(k in up for k in part_keys) and any(k in up for k in life_keys):
                out.append((c, label))
                used.add(c)
    return out


# ══════════════════════════════════════════════════════════
# ② Part 수명 구간 API
#    선택 영역이 각 부품 수명의 어느 구간(초/중/말)에 있는지
# ══════════════════════════════════════════════════════════
@csrf_exempt
def analysis_part_life(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    ids     = body.get('ids', [])
    table   = _an_table(oper_id)

    if not ids:
        return JsonResponse({'parts': []})

    try:
        part_cols = _resolve_part_cols(table)
        if not part_cols:
            return JsonResponse({'parts': []})

        cols = [c for c, _ in part_cols]
        ph   = ",".join(["%s"] * len(ids))
        agg  = ", ".join(f'MIN("{c}"), MAX("{c}"), AVG("{c}")' for c in cols)

        with connections['analysis_db'].cursor() as cur:
            # 전체 범위 = 해당 LOT_CD 전체 (수명 구간의 기준선)
            cur.execute(f'SELECT {agg} FROM {table} WHERE "LOT_CD" = %s', [lot_cd])
            arow = cur.fetchone()
            cur.execute(f'SELECT {agg} FROM {table} WHERE id IN ({ph})', ids)
            srow = cur.fetchone()

        parts = []
        for i, (col, label) in enumerate(part_cols):
            j = i * 3
            a_min, a_max, a_avg = arow[j], arow[j + 1], arow[j + 2]
            s_min, s_max, s_avg = srow[j], srow[j + 1], srow[j + 2]
            if a_min is None or a_max is None or s_avg is None:
                continue

            span = float(a_max) - float(a_min)

            def pos(v):
                if v is None:
                    return None
                if span <= 0:
                    return 0.5
                return max(0.0, min(1.0, (float(v) - float(a_min)) / span))

            p_avg = pos(s_avg)
            phase = '초기' if p_avg < 1/3 else ('중기' if p_avg < 2/3 else '말기')

            parts.append({
                'col': col, 'label': label, 'phase': phase,
                'all': {'min': _f(a_min), 'max': _f(a_max), 'avg': _f(a_avg)},
                'sel': {'min': _f(s_min), 'max': _f(s_max), 'avg': _f(s_avg)},
                'pos': {'min': pos(s_min), 'max': pos(s_max), 'avg': p_avg},
            })

        return JsonResponse({'parts': parts})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ══════════════════════════════════════════════════════════
# ③ 편중도 + LLM 해석
# ══════════════════════════════════════════════════════════

# 편중 판정 기준
LIFT_MIN  = 1.5     # 전체 대비 몇 배 이상 몰려야 '편중'으로 볼지
COUNT_MIN = 3       # 선택 영역 내 최소 건수 (우연 배제)


def _enrichment(cur, table, col, ids, lot_cd, ph):
    """
    한 요인 컬럼의 편중도.
      선택 영역에서의 비율이 전체에서의 비율보다 얼마나 높은가(lift).
      예) 선택 85% vs 전체 32% → lift 2.7  →  이 장비에 문제가 몰려 있다
    """
    def counts(where, params):
        cur.execute(f'''
            SELECT COALESCE(NULLIF(CAST("{col}" AS TEXT), ''), '(없음)') AS k, COUNT(*)
            FROM {table} WHERE {where} GROUP BY k
        ''', params)
        return {r[0]: r[1] for r in cur.fetchall()}

    sel = counts(f'id IN ({ph})', list(ids))
    allc = counts('"LOT_CD" = %s', [lot_cd])

    s_tot = sum(sel.values()) or 1
    a_tot = sum(allc.values()) or 1

    out = []
    for k, sc in sel.items():
        ac = allc.get(k, 0)
        s_ratio = sc / s_tot
        a_ratio = (ac / a_tot) if ac else 0.0
        lift = (s_ratio / a_ratio) if a_ratio > 0 else None
        out.append({
            'key': k, 'sel_n': sc, 'all_n': ac,
            'sel_pct': round(s_ratio * 100, 1),
            'all_pct': round(a_ratio * 100, 1),
            'lift': round(lift, 2) if lift else None,
            'flag': bool(lift and lift >= LIFT_MIN and sc >= COUNT_MIN),
        })
    out.sort(key=lambda x: (x['lift'] or 0), reverse=True)
    return out


def _build_summary(oper_id, lot_cd, param, ids):
    """LLM 에 넘길 구조화 요약 (원본 행은 포함하지 않는다)"""
    table = _an_table(oper_id)
    ph    = ",".join(["%s"] * len(ids))

    with connections['analysis_db'].cursor() as cur:
        # 존재하는 컬럼만
        cur.execute("""
            SELECT upper(column_name) FROM information_schema.columns
            WHERE table_name = %s
        """, [table])
        have = {r[0] for r in cur.fetchall()}

        # 대상 파라미터 통계 (선택 vs 전체)
        cur.execute(f'''
            SELECT COUNT(*), AVG("{param}"), STDDEV("{param}"),
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "{param}")
            FROM {table} WHERE id IN ({ph})
        ''', list(ids))
        s_n, s_avg, s_std, s_med = cur.fetchone()

        cur.execute(f'''
            SELECT COUNT(*), AVG("{param}"), STDDEV("{param}"),
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "{param}")
            FROM {table} WHERE "LOT_CD" = %s
        ''', [lot_cd])
        a_n, a_avg, a_std, a_med = cur.fetchone()

        # 요인별 편중도
        factors = []
        for col, label, _empty in FACTOR_COLS:
            if col not in have:
                continue
            rows = _enrichment(cur, table, col, ids, lot_cd, ph)
            factors.append({'col': col, 'label': label, 'rows': rows})

    # Part 수명
    parts = []
    try:
        part_cols = _resolve_part_cols(table)
        if part_cols:
            cols = [c for c, _ in part_cols]
            agg  = ", ".join(f'MIN("{c}"), MAX("{c}"), AVG("{c}")' for c in cols)
            with connections['analysis_db'].cursor() as cur:
                cur.execute(f'SELECT {agg} FROM {table} WHERE "LOT_CD" = %s', [lot_cd])
                arow = cur.fetchone()
                cur.execute(f'SELECT {agg} FROM {table} WHERE id IN ({ph})', list(ids))
                srow = cur.fetchone()
            for i, (col, label) in enumerate(part_cols):
                j = i * 3
                a_min, a_max = arow[j], arow[j + 1]
                s_avg = srow[j + 2]
                if a_min is None or a_max is None or s_avg is None:
                    continue
                span = float(a_max) - float(a_min)
                p = 0.5 if span <= 0 else (float(s_avg) - float(a_min)) / span
                p = max(0.0, min(1.0, p))
                parts.append({
                    'label': label,
                    'phase': '초기' if p < 1/3 else ('중기' if p < 2/3 else '말기'),
                    'pct':   round(p * 100),
                    'sel_avg': _f(s_avg),
                    'range': [_f(a_min), _f(a_max)],
                })
    except Exception:
        pass

    dev = None
    if s_avg is not None and a_avg is not None and a_avg:
        dev = round((float(s_avg) - float(a_avg)) / abs(float(a_avg)) * 100, 2)

    return {
        'oper_id': oper_id, 'lot_cd': lot_cd, 'param': param,
        'target': {
            'sel': {'n': s_n, 'avg': _f(s_avg), 'med': _f(s_med), 'std': _f(s_std)},
            'all': {'n': a_n, 'avg': _f(a_avg), 'med': _f(a_med), 'std': _f(a_std)},
            'dev_pct': dev,
        },
        'factors': factors,
        'parts': parts,
    }


def _summary_to_text(s):
    """요약 → LLM 프롬프트용 텍스트 (짧게 유지)"""
    L = []
    L.append(f"[대상] 공정 {s['oper_id']} / 제품 {s['lot_cd']} / 파라미터 {s['param']}")

    t = s['target']
    L.append(f"[선택 영역] {t['sel']['n']}장 (전체 {t['all']['n']}장)")
    L.append(f"  평균 {t['sel']['avg']} (전체 {t['all']['avg']}, 편차 {t['dev_pct']}%)")
    L.append(f"  표준편차 {t['sel']['std']} (전체 {t['all']['std']})")

    flagged = []
    for f in s['factors']:
        hits = [r for r in f['rows'] if r['flag']]
        for r in hits[:3]:
            flagged.append(
                f"  {f['label']} = {r['key']} : 선택 {r['sel_pct']}% vs 전체 {r['all_pct']}%"
                f" (집중도 {r['lift']}배, {r['sel_n']}장)")
    if flagged:
        L.append("[편중된 요인] (전체 대비 유의하게 몰려 있는 조건)")
        L.extend(flagged)
    else:
        L.append("[편중된 요인] 뚜렷하게 몰린 조건 없음")

    if s['parts']:
        L.append("[소모품 수명 구간]")
        for p in s['parts']:
            L.append(f"  {p['label']}: {p['phase']} (수명 {p['pct']}% 지점,"
                     f" 선택 평균 {p['sel_avg']}, 전체 범위 {p['range'][0]}~{p['range'][1]})")

    return "\n".join(L)


LLM_SYSTEM = """당신은 반도체 CMP(화학적 기계 연마) 공정 엔지니어를 돕는 분석 보조자입니다.
엔지니어가 트렌드 차트에서 이상 구간을 선택했고, 그 구간의 통계 요약이 주어집니다.

다음 형식으로 간결하게 한국어로 답하세요.

## 관찰
데이터에서 실제로 보이는 사실만 2~3줄.

## 추정 원인
가능성 높은 순으로 2~3개. 각 항목에 근거가 된 수치를 함께 적으세요.

## 확인할 것
엔지니어가 다음에 직접 확인해야 할 항목을 구체적으로.

규칙:
- 주어진 수치만 사용하고, 없는 값을 지어내지 마세요.
- 편중된 요인이 없으면 "특정 조건에 몰려 있지 않다"고 분명히 쓰세요.
- 상관관계를 인과관계로 단정하지 마세요.
- 소모품 수명 구간(초/중/말)이 한쪽에 몰려 있으면 반드시 언급하세요."""


def _call_company_llm(system, user):
    """
    ★ 사내 LLM API 호출 — 실제 엔드포인트로 교체할 것

    예시)
        import requests
        r = requests.post(
            "https://사내-llm-endpoint/v1/chat",
            json={"model": "...",
                  "messages": [{"role": "system", "content": system},
                               {"role": "user",   "content": user}]},
            headers={"Authorization": "Bearer ..."},
            timeout=60,
        )
        return r.json()["choices"][0]["message"]["content"]
    """
    raise NotImplementedError("사내 LLM API 미연결")


@csrf_exempt
def analysis_insight(request):
    """
    선택 영역의 편중도를 계산하고, LLM 해석을 덧붙인다.
    LLM 미연결이어도 편중도 결과는 그대로 반환한다.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    param   = body.get('param')
    ids     = body.get('ids', [])
    use_llm = body.get('use_llm', True)

    if not ids:
        return JsonResponse({'error': '선택된 영역이 없습니다'}, status=400)
    if not param or not re.match(r'^[0-9A-Za-z_]+$', param):
        return JsonResponse({'error': '잘못된 파라미터'}, status=400)

    try:
        summary = _build_summary(oper_id, lot_cd, param, ids)
        text    = _summary_to_text(summary)

        answer, llm_error = None, None
        if use_llm:
            try:
                answer = _call_company_llm(LLM_SYSTEM, text)
            except NotImplementedError:
                llm_error = '사내 LLM API가 아직 연결되지 않았습니다.'
            except Exception as e:
                llm_error = f'LLM 호출 실패: {e}'

        return JsonResponse({
            'summary':   summary,     # 화면에 표로 뿌릴 용도
            'prompt':    text,        # 무엇을 넘겼는지 확인용
            'answer':    answer,
            'llm_error': llm_error,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ══════════════════════════════════════════════════════════
# ④ equipment/urls.py 에 아래 2줄 추가
# ══════════════════════════════════════════════════════════
"""
    path('api/analysis/part-life/', views.analysis_part_life, name='analysis-part-life'),
    path('api/analysis/insight/',   views.analysis_insight,   name='analysis-insight'),
"""
