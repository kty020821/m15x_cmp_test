"""
equipment/analysis_views.py
Analysis 페이지 뷰 + API

[urls.py에 추가]
    path('analysis/',                 views.analysis_page,      name='analysis'),
    path('api/analysis/options/',     views.analysis_options,   name='analysis-options'),
    path('api/analysis/trend/',       views.analysis_trend,     name='analysis-trend'),
    path('api/analysis/stats/',       views.analysis_stats,     name='analysis-stats'),

[views.py에서 import]
    from .analysis_views import (analysis_page, analysis_options,
                                 analysis_trend, analysis_stats)
"""
import json
import re
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import connections

# ── 기준정보 (analysis_config.py 에서 관리) ──────────────
# 지금은 파일럿이라 여기 직접. 실제로는 config에서 import.
TECH_LOT_MAP = {
    'LUCY': ['5E2', '5E9'],
}
# 공정 목록 (oper_id, 표시명). 실제로는 OPER_LIST 사용
OPER_LIST = [
    ('OP100', 'BLC BUF NIT TOUCH CMP'),
]

# ── 범례 후보 컬럼 ───────────────────────────────────────
# ★ 범례 항목 추가/변경하려면 이 리스트만 수정하면 됨
#   (컬럼명, 화면표시명) — 컬럼명은 PG 테이블 컬럼과 일치해야 함
LEGEND_OPTIONS = [
    ('EQP_ID',    '장비 ID'),
    ('RECIPE_ID', 'Recipe'),
    ('EQP_CH_ID', 'Chamber'),
    ('IDLE',      'Idle/Layer'),
    ('EQP_MODEL', '장비 모델'),
]

# ── 측정값(파라미터)이 아닌 메타 컬럼 (드롭박스 param 후보에서 제외) ──
META_COLS = {
    'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
    'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
    'WF_ID', 'IDLE',
}


def _table_name(oper_id):
    name = re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()
    return f"cmp_analysis_{name}"


def analysis_page(request):
    """Analysis 페이지 (초기 진입) - Tech 목록만 넘김"""
    ctx = {
        'tech_list':      list(TECH_LOT_MAP.keys()),
        'legend_options': LEGEND_OPTIONS,
    }
    return render(request, 'equipment/analysis.html', ctx)


@csrf_exempt
def analysis_options(request):
    """
    종속 드롭박스 옵션 반환.
    단계: tech → lot_cd → oper → param
    요청 body: {level, tech, lot_cd, oper_id}
    """
    body   = json.loads(request.body)
    level  = body.get('level')

    if level == 'lot_cd':
        tech = body.get('tech')
        return JsonResponse({'options': TECH_LOT_MAP.get(tech, [])})

    if level == 'oper':
        # 공정 목록 (표시명 + oper_id). 이름 겹치면 oper_id 괄호로 구분
        opts = [{'value': oid, 'label': f"{desc} ({oid})"} for oid, desc in OPER_LIST]
        return JsonResponse({'options': opts})

    if level == 'param':
        # 선택 oper의 테이블에서 측정값 컬럼만 추출
        oper_id = body.get('oper_id')
        table   = _table_name(oper_id)
        conn    = connections['analysis_db']
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = %s ORDER BY ordinal_position
                """, [table])
                cols = [r[0].upper() for r in cur.fetchall()]
            params = [c for c in cols if c not in META_COLS]
            return JsonResponse({'options': params})
        except Exception as e:
            return JsonResponse({'options': [], 'error': str(e)})

    return JsonResponse({'options': []})


@csrf_exempt
def analysis_trend(request):
    """
    트렌드 스캐터 데이터.
    요청: {oper_id, lot_cd, param}
    반환: [{date, val, EQP_ID, RECIPE_ID, EQP_CH_ID, IDLE, EQP_MODEL, LOT_ID, WF_ID, id}]
    """
    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    param   = body.get('param')
    table   = _table_name(oper_id)

    if not param or not re.match(r'^[0-9A-Za-z_]+$', param):
        return JsonResponse({'error': '잘못된 param'}, status=400)

    conn = connections['analysis_db']
    # 범례 컬럼들도 같이 (색상 구분용)
    legend_cols = [c for c, _ in LEGEND_OPTIONS]
    sel_cols = ['id', 'DATE', f'"{param}"'] + [f'"{c}"' for c in legend_cols] + ['"LOT_ID"', '"WF_ID"']

    sql = f"""
        SELECT id, "DATE", "{param}", {', '.join(f'"{c}"' for c in legend_cols)},
               "LOT_ID", "WF_ID"
        FROM {table}
        WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL
        ORDER BY "DATE"
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [lot_cd])
            rows = cur.fetchall()

        data = []
        for r in rows:
            data.append({
                'id':        r[0],
                'date':      r[1].strftime('%Y-%m-%d %H:%M:%S') if r[1] else None,
                'val':       float(r[2]) if r[2] is not None else None,
                'EQP_ID':    r[3],
                'RECIPE_ID': r[4],
                'EQP_CH_ID': r[5],
                'IDLE':      r[6],
                'EQP_MODEL': r[7],
                'LOT_ID':    r[8],
                'WF_ID':     r[9],
            })
        return JsonResponse({'data': data, 'param': param})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
def analysis_stats(request):
    """
    드래그 선택 영역 통계.
    요청: {oper_id, ids: [선택된 점 id들]}
    반환: 선택 점들의 모든 측정값 컬럼 통계(평균/표준편차/개수) + IDLE 분포
    → 통계는 PG(SQL)가 계산 (512MB 웹 회피)
    """
    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    ids     = body.get('ids', [])
    table   = _table_name(oper_id)

    if not ids:
        return JsonResponse({'stats': {}, 'count': 0})

    conn = connections['analysis_db']
    try:
        # 측정값 컬럼 목록
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s ORDER BY ordinal_position
            """, [table])
            all_cols = [r[0].upper() for r in cur.fetchall()]
        val_cols = [c for c in all_cols if c not in META_COLS]

        # 각 측정값 컬럼의 평균/표준편차/개수를 SQL로 계산
        agg_parts = []
        for c in val_cols:
            agg_parts.append(f'AVG("{c}") AS "{c}__avg"')
            agg_parts.append(f'STDDEV("{c}") AS "{c}__std"')
        agg_sql = ", ".join(agg_parts)

        placeholders = ",".join(["%s"] * len(ids))
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*), {agg_sql}
                FROM {table} WHERE id IN ({placeholders})
            """, ids)
            row = cur.fetchone()

            # IDLE 분포 (idle/layer change 요인 파악)
            cur.execute(f"""
                SELECT "IDLE", COUNT(*) FROM {table}
                WHERE id IN ({placeholders})
                GROUP BY "IDLE" ORDER BY COUNT(*) DESC
            """, ids)
            idle_dist = [{'idle': r[0] or '(정상)', 'count': r[1]} for r in cur.fetchall()]

        count = row[0]
        stats = {}
        idx = 1
        for c in val_cols:
            avg = row[idx]; std = row[idx+1]; idx += 2
            stats[c] = {
                'avg': round(float(avg), 3) if avg is not None else None,
                'std': round(float(std), 3) if std is not None else None,
            }

        return JsonResponse({'count': count, 'stats': stats, 'idle_dist': idle_dist})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
