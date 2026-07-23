"""
════════════════════════════════════════════════════════════
equipment/views.py  —  요인 분포에 WF 구간 추가
  기존 FACTOR_COLS / analysis_stats / _enrichment 를 아래로 교체
════════════════════════════════════════════════════════════

FACTOR_COLS 항목에 4번째 값(구간 수)을 주면 그 컬럼을 숫자로 보고
같은 폭의 구간으로 나눠 집계한다.  WF_ID 처럼 1~25 로 이어지는 값을
"1~5 / 6~10 / …" 로 묶어, 웨이퍼 위치에 따른 편중을 보기 위한 것.
"""

from math import ceil


# ══════════════════════════════════════════════════════════
# 요인 분포 항목
#   (컬럼, 표시명, 빈값 표기, 구간 수)
#   구간 수를 None 으로 두면 값 그대로 집계한다.
#   ★ 요인 추가는 여기 한 줄. 테이블에 없는 컬럼은 자동으로 건너뜀
# ══════════════════════════════════════════════════════════
FACTOR_COLS = [
    ('IDLE',       'Idle',           'Normal',  None),
    ('PRE_LAYER',  'Layer Change',   '(없음)',  None),
    ('PRE_EQP_ID', '사전공정 장비',   '(없음)',  None),
    ('PRE_EQP_CH', '사전공정 챔버',   '(없음)',  None),
    ('EQP_ID',     '장비',           '(없음)',  None),
    ('WF_ID',      'WF 구간',        '(없음)',  5),      # 25장 → 5구간
]


def _sqlstr(v):
    """SQL 문자열 리터럴 (설정값 전용 — 사용자 입력에는 쓰지 말 것)"""
    return "'" + str(v).replace("'", "''") + "'"


def _num_expr(col):
    """텍스트로 저장된 값에서 숫자만 뽑아내는 식 ('01' → 1)"""
    return (f"""NULLIF(regexp_replace(CAST("{col}" AS TEXT), '[^0-9]', '', 'g'), '')"""
            f"""::numeric""")


def _bin_edges(cur, table, col, nbins):
    """테이블 전체 범위를 nbins 개의 같은 폭 구간으로 → [(lo, hi), ...]"""
    cur.execute(f'SELECT MIN(v), MAX(v) FROM (SELECT {_num_expr(col)} AS v FROM {table}) t')
    lo, hi = cur.fetchone()
    if lo is None or hi is None:
        return []
    lo, hi = int(lo), int(hi)
    step = max(1, ceil((hi - lo + 1) / nbins))

    edges, a = [], lo
    while a <= hi:
        b = min(a + step - 1, hi)
        edges.append((a, b))
        a = b + 1
    return edges


def _key_expr(col, empty_label, edges=None):
    """집계 키 SQL 식. edges 가 있으면 구간명으로 묶는다."""
    if edges:
        num = _num_expr(col)
        cases = " ".join(
            f"WHEN {num} BETWEEN {a} AND {b} THEN {_sqlstr(f'{a}~{b}')}"
            for a, b in edges)
        return f"COALESCE(CASE {cases} END, {_sqlstr(empty_label)})"
    # 숫자 컬럼이어도 NULLIF 가 동작하도록 TEXT 로 캐스팅
    return f"""COALESCE(NULLIF(CAST("{col}" AS TEXT), ''), {_sqlstr(empty_label)})"""


def _order_expr(col, edges=None):
    """구간은 번호순, 그 외는 건수순"""
    return f'MIN({_num_expr(col)})' if edges else 'COUNT(*) DESC'


# ══════════════════════════════════════════════════════════
# 드래그 선택 영역의 요인 분포
# ══════════════════════════════════════════════════════════
@csrf_exempt
def analysis_stats(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    ids     = body.get('ids', [])
    table   = _an_table(oper_id)

    if not ids:
        return JsonResponse({'count': 0, 'factors': []})

    try:
        with connections['analysis_db'].cursor() as cur:
            # 실제 존재하는 컬럼만 (없는 컬럼 참조로 인한 500 방지)
            cur.execute("""
                SELECT upper(column_name) FROM information_schema.columns
                WHERE table_name = %s
            """, [table])
            have = {r[0] for r in cur.fetchall()}

            ph = ",".join(["%s"] * len(ids))
            cur.execute(f'SELECT COUNT(*) FROM {table} WHERE id IN ({ph})', ids)
            count = cur.fetchone()[0]

            factors = []
            for col, label, empty_label, nbins in FACTOR_COLS:
                if col not in have:
                    continue

                edges = _bin_edges(cur, table, col, nbins) if nbins else None
                key   = _key_expr(col, empty_label, edges)
                order = _order_expr(col, edges)

                cur.execute(f'''
                    SELECT {key} AS k, COUNT(*)
                    FROM {table} WHERE id IN ({ph})
                    GROUP BY k ORDER BY {order}
                ''', ids)
                rows = [{'key': r[0], 'count': r[1]} for r in cur.fetchall()]
                factors.append({'col': col, 'label': label, 'rows': rows,
                                'binned': bool(edges)})

        return JsonResponse({'count': count, 'factors': factors})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ══════════════════════════════════════════════════════════
# 편중도 (LLM 요약용)
#   선택 영역에서의 비율이 전체보다 얼마나 높은가(lift)
#   예) 선택 85% vs 전체 32% → lift 2.7 → 이 조건에 몰려 있다
# ══════════════════════════════════════════════════════════
def _enrichment(cur, table, col, ids, lot_cd, ph, empty_label='(없음)', nbins=None):
    edges = _bin_edges(cur, table, col, nbins) if nbins else None
    key   = _key_expr(col, empty_label, edges)

    def counts(where, params):
        cur.execute(f'SELECT {key} AS k, COUNT(*) FROM {table} '
                    f'WHERE {where} GROUP BY k', params)
        return {r[0]: r[1] for r in cur.fetchall()}

    sel  = counts(f'id IN ({ph})', list(ids))
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


# ══════════════════════════════════════════════════════════
# _build_summary 안의 요인 루프도 아래처럼 바꿀 것
# ══════════════════════════════════════════════════════════
"""
        factors = []
        for col, label, empty_label, nbins in FACTOR_COLS:
            if col not in have:
                continue
            rows = _enrichment(cur, table, col, ids, lot_cd, ph, empty_label, nbins)
            factors.append({'col': col, 'label': label, 'rows': rows})
"""
