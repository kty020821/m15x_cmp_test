
"""
════════════════════════════════════════════════════════════
equipment/views.py 수정 3곳
════════════════════════════════════════════════════════════
"""

# ──────────────────────────────────────────────────────────
# ① LEGEND_OPTIONS — 'Idle/Layer' → 'Idle'
#    (기존 LEGEND_OPTIONS 를 아래로 교체)
# ──────────────────────────────────────────────────────────
LEGEND_OPTIONS = [
    ('EQP_ID',    '장비 ID'),
    ('RECIPE_ID', 'Recipe'),
    ('EQP_CH_ID', 'Chamber'),
    ('IDLE',      'Idle'),
    ('EQP_MODEL', '장비 모델'),
    ('PRE_LAYER', '사전 공정'),
]


# ──────────────────────────────────────────────────────────
# ② analysis_stats — IDLE 의 빈 값은 'Normal' 로 표기
#    (기존 analysis_stats 를 아래로 교체)
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
        return JsonResponse({'count': 0, 'idle_dist': [],
                             'layer_dist': [], 'eqp_dist': []})

    try:
        with connections['analysis_db'].cursor() as cur:
            # 존재하는 컬럼만 조회 (없는 컬럼 참조로 인한 500 방지)
            cur.execute("""
                SELECT upper(column_name) FROM information_schema.columns
                WHERE table_name = %s
            """, [table])
            have = {r[0] for r in cur.fetchall()}

            ph = ",".join(["%s"] * len(ids))
            cur.execute(f'SELECT COUNT(*) FROM {table} WHERE id IN ({ph})', ids)
            count = cur.fetchone()[0]

            def dist(col, empty_label='(없음)'):
                """col 의 값 분포. 빈 값은 empty_label 로 표기"""
                if col not in have:
                    return []
                cur.execute(f'''
                    SELECT COALESCE(NULLIF(CAST("{col}" AS TEXT), ''), %s) AS k,
                           COUNT(*)
                    FROM {table} WHERE id IN ({ph})
                    GROUP BY k ORDER BY COUNT(*) DESC
                ''', [empty_label] + list(ids))
                return [{'key': r[0], 'count': r[1]} for r in cur.fetchall()]

            idle_dist  = dist('IDLE', 'Normal')      # ← idle 없는 웨이퍼 = 정상
            layer_dist = dist('PRE_LAYER')
            eqp_dist   = dist('EQP_ID')

        return JsonResponse({'count': count, 'idle_dist': idle_dist,
                             'layer_dist': layer_dist, 'eqp_dist': eqp_dist})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ──────────────────────────────────────────────────────────
# ③ analysis_corr_rank — Top 5 → Top 15
#    (기존 analysis_corr_rank 안에서 아래 한 줄만 수정)
#
#      before:  top5     = r2[:5]
#      after :  top5     = r2[:15]
#
#    ※ 변수명은 그대로 두어도 무방. 아래는 전체 교체본.
# ──────────────────────────────────────────────────────────
TOP_N = 15


@csrf_exempt
def analysis_corr_rank(request):
    """트렌드 param 기준 R² 상위 N개 + 각 변수의 전체 통계"""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    base    = body.get('base_col')
    table   = _an_table(oper_id)

    if not base or not re.match(r'^[0-9A-Za-z_]+$', base):
        return JsonResponse({'error': '잘못된 base_col'}, status=400)

    try:
        num_cols = [c for c in _fetch_numeric_cols(table) if c != base]
        if not num_cols:
            return JsonResponse({'base': base, 'ranked': []})

        corr_parts = [f'CORR("{base}", "{c}")' for c in num_cols]
        with connections['analysis_db'].cursor() as cur:
            cur.execute(
                f'SELECT {", ".join(corr_parts)} FROM {table} WHERE "LOT_CD" = %s',
                [lot_cd])
            crow = cur.fetchone()

        r2 = []
        for i, c in enumerate(num_cols):
            v = crow[i]
            if v is not None:
                r2.append((c, round(v * v, 4)))
        r2.sort(key=lambda x: x[1], reverse=True)

        top      = r2[:TOP_N]
        top_cols = [c for c, _ in top]

        box_all = {}
        if top_cols:
            sql = _box_stats_sql(top_cols, f'{table} WHERE "LOT_CD" = %s')
            with connections['analysis_db'].cursor() as cur:
                cur.execute(sql, [lot_cd])
                box_all = _unpack_box(cur.fetchone(), top_cols)

        ranked = [{'col': c, 'r2': v, 'box_all': box_all.get(c)} for c, v in top]
        return JsonResponse({'base': base, 'ranked': ranked})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
