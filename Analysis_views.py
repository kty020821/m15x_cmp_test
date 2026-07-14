# ============================================================
# equipment/views.py 맨 아래에 추가
# ============================================================
# 파일 상단 import 확인 (없으면 추가):
#   import json, re
#   from django.shortcuts import render
#   from django.http import JsonResponse
#   from django.views.decorators.csrf import csrf_exempt
#   from django.db import connections
# ============================================================

import json
import re
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import connections


# ── 기준정보 ─────────────────────────────────────────────
TECH_LOT_MAP = {
    'LUCY': ['5E2', '5E9'],
}

OPER_LIST = [
    ('OP100', 'BLC BUF NIT TOUCH CMP'),
]

# ── 측정값이 아닌 메타 컬럼 (PARAMETER 후보에서 제외) ────
META_COLS = {
    'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
    'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
    'WF_ID', 'IDLE', 'PRE_LAYER',
}


def _an_table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def analysis_page(request):
    """산포 분석 페이지"""
    return render(request, 'equipment/analysis.html', {
        'tech_list': list(TECH_LOT_MAP.keys()),
    })


@csrf_exempt
def analysis_options(request):
    """
    종속 드롭박스 옵션.
    body: {level: 'lot_cd'|'oper'|'param', tech, oper_id}
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body  = json.loads(request.body)
    level = body.get('level')

    if level == 'lot_cd':
        return JsonResponse({'options': TECH_LOT_MAP.get(body.get('tech'), [])})

    if level == 'oper':
        return JsonResponse({'options': [
            {'value': oid, 'label': f"{desc} ({oid})"} for oid, desc in OPER_LIST
        ]})

    if level == 'param':
        table = _an_table(body.get('oper_id'))
        try:
            with connections['analysis_db'].cursor() as cur:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = %s ORDER BY ordinal_position
                """, [table])
                cols = [r[0].upper() for r in cur.fetchall()]
            return JsonResponse({'options': [c for c in cols if c not in META_COLS]})
        except Exception as e:
            return JsonResponse({'options': [], 'error': str(e)})

    return JsonResponse({'options': []})
