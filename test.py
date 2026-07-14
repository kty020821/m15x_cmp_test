# ============================================================
# Step 3 — equipment/views.py 에 추가
# ============================================================
# 드래그 선택 영역 통계 (계산은 전부 PostgreSQL이 담당)
# ============================================================

@csrf_exempt
def analysis_stats(request):
    """
    드래그로 선택된 웨이퍼들의 통계 요약.
    body: {oper_id, ids: [id, id, ...]}
    반환:
      count      : 선택 웨이퍼 수
      stats      : {컬럼: {avg, std, min, max}}  ← 모든 측정값 컬럼
      idle_dist  : IDLE 값 분포
      layer_dist : PRE_LAYER(사전공정) 분포
      eqp_dist   : 장비 분포
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    ids     = body.get('ids', [])
    table   = _an_table(oper_id)

    if not ids:
        return JsonResponse({'count': 0, 'stats': {},
                             'idle_dist': [], 'layer_dist': [], 'eqp_dist': []})

    try:
        # 1) 측정값 컬럼 목록 조회
        with connections['analysis_db'].cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s ORDER BY ordinal_position
            """, [table])
            all_cols = [r[0].upper() for r in cur.fetchall()]
        val_cols = [c for c in all_cols if c not in META_COLS]

        # 2) 각 측정값의 avg/std/min/max 를 SQL로 계산
        agg = []
        for c in val_cols:
            agg.append(f'AVG("{c}")    AS "{c}__a"')
            agg.append(f'STDDEV("{c}") AS "{c}__s"')
            agg.append(f'MIN("{c}")    AS "{c}__n"')
            agg.append(f'MAX("{c}")    AS "{c}__x"')
        agg_sql = ", ".join(agg)
        ph = ",".join(["%s"] * len(ids))

        with connections['analysis_db'].cursor() as cur:
            cur.execute(
                f'SELECT COUNT(*), {agg_sql} FROM {table} WHERE id IN ({ph})', ids
            )
            row = cur.fetchone()

            # 3) 카테고리 분포 (요인 파악용)
            def dist(col):
                cur.execute(f'''
                    SELECT COALESCE(NULLIF("{col}", ''), '(없음)') AS k, COUNT(*)
                    FROM {table} WHERE id IN ({ph})
                    GROUP BY k ORDER BY COUNT(*) DESC
                ''', ids)
                return [{'key': r[0], 'count': r[1]} for r in cur.fetchall()]

            idle_dist  = dist('IDLE')
            layer_dist = dist('PRE_LAYER')
            eqp_dist   = dist('EQP_ID')

        count = row[0]
        stats = {}
        i = 1
        for c in val_cols:
            a, s, n, x = row[i], row[i+1], row[i+2], row[i+3]
            i += 4
            stats[c] = {
                'avg': round(float(a), 3) if a is not None else None,
                'std': round(float(s), 3) if s is not None else None,
                'min': round(float(n), 3) if n is not None else None,
                'max': round(float(x), 3) if x is not None else None,
            }

        return JsonResponse({
            'count':      count,
            'stats':      stats,
            'idle_dist':  idle_dist,
            'layer_dist': layer_dist,
            'eqp_dist':   eqp_dist,
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
