"""
════════════════════════════════════════════════════════════
equipment/views.py 수정 3곳
  ① LEGEND_OPTIONS  — 사전공정 장비/챔버 추가
  ② FACTOR_COLS     — 신규 (요인 분포 항목을 설정으로 관리)
  ③ analysis_stats  — FACTOR_COLS 기반으로 일반화
════════════════════════════════════════════════════════════
"""

# ──────────────────────────────────────────────────────────
# ① 범례 후보
#    ★ 범례 항목 추가/변경은 여기만 수정 (PG 컬럼명과 일치해야 함)
# ──────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────
# ② 요인 분포 항목  (컬럼, 표시명, 빈값 표기)
#    ★ 요인 추가는 여기 한 줄만 넣으면 됨
#      테이블에 없는 컬럼은 자동으로 건너뛴다
# ──────────────────────────────────────────────────────────
FACTOR_COLS = [
    ('IDLE',       'Idle',          'Normal'),
    ('PRE_LAYER',  'Layer Change',  '(없음)'),
    ('PRE_EQP_ID', '사전공정 장비',  '(없음)'),
    ('PRE_EQP_CH', '사전공정 챔버',  '(없음)'),
    ('EQP_ID',     '장비',          '(없음)'),
]


# ──────────────────────────────────────────────────────────
# ③ 드래그 선택 영역의 요인 분포
#    반환: {count, factors: [{col, label, rows: [{key, count}]}]}
# ──────────────────────────────────────────────────────────
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
            # 실제 존재하는 컬럼만 조회 (없는 컬럼 참조로 인한 500 방지)
            cur.execute("""
                SELECT upper(column_name) FROM information_schema.columns
                WHERE table_name = %s
            """, [table])
            have = {r[0] for r in cur.fetchall()}

            ph = ",".join(["%s"] * len(ids))
            cur.execute(f'SELECT COUNT(*) FROM {table} WHERE id IN ({ph})', ids)
            count = cur.fetchone()[0]

            factors = []
            for col, label, empty_label in FACTOR_COLS:
                if col not in have:
                    continue
                # 숫자 컬럼이어도 NULLIF 가 동작하도록 TEXT 로 캐스팅
                cur.execute(f'''
                    SELECT COALESCE(NULLIF(CAST("{col}" AS TEXT), ''), %s) AS k,
                           COUNT(*)
                    FROM {table} WHERE id IN ({ph})
                    GROUP BY k ORDER BY COUNT(*) DESC
                ''', [empty_label] + list(ids))
                rows = [{'key': r[0], 'count': r[1]} for r in cur.fetchall()]
                factors.append({'col': col, 'label': label, 'rows': rows})

        return JsonResponse({'count': count, 'factors': factors})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
