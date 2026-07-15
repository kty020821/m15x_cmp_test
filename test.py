# ============================================================
# equipment/views.py 에 추가 — Correlation 데이터 API
# ============================================================

@csrf_exempt
def analysis_corr(request):
    """
    Correlation 산점도 데이터.
    body: {oper_id, lot_cd, x_col, y_col}
    반환: [{x, y, id, EQP_ID, RECIPE_ID, ...범례컬럼, LOT_ID, WF_ID}]
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    x_col   = body.get('x_col')
    y_col   = body.get('y_col')
    table   = _an_table(oper_id)

    # 컬럼명 화이트리스트 검증
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

        return JsonResponse({'data': data, 'x_col': x_col, 'y_col': y_col})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ============================================================
# analysis_options 의 param 레벨을 "숫자 컬럼만" 반환하도록 수정
# (기존 param 블록을 아래로 교체)
# ============================================================
#
#     if level == 'param':
#         table = _an_table(body.get('oper_id'))
#         try:
#             with connections['analysis_db'].cursor() as cur:
#                 cur.execute("""
#                     SELECT column_name, data_type FROM information_schema.columns
#                     WHERE table_name = %s ORDER BY ordinal_position
#                 """, [table])
#                 rows = cur.fetchall()
#             # 숫자형(double precision, numeric, integer 등)만
#             num = [r[0].upper() for r in rows
#                    if r[0].upper() not in META_COLS and 'double' in r[1] or 'numeric' in r[1] or 'int' in r[1]]
#             # 위 조건 우선순위 버그 방지용으로 아래처럼 명확히:
#             num = []
#             for name, dtype in rows:
#                 up = name.upper()
#                 if up in META_COLS:
#                     continue
#                 if any(t in dtype for t in ('double', 'numeric', 'real', 'int')):
#                     num.append(up)
#             return JsonResponse({'options': num})
#         except Exception as e:
#             return JsonResponse({'options': [], 'error': str(e)})
