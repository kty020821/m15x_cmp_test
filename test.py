"""
════════════════════════════════════════════════════════════
views_analysis.py 수정
  ① 상단에서 tech_map import, 기존 TECH_LOT_MAP 하드코딩 제거
  ② analysis_page / analysis_options 를 아래로 교체
════════════════════════════════════════════════════════════
"""

from . import tech_map


# ══════════════════════════════════════════════════════════
# 페이지
# ══════════════════════════════════════════════════════════
def analysis_page(request):
    return render(request, 'equipment/analysis.html', {
        'tech_list':      tech_map.all_techs(),
        'legend_options': LEGEND_OPTIONS,
    })


# ══════════════════════════════════════════════════════════
# 종속 드롭박스 옵션
# ══════════════════════════════════════════════════════════
def _lots_with_data():
    """실제 적재된 테이블들에 존재하는 LOT_CD 전체 (중복 제거)"""
    lots = set()
    with connections['analysis_db'].cursor() as cur:
        cur.execute("""
            SELECT tablename FROM pg_tables
            WHERE tablename LIKE 'cmp_analysis_%'
        """)
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            try:
                cur.execute(f'SELECT DISTINCT "LOT_CD" FROM {t} '
                            f'WHERE "LOT_CD" IS NOT NULL')
                lots.update(r[0] for r in cur.fetchall())
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
        # 매핑에 등록된 LOT_CD 중, 실제 데이터가 있는 것만 보여준다.
        # (등록됐지만 아직 적재 안 된 device 가 드롭박스에 뜨는 것 방지)
        tech     = body.get('tech')
        mapped   = tech_map.lots_of_tech(tech)
        have     = _lots_with_data()
        options  = [lc for lc in mapped if lc in have]

        # 데이터에는 있는데 매핑에 없는 LOT_CD = 미등록 device
        unmapped = sorted(lc for lc in have
                          if tech_map.tech_of_lot(lc) is None)
        return JsonResponse({'options': options, 'unmapped': unmapped})

    if level == 'oper':
        return JsonResponse({'options': [
            {'value': oid, 'label': f"{desc} ({oid})"} for oid, desc in OPER_LIST
        ]})

    if level == 'param':
        table = _an_table(body.get('oper_id'))
        try:
            return JsonResponse({'options': _fetch_numeric_cols(table)})
        except Exception as e:
            return JsonResponse({'options': [], 'error': str(e)})

    return JsonResponse({'options': []})
