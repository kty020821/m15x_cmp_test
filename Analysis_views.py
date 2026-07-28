"""
════════════════════════════════════════════════════════════
equipment/views_analysis.py
────────────────────────────────────────────────────────────
산포 분석 페이지의 화면 + API 전체

  화면 흐름
    STEP 1  대상 선택   TECH → LOT_CD → OPER → PARAMETER
    STEP 2  구간 지정   트렌드에서 드래그 (상관 차트에 교차 표시)
    STEP 3  원인 분석   요인 편중 + AI 해석

  설계 원칙
    · 통계 계산은 전부 PostgreSQL 이 담당하고, 웹은 그리기만 한다
    · LLM 에는 원본 행을 넘기지 않는다. 코드가 만든 요약만 넘긴다

  [사용법]
    이 파일을 그대로 두고 views.py 에서 가져다 쓰거나,
    내용을 views.py 에 붙여넣어도 된다.
    별도 파일로 둘 경우 urls.py 에서 이 모듈을 import 할 것.

  [설정 지점]
    LEGEND_OPTIONS  차트 범례 후보
    FACTOR_COLS     요인 분포 항목
    LIFT_MIN        편중 판정 기준
    LLM_SYSTEM      LLM 프롬프트

    TECH / LOT_CD 목록과 공정명은 equipment/tech_map.py 에서 관리한다.
    OPER 드롭박스는 tech_map 의 공정명 + 적재된 테이블에서 자동 생성한다.
════════════════════════════════════════════════════════════
"""

import json
import re
from math import ceil

import requests
from django.conf import settings
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import connections

from . import tech_map


# ══════════════════════════════════════════════════════════
# 1. 기준정보
# ══════════════════════════════════════════════════════════
# ── 차트 범례 후보 ────────────────────────────────────────
#    ★ 항목 추가는 여기만 (PG 컬럼명과 일치해야 함)
LEGEND_OPTIONS = [
    ('EQP_ID',     '장비 ID'),
    ('RECIPE_ID',  'Recipe'),
    ('EQP_CH_ID',  'Chamber'),
    ('IDLE',       'Idle'),
    ('EQP_MODEL',  '장비 모델'),
    ('PRE_LAYER',  'Layer Change'),
    ('PRE_EQP_ID', '사전공정 장비'),
    ('PRE_EQP_CH', '사전공정 챔버'),
]

# ── 요인 분포 항목 ────────────────────────────────────────
#    (컬럼, 표시명, 빈값 표기, 구간 수)
#    구간 수를 주면 숫자로 보고 같은 폭의 구간으로 묶는다 (WF_ID 등).
#    ★ 요인 추가는 여기 한 줄. 테이블에 없는 컬럼은 자동으로 건너뜀
FACTOR_COLS = [
    ('IDLE',       'Idle',          'Normal',  None),
    ('PRE_LAYER',  'Layer Change',  '(없음)',  None),
    ('PRE_EQP_ID', '사전공정 장비',  '(없음)',  None),
    ('PRE_EQP_CH', '사전공정 챔버',  '(없음)',  None),
    ('EQP_ID',     '장비',          '(없음)',  None),
    ('WF_ID',      'WF 구간',       '(없음)',  5),      # 25장 → 5구간
]

# ── 측정값이 아닌 메타 컬럼 (PARAMETER 후보에서 제외) ──────
META_COLS = {
    'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
    'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
    'WF_ID', 'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY',
}

# PG 숫자 타입 (정확 일치로 판정 — 부분문자열 매칭은 오탐이 난다)
NUMERIC_TYPES = {
    'smallint', 'integer', 'bigint',
    'decimal', 'numeric', 'real', 'double precision',
}

# 편중 판정 기준
LIFT_MIN  = 1.5     # 전체 대비 몇 배 이상 몰려야 '편중' 으로 볼지
COUNT_MIN = 3       # 선택 영역 내 최소 건수 (우연 배제)


# ══════════════════════════════════════════════════════════
# 2. 공통 헬퍼
# ══════════════════════════════════════════════════════════
def _an_table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def _f(v):
    return round(float(v), 3) if v is not None else None


def _sqlstr(v):
    """SQL 문자열 리터럴 (설정값 전용 — 사용자 입력에는 쓰지 말 것)"""
    return "'" + str(v).replace("'", "''") + "'"


def _existing_cols(table):
    """테이블에 실제 존재하는 컬럼(대문자) 집합"""
    with connections['analysis_db'].cursor() as cur:
        cur.execute("""
            SELECT upper(column_name) FROM information_schema.columns
            WHERE table_name = %s
        """, [table])
        return {r[0] for r in cur.fetchall()}


def _fetch_numeric_cols(table):
    """숫자형 측정값 컬럼 목록 (메타 컬럼 제외)"""
    with connections['analysis_db'].cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = %s ORDER BY ordinal_position
        """, [table])
        rows = cur.fetchall()

    out = []
    for name, dtype in rows:
        up = name.upper()
        if up in META_COLS:
            continue
        if dtype.lower() in NUMERIC_TYPES:
            out.append(up)
    return out


def _factor_items():
    """FACTOR_COLS 정규화 — 구간 수를 생략한 3개짜리 항목도 허용"""
    out = []
    for item in FACTOR_COLS:
        col, label, empty = item[0], item[1], item[2]
        nbins = item[3] if len(item) > 3 else None
        out.append((col, label, empty, nbins))
    return out


# ── 요인 집계용 SQL 식 ────────────────────────────────────
def _num_expr(col):
    """텍스트로 저장된 값에서 숫자만 추출 ('01' → 1)"""
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
    """집계 키 SQL 식. edges 가 있으면 구간명으로 묶는다"""
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
# 3. 페이지
# ══════════════════════════════════════════════════════════
def analysis_page(request):
    return render(request, 'equipment/analysis.html', {
        'tech_list':      tech_map.all_techs(),
        'legend_options': LEGEND_OPTIONS,
    })


# ══════════════════════════════════════════════════════════
# 4. 종속 드롭박스 옵션
#    TECH  → tech_map.py
#    LOT_CD→ tech_map ∩ 실제 적재된 값
#    OPER  → tech_map 공정명 ∩ 적재된 테이블
#    PARAM → 테이블 숫자 컬럼
# ══════════════════════════════════════════════════════════

def _oper_names():
    """공정 코드 → 공정명. tech_map.OPER_NAME_MAP 에서 관리한다"""
    return tech_map.OPER_NAME_MAP


def _loaded_tables():
    """적재된 분석 테이블 이름 집합"""
    with connections['analysis_db'].cursor() as cur:
        cur.execute("""
            SELECT tablename FROM pg_tables
            WHERE tablename LIKE 'cmp_analysis_%'
        """)
        return {r[0] for r in cur.fetchall()}


def _oper_options():
    """
    적재된 공정만 [{'value': OPER_ID, 'label': '공정명 (OPER_ID)'}, ...]

    테이블명에서 OPER_ID 를 역산하지 않는다. _an_table() 이 특수문자를
    '_' 로 바꾸므로 역변환이 불가능하기 때문(V507-00E → v507_00e → V507_00E).
    등록된 OPER_ID 로 테이블명을 만들어 존재 여부를 확인하는 방향이 정확하다.
    """
    names  = _oper_names()          # {OPER_ID: 공정명}
    tables = _loaded_tables()

    out, used = [], set()

    # tech_map 에 등록된 공정 중 실제로 적재된 것
    for oid, desc in names.items():
        t = _an_table(oid)
        if t in tables:
            out.append({'value': oid,
                        'label': f'{desc} ({oid})' if desc else oid})
            used.add(t)

    # 적재는 됐는데 tech_map 에 이름이 없는 것 (코드만 표시)
    for t in sorted(tables - used):
        oid = t.replace('cmp_analysis_', '').upper()
        out.append({'value': oid, 'label': oid})

    out.sort(key=lambda o: o['label'])
    return out


def _lots_with_data():
    """적재된 테이블들에 실제 존재하는 LOT_CD (문자열로 통일)"""
    lots = set()
    with connections['analysis_db'].cursor() as cur:
        cur.execute("""
            SELECT tablename FROM pg_tables
            WHERE tablename LIKE 'cmp_analysis_%'
        """)
        for (t,) in cur.fetchall():
            try:
                cur.execute(f'SELECT DISTINCT "LOT_CD" FROM {t} '
                            f'WHERE "LOT_CD" IS NOT NULL')
                # 과거에 숫자로 저장된 테이블이 섞여 있어도 정렬이 깨지지 않게
                lots.update(str(r[0]) for r in cur.fetchall())
            except Exception:
                pass
    return lots


@csrf_exempt
def analysis_options(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body  = json.loads(request.body)
    level = body.get('level')

    if level == 'lot_cd':
        # 매핑에 등록됐고 실제 데이터도 있는 것만
        mapped  = tech_map.lots_of_tech(body.get('tech'))
        have    = _lots_with_data()
        options = [lc for lc in mapped if lc in have]

        # 데이터에는 있는데 tech_map 에 없는 = 미등록 device
        unmapped = sorted(lc for lc in have if tech_map.tech_of_lot(lc) is None)
        return JsonResponse({'options': options, 'unmapped': unmapped})

    if level == 'oper':
        return JsonResponse({'options': _oper_options()})

    if level == 'param':
        table = _an_table(body.get('oper_id'))
        try:
            return JsonResponse({'options': _fetch_numeric_cols(table)})
        except Exception as e:
            return JsonResponse({'options': [], 'error': str(e)})

    return JsonResponse({'options': []})


# ══════════════════════════════════════════════════════════
# 5. 트렌드 스캐터
# ══════════════════════════════════════════════════════════
def _sel_list(cols):
    """SELECT 절 조각 — 비어 있으면 빈 문자열 (콤마 중복 방지)"""
    return "".join(f', "{c}"' for c in cols)


@csrf_exempt
def analysis_trend(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    param   = body.get('param')
    table   = _an_table(oper_id)

    if not param or not re.match(r'^[0-9A-Za-z_]+$', param):
        return JsonResponse({'error': '잘못된 파라미터'}, status=400)

    try:
        have = _existing_cols(table)

        # 선택한 파라미터가 이 공정에 없으면 오류가 아니라 '데이터 없음'
        if param.upper() not in have:
            return JsonResponse({'data': [], 'param': param,
                                 'note': f'{param} 컬럼이 이 공정에 없습니다'})

        legend_cols = [c for c, _ in LEGEND_OPTIONS if c in have]
        extra_cols  = [c for c in ('LOT_ID', 'WF_ID') if c in have]

        sql = f'''
            SELECT id, "DATE", "{param}"{_sel_list(legend_cols)}{_sel_list(extra_cols)}
            FROM {table}
            WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL
            ORDER BY "DATE"
        '''
        with connections['analysis_db'].cursor() as cur:
            cur.execute(sql, [lot_cd])
            rows = cur.fetchall()

        n = len(legend_cols)
        data = []
        for r in rows:
            item = {
                'id':   r[0],
                'date': r[1].strftime('%Y-%m-%d %H:%M:%S') if r[1] else None,
                'val':  float(r[2]) if r[2] is not None else None,
            }
            for i, c in enumerate(legend_cols):
                item[c] = r[3 + i]
            for j, c in enumerate(extra_cols):
                item[c] = r[3 + n + j]
            data.append(item)

        return JsonResponse({'data': data, 'param': param})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ══════════════════════════════════════════════════════════
# 6. 상관 산점도 + R² + 추세선
#    범례로 항목을 걸러내면 화면에서 다시 계산한다 (analysis.html)
# ══════════════════════════════════════════════════════════
@csrf_exempt
def analysis_corr(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    x_col   = body.get('x_col')
    y_col   = body.get('y_col')
    table   = _an_table(oper_id)

    for c in (x_col, y_col):
        if not c or not re.match(r'^[0-9A-Za-z_]+$', c):
            return JsonResponse({'error': '잘못된 컬럼'}, status=400)

    try:
        have = _existing_cols(table)

        # 축 컬럼이 이 공정에 없으면 오류가 아니라 '데이터 없음'
        missing = [c for c in (x_col, y_col) if c.upper() not in have]
        if missing:
            return JsonResponse({
                'data': [], 'x_col': x_col, 'y_col': y_col,
                'r2': None, 'trend': None,
                'note': f"{', '.join(missing)} 컬럼이 이 공정에 없습니다",
            })

        legend_cols = [c for c, _ in LEGEND_OPTIONS if c in have]
        extra_cols  = [c for c in ('LOT_ID', 'WF_ID') if c in have]

        sql = f'''
            SELECT id, "{x_col}", "{y_col}"{_sel_list(legend_cols)}{_sel_list(extra_cols)}
            FROM {table}
            WHERE "LOT_CD" = %s AND "{x_col}" IS NOT NULL AND "{y_col}" IS NOT NULL
            ORDER BY "DATE"
        '''
        with connections['analysis_db'].cursor() as cur:
            cur.execute(sql, [lot_cd])
            rows = cur.fetchall()

            # 데이터가 없으면 회귀를 돌릴 필요도 없다
            if not rows:
                return JsonResponse({'data': [], 'x_col': x_col, 'y_col': y_col,
                                     'r2': None, 'trend': None})

            cur.execute(f'''
                SELECT CORR("{x_col}", "{y_col}"),
                       REGR_SLOPE("{y_col}", "{x_col}"),
                       REGR_INTERCEPT("{y_col}", "{x_col}"),
                       MIN("{x_col}"), MAX("{x_col}")
                FROM {table} WHERE "LOT_CD" = %s
            ''', [lot_cd])
            corr, slope, intercept, xmin, xmax = cur.fetchone()

        r2 = round(corr * corr, 4) if corr is not None else None

        trend = None
        # x 가 한 값뿐이면(xmin == xmax) 직선을 그릴 수 없다
        if (slope is not None and intercept is not None
                and xmin is not None and xmax is not None
                and float(xmin) != float(xmax)):
            trend = {
                'x': [float(xmin), float(xmax)],
                'y': [float(slope * xmin + intercept), float(slope * xmax + intercept)],
                'slope': round(float(slope), 5),
                'intercept': round(float(intercept), 5),
            }

        n = len(legend_cols)
        data = []
        for r in rows:
            item = {
                'id': r[0],
                'x':  float(r[1]) if r[1] is not None else None,
                'y':  float(r[2]) if r[2] is not None else None,
            }
            for i, c in enumerate(legend_cols):
                item[c] = r[3 + i]
            for j, c in enumerate(extra_cols):
                item[c] = r[3 + n + j]
            data.append(item)

        return JsonResponse({'data': data, 'x_col': x_col, 'y_col': y_col,
                             'r2': r2, 'trend': trend})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ══════════════════════════════════════════════════════════
# 7. 선택 구간의 요인 분포
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
            cur.execute("""
                SELECT upper(column_name) FROM information_schema.columns
                WHERE table_name = %s
            """, [table])
            have = {r[0] for r in cur.fetchall()}

            ph = ",".join(["%s"] * len(ids))
            cur.execute(f'SELECT COUNT(*) FROM {table} WHERE id IN ({ph})', ids)
            count = cur.fetchone()[0]

            factors = []
            for col, label, empty_label, nbins in _factor_items():
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
# 8. 편중도 · 상관 · 요약
#    LLM 이 해석할 재료를 코드가 계산한다
# ══════════════════════════════════════════════════════════
def _enrichment(cur, table, col, ids, lot_cd, ph, empty_label='(없음)', nbins=None):
    """
    한 요인의 편중도.
      선택 구간에서의 비율이 전체에서의 비율보다 얼마나 높은가(lift).
      예) 선택 85% vs 전체 32% → 2.7배 → 이 조건에 몰려 있다
    """
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


def _top_correlations(cur, table, param, lot_cd, ids, ph, top_n=5):
    """'무엇과 함께 움직였나' — 원인 추론의 강한 단서"""
    num_cols = [c for c in _fetch_numeric_cols(table) if c != param]
    if not num_cols:
        return []

    corr_sel = ", ".join(f'CORR("{param}", "{c}")' for c in num_cols)
    cur.execute(f'SELECT {corr_sel} FROM {table} WHERE "LOT_CD" = %s', [lot_cd])
    crow = cur.fetchone()

    ranked = [(c, crow[i]) for i, c in enumerate(num_cols) if crow[i] is not None]
    ranked.sort(key=lambda x: abs(x[1]), reverse=True)
    top = ranked[:top_n]
    if not top:
        return []

    cols = [c for c, _ in top]
    agg  = ", ".join(f'AVG("{c}")' for c in cols)
    cur.execute(f'SELECT {agg} FROM {table} WHERE id IN ({ph})', list(ids))
    srow = cur.fetchone()
    cur.execute(f'SELECT {agg} FROM {table} WHERE "LOT_CD" = %s', [lot_cd])
    arow = cur.fetchone()

    return [{
        'col': c,
        'r2': round(v * v, 3),
        'dir': '같은 방향' if v > 0 else '반대 방향',
        'sel_avg': _f(srow[i]),
        'all_avg': _f(arow[i]),
    } for i, (c, v) in enumerate(top)]


def _build_summary(oper_id, lot_cd, param, ids):
    """LLM 에 넘길 구조화 요약 (원본 행은 포함하지 않는다)"""
    table = _an_table(oper_id)
    ph    = ",".join(["%s"] * len(ids))

    with connections['analysis_db'].cursor() as cur:
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

        factors = []
        for col, label, empty_label, nbins in _factor_items():
            if col not in have:
                continue
            rows = _enrichment(cur, table, col, ids, lot_cd, ph, empty_label, nbins)
            factors.append({'col': col, 'label': label, 'rows': rows})

        corr = _top_correlations(cur, table, param, lot_cd, ids, ph)

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
        'corr': corr,
    }


def _summary_to_text(s):
    """
    프롬프트용 텍스트.
    무엇이 포함됐고 무엇이 없는지를 명시해, 모델이 없는 정보를
    임의로 끌어오는 것을 막는다.
    """
    L = []
    L.append("=== 데이터 ===")
    L.append(f"공정 {s['oper_id']} / 제품 {s['lot_cd']} / 분석 파라미터 {s['param']}")
    L.append("")

    t   = s['target']
    dev = t.get('dev_pct')
    if dev is None:
        direction = ''
    elif dev < 0:
        direction = ' → 선택 구간이 전체보다 낮음'
    elif dev > 0:
        direction = ' → 선택 구간이 전체보다 높음'
    else:
        direction = ''

    L.append("[선택 구간 vs 전체]")
    L.append(f"  웨이퍼 수 : 선택 {t['sel']['n']}장 / 전체 {t['all']['n']}장")
    L.append(f"  평균      : 선택 {t['sel']['avg']} / 전체 {t['all']['avg']}"
             f" (편차 {dev}%){direction}")
    L.append(f"  중앙값    : 선택 {t['sel']['med']} / 전체 {t['all']['med']}")
    L.append(f"  표준편차  : 선택 {t['sel']['std']} / 전체 {t['all']['std']}")
    L.append("")

    flagged = []
    for f in s['factors']:
        for r in [r for r in f['rows'] if r['flag']][:3]:
            flagged.append(
                f"  {f['label']} = {r['key']} : 선택 {r['sel_pct']}% / 전체 {r['all_pct']}%"
                f" → {r['lift']}배 집중 ({r['sel_n']}장)")

    if flagged:
        L.append("[편중된 조건] 전체 대비 선택 구간에 몰려 있는 항목")
        L.extend(flagged)
    else:
        L.append("[편중된 조건] 없음")
        L.append("  검사한 모든 요인에서 전체 대비 유의하게 몰린 조건이 발견되지 않음")
    L.append("")

    L.append(f"[검사한 요인] {', '.join(f['label'] for f in s['factors'])}")
    L.append("")

    if s.get('corr'):
        L.append("[함께 움직인 파라미터] 전체 데이터 기준 상관 상위")
        for c in s['corr']:
            L.append(f"  {c['col']} : R² {c['r2']} ({c['dir']}),"
                     f" 선택 평균 {c['sel_avg']} / 전체 평균 {c['all_avg']}")
        L.append("")

    absent = []
    if not s.get('corr'):
        absent.append("파라미터 간 상관")
    absent.append("소모품(Pad/Head/Disk) 수명")
    absent.append("압력·회전수·슬러리 등 위에 나열되지 않은 모든 설비 조건")
    absent.append("스펙 상하한, 목표값")

    L.append("[이 요약에 없는 정보] 아래는 제공되지 않았으므로 언급하지 말 것")
    for a in absent:
        L.append(f"  - {a}")

    return "\n".join(L)


# ══════════════════════════════════════════════════════════
# 9. LLM 해석
# ══════════════════════════════════════════════════════════
LLM_SYSTEM = """당신은 반도체 CMP 공정 엔지니어를 돕는 데이터 분석 보조자입니다.
엔지니어가 트렌드 차트에서 이상 구간을 선택했고, 그 구간의 통계 요약이 주어집니다.

# 절대 규칙 (다른 무엇보다 우선)

1. **주어진 [데이터]에 실제로 적힌 수치만 사용합니다.**
   데이터에 없는 항목(예: 압력, 온도, 슬러리, 소모품 수명 등)은
   그 항목이 데이터에 등장하지 않는 한 절대 언급하지 마세요.

2. **모든 주장 끝에는 근거 수치를 괄호로 인용합니다.**
   예: "5CMP1E21 장비에 몰려 있습니다 (선택 85% vs 전체 32%, 2.7배)"
   인용할 수치가 없으면 그 주장은 쓰지 마세요.

3. **아래 배경지식은 데이터에 해당 항목이 있을 때만 해석에 사용합니다.**
   데이터에 없는 원인을 배경지식에서 끌어와 추측하지 마세요.

4. **편중된 조건이 없으면** "특정 조건에 몰려 있지 않다"고 명시하고,
   억지로 원인을 만들지 마세요. 그 경우 산발적 편차 가능성과
   추가로 확보해야 할 데이터를 제안하는 것이 올바른 답변입니다.

5. 상관관계를 인과로 단정하지 마세요. "~와 함께 움직였다" 로 표현합니다.

# 참고 배경지식 (해당 항목이 데이터에 있을 때만 적용)

- 두께가 목표보다 **낮다** = 과연마. 제거율이 높았다는 뜻.
- 두께가 목표보다 **높다** = 미연마. 제거율이 낮았다는 뜻.

- **장비/챔버 편중** → 해당 설비의 상태(부품 마모, 셋업 편차)를 우선 의심
- **사전공정(장비·Layer) 편중** → 입고 편차일 수 있으므로,
  CMP 조건을 바꾸기 전에 입고 두께부터 확인
- **Idle 편중** (idle_1~4 는 idle 직후 N번째 웨이퍼) →
  재가동 초기 패드 온도·수분 상태가 정상과 달라 제거율이 흔들림
- **Layer Change 편중** → 직전에 다른 layer 를 돌린 영향
- **WF 구간 편중**
  - 앞번호(1~5): lot 초반. 장비 예열·안정화 문제
  - 뒷번호(21~25): 연속 처리로 패드 온도 상승, slurry 누적
  - 특정 구간 편중 없음: 웨이퍼 순서와 무관

# 출력 형식

## 진단
무엇이 어떻게 벗어났는지 2~3줄. 반드시 수치 인용.

## 유력 원인
데이터에서 근거를 찾을 수 있는 것만, 가능성 높은 순으로 최대 3개.
근거가 하나도 없으면 이 항목에 "데이터상 뚜렷한 원인 신호 없음" 이라고만 쓰세요.

## 조치 방안
원인별 구체적 조치. 즉시 조치와 추가 확인이 필요한 것을 구분.
원인이 불명확하면 무엇을 더 봐야 하는지를 쓰세요.

# 작성 후 자체 점검
답변을 쓴 뒤, 각 문장에 인용한 수치가 [데이터]에 실제로 있는지 확인하세요.
없는 수치를 썼다면 그 문장을 지우세요. 한국어로, 서론 없이 간결하게."""


def _call_company_llm(system, user):
    """
    사내 LLM 호출 (OpenAI 호환 형식).
    settings 에 LLM_URL / LLM_MODEL / LLM_API_KEY 가 있어야 한다.

    ※ 게이트웨이가 system 역할이나 temperature 를 거부하면
      messages 를 user 하나로 합치고 temperature 를 빼면 된다.
    """
    resp = requests.post(
        settings.LLM_URL + '/chat/completions',
        json={
            'model': settings.LLM_MODEL,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user',   'content': user},
            ],
            'temperature': 0.2,      # 분석이므로 낮게 — 같은 데이터면 같은 답
        },
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {settings.LLM_API_KEY}',
        },
        timeout=getattr(settings, 'LLM_TIMEOUT', 60),
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']


@csrf_exempt
def analysis_insight(request):
    """
    선택 구간의 편중도를 계산하고 LLM 해석을 덧붙인다.
    LLM 이 실패해도 편중도 요약(prompt)은 그대로 반환한다.
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
        return JsonResponse({'error': '선택된 구간이 없습니다'}, status=400)
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
                llm_error = '사내 LLM API가 연결되지 않았습니다.'
            except Exception as e:
                llm_error = f'LLM 호출 실패: {e}'

        return JsonResponse({
            'summary':   summary,     # 화면 강조용 (편중 항목 표시)
            'prompt':    text,        # 무엇을 넘겼는지 확인용
            'answer':    answer,
            'llm_error': llm_error,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ══════════════════════════════════════════════════════════
# 10. 대화형 분석
#     선택 구간에 대해 자유롭게 묻고 이어서 물어볼 수 있게 한다.
#
#     질문에 나온 파라미터를 찾아 그 통계를 코드가 계산한 뒤 넘긴다.
#     LLM 은 계산된 수치를 읽고 설명만 한다 — 값을 만들어내지 않는다.
# ══════════════════════════════════════════════════════════
# ── 대화 설정 ─────────────────────────────────────────────
CHAT_MAX_PARAMS  = 3     # 한 질문에서 통계를 계산할 파라미터 최대 개수
CHAT_HISTORY_MAX = 6     # LLM 에 넘길 이전 대화 수 (질문+답변 = 2)
HIST_BINS        = 8     # 분포 구간 개수


# ══════════════════════════════════════════════════════════
# 질문에서 파라미터 찾기
# ══════════════════════════════════════════════════════════
def _mentioned_params(question, table, base_param=None):
    """
    질문에 등장하는 파라미터명을 실제 컬럼과 대조해 찾는다.

    긴 이름부터 확인해 부분 일치로 인한 오탐을 줄인다.
    (THK 와 THK_AVG 가 모두 있을 때 THK_AVG 를 우선)
    """
    cols = _fetch_numeric_cols(table)
    q    = question.upper()

    hits = []
    for c in sorted(cols, key=len, reverse=True):
        if c in q and not any(c in h for h in hits):
            hits.append(c)
        if len(hits) >= CHAT_MAX_PARAMS:
            break

    # 아무것도 못 찾으면 현재 보고 있는 파라미터를 기본으로
    if not hits and base_param and base_param in cols:
        hits = [base_param]
    return hits


# ══════════════════════════════════════════════════════════
# 파라미터 분포 통계
# ══════════════════════════════════════════════════════════
def _param_stats(cur, table, param, lot_cd, ids, ph):
    """선택 구간 vs 전체의 분포 (사분위 포함)"""
    def agg(where, params):
        cur.execute(f'''
            SELECT COUNT("{param}"), AVG("{param}"), STDDEV("{param}"),
                   MIN("{param}"), MAX("{param}"),
                   PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY "{param}"),
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY "{param}"),
                   PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY "{param}")
            FROM {table} WHERE {where} AND "{param}" IS NOT NULL
        ''', params)
        n, avg, std, mn, mx, q1, med, q3 = cur.fetchone()
        return {'n': n or 0, 'avg': _f(avg), 'std': _f(std),
                'min': _f(mn), 'max': _f(mx),
                'q1': _f(q1), 'med': _f(med), 'q3': _f(q3)}

    sel = agg(f'id IN ({ph})', list(ids))
    alw = agg('"LOT_CD" = %s', [lot_cd])

    return {'param': param, 'sel': sel, 'all': alw,
            'hist': _param_hist(cur, table, param, lot_cd, ids, ph, alw)}


def _param_hist(cur, table, param, lot_cd, ids, ph, alw):
    """
    전체 범위를 같은 폭으로 나눠 선택 구간의 도수를 센다.
    분포가 한쪽에 몰렸는지, 두 덩어리로 갈렸는지를 보기 위함.
    """
    if alw['min'] is None or alw['max'] is None:
        return []
    lo, hi = float(alw['min']), float(alw['max'])
    if hi <= lo:
        return []

    width = (hi - lo) / HIST_BINS
    cur.execute(f'''
        SELECT width_bucket("{param}", %s, %s, %s) AS b, COUNT(*)
        FROM {table} WHERE id IN ({ph}) AND "{param}" IS NOT NULL
        GROUP BY b ORDER BY b
    ''', [lo, hi, HIST_BINS] + list(ids))
    counts = {r[0]: r[1] for r in cur.fetchall()}

    out = []
    for b in range(1, HIST_BINS + 1):
        a = lo + width * (b - 1)
        z = lo + width * b
        # width_bucket 은 상한 초과를 HIST_BINS+1 로 반환한다
        n = counts.get(b, 0) + (counts.get(HIST_BINS + 1, 0) if b == HIST_BINS else 0)
        out.append({'lo': round(a, 3), 'hi': round(z, 3), 'n': n})
    return out


def _param_by_eqp(cur, table, param, lot_cd, ids, ph):
    """선택 구간의 장비별 평균 (어느 장비가 다른지 확인용)"""
    cur.execute(f'''
        SELECT "EQP_ID", COUNT(*), AVG("{param}")
        FROM {table} WHERE id IN ({ph}) AND "{param}" IS NOT NULL
        GROUP BY "EQP_ID" ORDER BY AVG("{param}") DESC
    ''', list(ids))
    return [{'eqp': r[0], 'n': r[1], 'avg': _f(r[2])} for r in cur.fetchall()]


def _stats_to_text(st, by_eqp=None):
    """계산 결과 → LLM 이 읽을 텍스트"""
    L = [f"[{st['param']}]"]
    s, a = st['sel'], st['all']

    L.append(f"  선택 구간 {s['n']}장 : 평균 {s['avg']}, 표준편차 {s['std']}, "
             f"범위 {s['min']}~{s['max']}")
    L.append(f"    사분위 Q1 {s['q1']} / 중앙값 {s['med']} / Q3 {s['q3']}")
    L.append(f"  전체 {a['n']}장 : 평균 {a['avg']}, 표준편차 {a['std']}, "
             f"범위 {a['min']}~{a['max']}")
    L.append(f"    사분위 Q1 {a['q1']} / 중앙값 {a['med']} / Q3 {a['q3']}")

    if st['hist']:
        L.append("  선택 구간 분포 (전체 범위를 8구간으로 나눔)")
        for h in st['hist']:
            bar = '■' * min(h['n'], 30)
            L.append(f"    {h['lo']} ~ {h['hi']} : {h['n']}장 {bar}")

    if by_eqp:
        L.append("  선택 구간 장비별 평균")
        for e in by_eqp:
            L.append(f"    {e['eqp']} : {e['avg']} ({e['n']}장)")

    return "\n".join(L)


# ══════════════════════════════════════════════════════════
# 대화용 프롬프트
# ══════════════════════════════════════════════════════════
CHAT_SYSTEM = """당신은 반도체 CMP 공정 엔지니어의 데이터 분석을 돕는 보조자입니다.
엔지니어가 트렌드 차트에서 특정 구간을 선택했고, 그 구간에 대해 질문합니다.

# 절대 규칙
1. 주어진 [데이터]에 적힌 수치만 사용하세요. 없는 값은 만들지 마세요.
2. 데이터에 없는 항목을 물으면 "그 정보는 제공되지 않았습니다"라고 답하고,
   무엇을 보면 알 수 있는지 알려주세요.
3. 수치를 인용할 때는 괄호로 함께 적으세요.
4. 상관관계를 인과로 단정하지 마세요.

# 해석 참고 (데이터에 해당 항목이 있을 때만)
- 두께가 목표보다 낮다 = 과연마(제거율이 높았다), 높다 = 미연마
- 분포가 두 덩어리로 갈리면 조건이 섞여 있다는 신호 (장비·챔버·레시피 확인)
- 선택 구간의 표준편차가 전체보다 크면 그 구간에서 산포가 더 벌어진 것
- 장비별 평균이 한 대만 동떨어지면 그 설비 상태를 우선 의심

# 답변 방식
- 질문에 직접 답하세요. 서론과 요약 반복은 생략합니다.
- 짧게, 한국어로. 필요하면 3~4줄이면 충분합니다.
- 데이터에서 눈에 띄는 점이 있으면 한 줄로 덧붙이세요."""


# ══════════════════════════════════════════════════════════
# 대화 API
# ══════════════════════════════════════════════════════════
@csrf_exempt
def analysis_chat(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body     = json.loads(request.body)
    oper_id  = body.get('oper_id')
    lot_cd   = body.get('lot_cd')
    param    = body.get('param')
    ids      = body.get('ids', [])
    question = (body.get('question') or '').strip()
    history  = body.get('history', [])

    if not ids:
        return JsonResponse({'error': '선택된 구간이 없습니다'}, status=400)
    if not question:
        return JsonResponse({'error': '질문이 비어 있습니다'}, status=400)

    table = _an_table(oper_id)
    ph    = ",".join(["%s"] * len(ids))

    try:
        # ── 질문에 나온 파라미터의 통계를 계산 ──────────────
        targets = _mentioned_params(question, table, param)

        blocks = []
        with connections['analysis_db'].cursor() as cur:
            have = _existing_cols(table)
            for p in targets:
                st  = _param_stats(cur, table, p, lot_cd, ids, ph)
                eqp = (_param_by_eqp(cur, table, p, lot_cd, ids, ph)
                       if 'EQP_ID' in have else None)
                blocks.append(_stats_to_text(st, eqp))

        ctx = [f"공정 {oper_id} / 제품 {lot_cd} / 선택 웨이퍼 {len(ids)}장", ""]
        if blocks:
            ctx.append("=== 계산 결과 ===")
            ctx.extend(blocks)
        else:
            ctx.append("(질문에서 파라미터를 찾지 못해 통계를 계산하지 않았습니다)")

        # 이 요약에 없는 것을 분명히 해 둔다
        ctx.append("")
        ctx.append("[이 데이터에 없는 정보] 위에 나열되지 않은 파라미터,")
        ctx.append("스펙 상하한, 소모품 수명, 설비 조건은 제공되지 않았습니다.")

        # ── 대화 이력 + 이번 질문 ─────────────────────────
        messages = [{'role': 'system', 'content': CHAT_SYSTEM}]
        for h in history[-CHAT_HISTORY_MAX:]:
            role = 'assistant' if h.get('role') == 'assistant' else 'user'
            messages.append({'role': role, 'content': str(h.get('content', ''))})
        messages.append({
            'role': 'user',
            'content': "\n".join(ctx) + f"\n\n=== 질문 ===\n{question}",
        })

        answer, llm_error = None, None
        try:
            answer = _call_llm_messages(messages)
        except Exception as e:
            llm_error = f'LLM 호출 실패: {e}'

        return JsonResponse({
            'answer':    answer,
            'llm_error': llm_error,
            'params':    targets,              # 어떤 파라미터를 봤는지 화면 표시용
            'context':   "\n".join(ctx),       # 무엇을 넘겼는지 확인용
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def _call_llm_messages(messages):
    """
    messages 배열을 그대로 보내는 호출 (대화 이력 유지용).

    ※ 게이트웨이가 system 역할을 거부하면 첫 메시지를 user 로 합칠 것.
    """
    resp = requests.post(
        settings.LLM_URL + '/chat/completions',
        json={
            'model': settings.LLM_MODEL,
            'messages': messages,
            'temperature': 0.2,
        },
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {settings.LLM_API_KEY}',
        },
        timeout=getattr(settings, 'LLM_TIMEOUT', 60),
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']
