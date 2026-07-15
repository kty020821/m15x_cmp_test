# ============================================================
# equipment/views.py — analysis_corr 함수를 아래로 교체
# R² + 추세선(회귀식) 계산 추가
# ============================================================

@csrf_exempt
def analysis_corr(request):
    """
    Correlation 산점도 + R² + 추세선.
    body: {oper_id, lot_cd, x_col, y_col}
    """
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

    legend_cols = [c for c, _ in LEGEND_OPTIONS]
    legend_sel  = ', '.join(f'"{c}"' for c in legend_cols)

    sql = f'''
        SELECT id, "{x_col}", "{y_col}", {legend_sel}, "LOT_ID", "WF_ID"
        FROM {table}
        WHERE "LOT_CD" = %s AND "{x_col}" IS NOT NULL AND "{y_col}" IS NOT NULL
        ORDER BY "DATE"
    '''
    try:
        with connections['analysis_db'].cursor() as cur:
            cur.execute(sql, [lot_cd])
            rows = cur.fetchall()
            # R², 회귀선(기울기/절편), x범위 를 SQL로 한 번에
            #   REGR_SLOPE(y, x), REGR_INTERCEPT(y, x), CORR(x, y)
            cur.execute(f'''
                SELECT CORR("{x_col}", "{y_col}"),
                       REGR_SLOPE("{y_col}", "{x_col}"),
                       REGR_INTERCEPT("{y_col}", "{x_col}"),
                       MIN("{x_col}"), MAX("{x_col}")
                FROM {table} WHERE "LOT_CD" = %s
            ''', [lot_cd])
            corr, slope, intercept, xmin, xmax = cur.fetchone()

        r2 = round(corr * corr, 4) if corr is not None else None

        # 추세선 두 점 (xmin, xmax 에서의 y)
        trend = None
        if slope is not None and intercept is not None and xmin is not None:
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
                'id':     r[0],
                'x':      float(r[1]) if r[1] is not None else None,
                'y':      float(r[2]) if r[2] is not None else None,
                'LOT_ID': r[3 + n],
                'WF_ID':  r[4 + n],
            }
            for i, c in enumerate(legend_cols):
                item[c] = r[3 + i]
            data.append(item)

        return JsonResponse({
            'data': data, 'x_col': x_col, 'y_col': y_col,
            'r2': r2, 'trend': trend,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
