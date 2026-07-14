
# ============================================================
# Step 2 — equipment/views.py 에 추가
# ============================================================

# ── 범례 후보 (★ 항목 추가/변경하려면 여기만 수정) ──────
#    (PG 컬럼명, 화면 표시명)
LEGEND_OPTIONS = [
    ('EQP_ID',    '장비 ID'),
    ('RECIPE_ID', 'Recipe'),
    ('EQP_CH_ID', 'Chamber'),
    ('IDLE',      'Idle/Layer'),
    ('EQP_MODEL', '장비 모델'),
    ('PRE_LAYER', '사전 공정'),
]


@csrf_exempt
def analysis_trend(request):
    """
    트렌드 스캐터 데이터.
    body: {oper_id, lot_cd, param}
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    param   = body.get('param')
    table   = _an_table(oper_id)

    # SQL 인젝션 방지: 컬럼명은 화이트리스트 패턴만 허용
    if not param or not re.match(r'^[0-9A-Za-z_]+$', param):
        return JsonResponse({'error': '잘못된 파라미터'}, status=400)

    legend_cols = [c for c, _ in LEGEND_OPTIONS]
    legend_sel  = ', '.join(f'"{c}"' for c in legend_cols)

    sql = f'''
        SELECT id, "DATE", "{param}", {legend_sel}, "LOT_ID", "WF_ID"
        FROM {table}
        WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL
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
                'date':   r[1].strftime('%Y-%m-%d %H:%M:%S') if r[1] else None,
                'val':    float(r[2]) if r[2] is not None else None,
                'LOT_ID': r[3 + n],
                'WF_ID':  r[4 + n],
            }
            for i, c in enumerate(legend_cols):
                item[c] = r[3 + i]
            data.append(item)

        return JsonResponse({'data': data, 'param': param})

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ============================================================
# analysis_page 도 수정 (legend_options 넘기기)
# ============================================================
def analysis_page(request):
    return render(request, 'equipment/analysis.html', {
        'tech_list':      list(TECH_LOT_MAP.keys()),
        'legend_options': LEGEND_OPTIONS,      # ← 추가
    })
