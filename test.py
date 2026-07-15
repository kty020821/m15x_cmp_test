# ============================================================
# equipment/views.py 에 추가/수정 — Step 5: R² top5 + 박스플롯
# ============================================================

# ── psycopg2 percentile 용: 박스플롯 5-숫자 요약을 SQL로 계산 ──
def _box_stats_sql(cols, id_filter_sql, params):
    """주어진 컬럼들의 min/q1/median/q3/max 를 한 번에 계산"""
    parts = []
    for c in cols:
        parts += [
            f'MIN("{c}")                                             AS "{c}__min"',
            f'PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY "{c}")    AS "{c}__q1"',
            f'PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY "{c}")    AS "{c}__med"',
            f'PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY "{c}")    AS "{c}__q3"',
            f'MAX("{c}")                                             AS "{c}__max"',
            f'AVG("{c}")                                             AS "{c}__avg"',
        ]
    return f'SELECT {", ".join(parts)} FROM {id_filter_sql}', params


@csrf_exempt
def analysis_corr_rank(request):
    """
    조회 시점 호출.
    트렌드 param(base_col) 기준으로 나머지 숫자컬럼과의 R²(상관계수²) 계산 → 상위 5개.
    각 top5 변수의 '전체 분포' 박스플롯 통계도 함께 반환.
    body: {oper_id, lot_cd, base_col}
    """
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
        # 숫자 컬럼 목록
        with connections['analysis_db'].cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_name = %s ORDER BY ordinal_position
            """, [table])
            rows = cur.fetchall()
        num_cols = []
        for name, dtype in rows:
            up = name.upper()
            if up in META_COLS or up == base:
                continue
            if any(t in dtype for t in ('double', 'numeric', 'real', 'int')):
                num_cols.append(up)

        if not num_cols:
            return JsonResponse({'base': base, 'ranked': []})

        # 각 컬럼과 base의 상관계수 CORR() → R² = corr^2, SQL 한 방에
        corr_parts = [f'CORR("{base}", "{c}") AS "{c}"' for c in num_cols]
        with connections['analysis_db'].cursor() as cur:
            cur.execute(
                f'SELECT {", ".join(corr_parts)} FROM {table} WHERE "LOT_CD" = %s',
                [lot_cd]
            )
            crow = cur.fetchone()

        r2 = []
        for i, c in enumerate(num_cols):
            corr = crow[i]
            if corr is None:
                continue
            r2.append((c, round(corr * corr, 4)))
        r2.sort(key=lambda x: x[1], reverse=True)
        top5 = r2[:5]
        top_cols = [c for c, _ in top5]

        # top5 전체 분포 박스플롯 통계
        box_all = {}
        if top_cols:
            sql, prm = _box_stats_sql(
                top_cols, f'{table} WHERE "LOT_CD" = %s', [lot_cd])
            with connections['analysis_db'].cursor() as cur:
                cur.execute(sql, prm)
                brow = cur.fetchone()
            j = 0
            for c in top_cols:
                box_all[c] = {
                    'min': _f(brow[j]), 'q1': _f(brow[j+1]), 'med': _f(brow[j+2]),
                    'q3': _f(brow[j+3]), 'max': _f(brow[j+4]), 'avg': _f(brow[j+5]),
                }
                j += 6

        ranked = [{'col': c, 'r2': v, 'box_all': box_all.get(c)} for c, v in top5]
        return JsonResponse({'base': base, 'ranked': ranked})

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
def analysis_box_selected(request):
    """
    드래그 시점 호출.
    선택된 id들에 대해 지정 컬럼들의 박스플롯 통계 반환.
    body: {oper_id, ids, cols}
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body    = json.loads(request.body)
    oper_id = body.get('oper_id')
    ids     = body.get('ids', [])
    cols    = body.get('cols', [])
    table   = _an_table(oper_id)

    cols = [c for c in cols if re.match(r'^[0-9A-Za-z_]+$', c)]
    if not ids or not cols:
        return JsonResponse({'count': 0, 'box_sel': {}})

    try:
        ph = ",".join(["%s"] * len(ids))
        sql, _ = _box_stats_sql(cols, f'{table} WHERE id IN ({ph})', None)
        with connections['analysis_db'].cursor() as cur:
            cur.execute(sql, ids)
            brow = cur.fetchone()
            cur.execute(f'SELECT COUNT(*) FROM {table} WHERE id IN ({ph})', ids)
            count = cur.fetchone()[0]

        box_sel = {}
        j = 0
        for c in cols:
            box_sel[c] = {
                'min': _f(brow[j]), 'q1': _f(brow[j+1]), 'med': _f(brow[j+2]),
                'q3': _f(brow[j+3]), 'max': _f(brow[j+4]), 'avg': _f(brow[j+5]),
            }
            j += 6
        return JsonResponse({'count': count, 'box_sel': box_sel})

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def _f(v):
    return round(float(v), 3) if v is not None else None
