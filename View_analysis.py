import json
import re
import traceback
from math import ceil

import requests
from django.conf import settings
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import connections

from . import tech_map

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

FACTOR_COLS = [
    ('IDLE',       'Idle',          'Normal',  None),
    ('PRE_LAYER',  'Layer Change',  '(없음)',  None),
    ('PRE_EQP_ID', '사전공정 장비',  '(없음)',  None),
    ('PRE_EQP_CH', '사전공정 챔버',  '(없음)',  None),
    ('EQP_ID',     '장비',          '(없음)',  None),
    ('WF_ID',      'WF 구간',       '(없음)',  5),
]

META_COLS = {
    'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
    'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
    'WF_ID', 'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY',
}

NUMERIC_TYPES = {
    'smallint', 'integer', 'bigint', 'int2', 'int4', 'int8',
    'decimal', 'numeric', 'real', 'double precision', 'float',
    'float4', 'float8', 'double', 'number', 'text', 'varchar',
    'character varying'
}

LIFT_MIN  = 1.5
COUNT_MIN = 3


def _an_table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def _f(v):
    return round(float(v), 3) if v is not None else None


def _sqlstr(v):
    return "'" + str(v).replace("'", "''") + "'"


def _existing_cols(table):
    try:
        with connections['analysis_db'].cursor() as cur:
            cur.execute("""
                SELECT upper(column_name) FROM information_schema.columns
                WHERE table_name = %s
            """, [table.lower()])
            cols = {r[0] for r in cur.fetchall()}

        if not cols:
            with connections['analysis_db'].cursor() as cur:
                cur.execute(f"SELECT * FROM {table} LIMIT 0")
                cols = {desc[0].upper() for desc in cur.description}
        return cols
    except Exception as e:
        print(f'[analysis] _existing_cols 실패 ({table}): {e}')
        return set()


def _fetch_numeric_cols(table):
    out = []
    try:
        with connections['analysis_db'].cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_name = %s ORDER BY ordinal_position
            """, [table.lower()])
            rows = cur.fetchall()

        for name, dtype in rows:
            up = name.upper()
            if up in META_COLS:
                continue
            out.append(up)
    except Exception as e:
        print(f'[analysis] information_schema 조회 실패 ({table}): {e}')

    if not out:
        try:
            with connections['analysis_db'].cursor() as cur:
                cur.execute(f"SELECT * FROM {table} LIMIT 0")
                col_names = [desc[0].upper() for desc in cur.description]
                out = [c for c in col_names if c not in META_COLS]
        except Exception as e:
            print(f'[analysis] cur.description 커서 조회 실패 ({table}): {e}')

    return out


def _numeric_col_set(table):
    return set(_fetch_numeric_cols(table))


def _table_exists(table):
    try:
        with connections['analysis_db'].cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", [table.lower()])
            row = cur.fetchone()
            return bool(row and row[0])
    except Exception as e:
        print(f'[analysis] _table_exists 실패 ({table}): {e}')
        return False


def _safe_name(v):
    return bool(v) and bool(re.match(r'^[0-9A-Za-z_]+$', str(v)))


def _api_fail(msg, payload=None, exc=None):
    print(f'[analysis] {msg}')
    if exc is not None:
        traceback.print_exc()
    out = dict(payload or {})
    out['error'] = msg
    return JsonResponse(out, status=200)


def _factor_items():
    out = []
    for item in FACTOR_COLS:
        col, label, empty = item[0], item, item[2]
        nbins = item[3] if len(item) > 3 else None
        out.append((col, label, empty, nbins))
    return out


def _num_expr(col):
    return (f'NULLIF(regexp_replace(CAST("{col}" AS TEXT), \'[^0-9]\', \'\', \'g\'), \'\')::numeric')


def _bin_edges(cur, table, col, nbins):
    try:
        cur.execute(f'SELECT MIN(v), MAX(v) FROM '
                    f'(SELECT {_num_expr(col)} AS v FROM {table}) t')
        lo, hi = cur.fetchone()
    except Exception:
        return []
    if lo is None or hi is None:
        return []
    lo, hi = int(lo), int(hi)
    step = max(1, ceil((hi - lo + 1) / nbins))

    edges, a = [], lo
    while a <= hi:
        b = min(a + step - 1, hi)
        edges.append((a, b))
        a = b + 1
    return edges


def _key_expr(col, empty_label, edges=None):
    if edges:
        num = _num_expr(col)
        cases = " ".join(
            f"WHEN {num} BETWEEN {a} AND {b} THEN {_sqlstr(f'{a}~{b}')}"
            for a, b in edges)
        return f"COALESCE(CASE {cases} END, {_sqlstr(empty_label)})"
    return f'COALESCE(NULLIF(CAST("{col}" AS TEXT), \'\'), {_sqlstr(empty_label)})'


def _order_expr(col, edges=None):
    return f'MIN({_num_expr(col)})' if edges else 'COUNT(*) DESC'


def analysis_page(request):
    return render(request, 'equipment/analysis.html', {
        'tech_list':      tech_map.all_techs(),
        'legend_options': LEGEND_OPTIONS,
    })


_OPER_CACHE = None
_OPER_ERROR = None


def _oper_cache_clear():
    global _OPER_CACHE, _OPER_ERROR
    _OPER_CACHE = None
    _OPER_ERROR = None


def _oper_names():
    global _OPER_CACHE, _OPER_ERROR
    if _OPER_CACHE:
        return _OPER_CACHE

    names = {}
    try:
        from . import config_service as cs
        for o in cs.list_opers():
            if str(o.get('use_yn') or 'Y').upper() == 'N':
                continue
            oid = str(o['oper_id']).upper().strip()
            if oid:
                names[oid] = (o.get('oper_desc') or '').strip()
    except Exception as e:
        print(f'[analysis] 기준정보 공정 목록 조회 실패: {e.__class__.__name__}: {e}')

    try:
        for oid, desc in tech_map.oper_names().items():
            names.setdefault(str(oid).upper().strip(), desc)
    except AttributeError:
        print('[analysis] tech_map.oper_names() 가 없습니다 (동작은 계속)')
    except Exception as e:
        print(f'[analysis] tech_map 공정명 조회 실패: {e.__class__.__name__}: {e}')

    try:
        from . import analysis_service as svc
        df = svc.get_config()
        for _, r in df.drop_duplicates(subset=['OPER_ID']).iterrows():
            oid  = str(r['OPER_ID']).upper().strip()
            desc = str(r.get('OPER_DESC') or '').strip()
            if oid and desc:
                names.setdefault(oid, desc)
    except Exception as e:
        print(f'[analysis] 기준정보 보조 조회 생략: {e.__class__.__name__}: {e}')

    if not names:
        _OPER_ERROR = ('공정명을 얻지 못했습니다. 셋업 페이지에서 공정을 '
                       '등록하거나 tech_map.OPER_NAME_MAP 에 추가하세요. '
                       '(공정명 없이 ID 로는 계속 사용할 수 있습니다)')
        print(f'[analysis] {_OPER_ERROR}')
        return {}

    _OPER_ERROR = None
    _OPER_CACHE = names
    print(f'[analysis] 공정명 {len(names)}건 캐시 완료')
    return names


def _loaded_tables():
    try:
        with connections['analysis_db'].cursor() as cur:
            cur.execute("""
                SELECT tablename FROM pg_tables
                WHERE tablename LIKE 'cmp_analysis_%'
                ORDER BY tablename
            """)
            return {r[0] for r in cur.fetchall()}
    except Exception as e:
        print(f'[analysis] _loaded_tables 실패: {e}')
        return set()


def _oper_options():
    names  = _oper_options_names()
    tables = _loaded_tables()

    if not tables:
        print('[analysis] cmp_analysis_* 테이블이 없습니다 — 적재가 필요합니다')
        return []

    out = []
    for oid, desc in names.items():
        if _an_table(oid) in tables:
            out.append({'value': oid,
                        'label': f'{desc} ({oid})' if desc else oid})

    if out:
        out.sort(key=lambda o: o['label'])
        known = {_an_table(oid) for oid in names}
        orphan = sorted(t for t in tables if t not in known)
        if orphan:
            print(f'[analysis] 등록에 없는 잔여 테이블 {len(orphan)}개 '
                  f'(드롭박스에서 제외됨): {", ".join(orphan[:8])}'
                  f'{" ..." if len(orphan) > 8 else ""}')
        return out

    print(f'[analysis] 등록된 공정명({len(names)}개)과 일치하는 테이블이 없어 '
          f'적재 테이블 {len(tables)}개를 ID 로 표시합니다')
    return [{'value': t.replace('cmp_analysis_', '').upper(),
             'label': t.replace('cmp_analysis_', '').upper()}
            for t in sorted(tables)]


def _oper_options_names():
    try:
        return _oper_names() or {}
    except Exception as e:
        print(f'[analysis] 공정명 조회 중 예외 — ID 로 표시합니다: '
              f'{e.__class__.__name__}: {e}')
        traceback.print_exc()
        return {}


def _lots_with_data():
    lots = set()
    try:
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
                    lots.update(str(r[0]) for r in cur.fetchall())
                except Exception:
                    pass
    except Exception as e:
        print(f'[analysis] _lots_with_data 실패: {e}')
    return lots


@csrf_exempt
def analysis_options(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    try:
        body = json.loads(request.body)
    except Exception:
        return _api_fail('요청 형식 오류', {'options': []})
    level = body.get('level')

    try:
        if level == 'lot_cd':
            mapped  = tech_map.lots_of_tech(body.get('tech'))
            have    = _lots_with_data()
            options = [lc for lc in mapped if lc in have]
            unmapped = sorted(lc for lc in have if tech_map.tech_of_lot(lc) is None)
            return JsonResponse({'options': options, 'unmapped': unmapped})

        if level == 'oper':
            opts = _oper_options()
            out  = {'options': opts}
            if _OPER_ERROR:
                out['note'] = _OPER_ERROR
            return JsonResponse(out)

        if level == 'param':
            oper_id = body.get('oper_id') or body.get('oper')
            if not oper_id:
                return JsonResponse({'options': [], 'note': 'oper_id가 지정되지 않았습니다'})

            table = _an_table(oper_id)
            if not _table_exists(table):
                return JsonResponse({'options': [], 'note': '적재된 데이터가 없습니다'})

            cols = _fetch_numeric_cols(table)
            return JsonResponse({'options': cols})

        return JsonResponse({'options': []})
    except Exception as e:
        return _api_fail(f'옵션 조회 실패: {e}', {'options': []}, exc=e)


def _sel_list(cols):
    return "".join(f', "{c}"' for c in cols)


@csrf_exempt
def analysis_trend(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    empty = {'data': [], 'param': None}
    try:
        body = json.loads(request.body)
    except Exception:
        return _api_fail('요청 형식 오류', empty)

    oper_id = body.get('oper_id') or body.get('oper')
    lot_cd  = body.get('lot_cd')
    param   = body.get('param')
    table   = _an_table(oper_id)
    empty['param'] = param

    if not _safe_name(param):
        return JsonResponse({'data': [], 'param': param,
                             'note': '파라미터가 선택되지 않았습니다'})

    try:
        if not _table_exists(table):
            return JsonResponse({'data': [], 'param': param,
                                 'note': '적재된 데이터가 없습니다'})

        have     = _existing_cols(table)
        num_cols = _numeric_col_set(table)

        if param.upper() not in num_cols and param.upper() not in have:
            note = f'{param} 컬럼이 이 공정에 없습니다'
            return JsonResponse({'data': [], 'param': param, 'note': note})

        legend_cols = [c for c, _ in LEGEND_OPTIONS if c in have]
        extra_cols  = [c for c in ('LOT_ID', 'WF_ID') if c in have]

        sql = f'''
            SELECT id, "DATE", "{param}"{_sel_list(legend_cols)}{_sel_list(extra_cols)}
            FROM {table}
            WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL
            ORDER BY "DATE"
        '''
        with connections['analysis_db'].cursor() as cur:
            cur.execute(sql, [lot_cd])
            rows = cur.fetchall()

        n = len(legend_cols)
        data = []
        for r in rows:
            item = {
                'id':   r[0],
                'date': r.strftime('%Y-%m-%d %H:%M:%S') if r else None,
                'val':  float(r[2]) if r[2] is not None else None,
            }
            for i, c in enumerate(legend_cols):
                item[c] = r[3 + i]
            for j, c in enumerate(extra_cols):
                item[c] = r[3 + n + j]
            data.append(item)

        out = {'data': data, 'param': param}
        if not data:
            out['note'] = f'{param} 에 표시할 값이 없습니다'
        return JsonResponse(out)
    except Exception as e:
        return _api_fail(f'트렌드 조회 실패: {e}', empty, exc=e)


@csrf_exempt
def analysis_corr(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    empty = {'data': [], 'x_col': None, 'y_col': None, 'r2': None, 'trend': None}
    try:
        body = json.loads(request.body)
    except Exception:
        return _api_fail('요청 형식 오류', empty)

    oper_id = body.get('oper_id') or body.get('oper')
    lot_cd  = body.get('lot_cd')
    x_col   = body.get('x_col')
    y_col   = body.get('y_col')
    table   = _an_table(oper_id)
    empty.update({'x_col': x_col, 'y_col': y_col})

    for c in (x_col, y_col):
        if not _safe_name(c):
            return JsonResponse(dict(empty, note='축이 선택되지 않았습니다'))

    try:
        if not _table_exists(table):
            return JsonResponse(dict(empty, note='적재된 데이터가 없습니다'))

        have     = _existing_cols(table)
        num_cols = _numeric_col_set(table)

        missing = [c for c in (x_col, y_col) if c.upper() not in num_cols and c.upper() not in have]
        if missing:
            note = f"{', '.join(missing)} 컬럼이 이 공정에 없습니다"
            return JsonResponse(dict(empty, note=note))

        legend_cols = [c for c, _ in LEGEND_OPTIONS if c in have]
        extra_cols  = [c for c in ('LOT_ID', 'WF_ID') if c in have]

        sql = f'''
            SELECT id, "{x_col}", "{y_col}"{_sel_list(legend_cols)}{_sel_list(extra_cols)}
            FROM {table}
            WHERE "LOT_CD" = %s AND "{x_col}" IS NOT NULL AND "{y_col}" IS NOT NULL
            ORDER BY "DATE"
        '''
        with connections['analysis_db'].cursor() as cur:
            cur.execute(sql, [lot_cd])
            rows = cur.fetchall()

            if not rows:
                return JsonResponse(dict(empty, note='두 축 모두 값이 있는 웨이퍼가 없습니다'))

            cur.execute(f'''
                SELECT CORR("{x_col}", "{y_col}"),
                       REGR_SLOPE("{y_col}", "{x_col}"),
                       REGR_INTERCEPT("{y_col}", "{x_col}"),
                       MIN("{x_col}"), MAX("{x_col}")
                FROM {table} WHERE "LOT_CD" = %s
            ''', [lot_cd])
            corr, slope, intercept, xmin, xmax = cur.fetchone()

        r2 = round(corr * corr, 4) if corr is not None else None

        trend = None
        if (slope is not None and intercept is not None
                and xmin is not None and xmax is not None
                and float(xmin) != float(xmax)):
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
                'id': r[0],
                'x':  float(r) if r is not None else None,
                'y':  float(r[2]) if r[2] is not None else None,
            }
            for i, c in enumerate(legend_cols):
                item[c] = r[3 + i]
            for j, c in enumerate(extra_cols):
                item[c] = r[3 + n + j]
            data.append(item)

        return JsonResponse({'data': data, 'x_col': x_col, 'y_col': y_col,
                             'r2': r2, 'trend': trend})
    except Exception as e:
        return _api_fail(f'상관 조회 실패: {e}', empty, exc=e)


@csrf_exempt
def analysis_stats(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    empty = {'count': 0, 'factors': []}
    try:
        body = json.loads(request.body)
    except Exception:
        return _api_fail('요청 형식 오류', empty)

    oper_id = body.get('oper_id') or body.get('oper')
    ids     = body.get('ids', [])
    table   = _an_table(oper_id)

    if not ids:
        return JsonResponse(empty)

    try:
        if not _table_exists(table):
            return JsonResponse(dict(empty, note='적재된 데이터가 없습니다'))

        with connections['analysis_db'].cursor() as cur:
            have = _existing_cols(table)

            ph = ",".join(["%s"] * len(ids))
            cur.execute(f'SELECT COUNT(*) FROM {table} WHERE id IN ({ph})', ids)
            count = cur.fetchone()[0]

            factors = []
            for col, label, empty_label, nbins in _factor_items():
                if col not in have:
                    continue

                try:
                    edges = _bin_edges(cur, table, col, nbins) if nbins else None
                    key   = _key_expr(col, empty_label, edges)
                    order = _order_expr(col, edges)

                    cur.execute(f'''
                        SELECT {key} AS k, COUNT(*)
                        FROM {table} WHERE id IN ({ph})
                        GROUP BY k ORDER BY {order}
                    ''', ids)
                    rows = [{'key': r[0], 'count': r} for r in cur.fetchall()]
                except Exception as e:
                    print(f'[analysis] 요인 집계 건너뜀 ({col}): {e}')
                    continue

                factors.append({'col': col, 'label': label, 'rows': rows,
                                'binned': bool(edges)})

        return JsonResponse({'count': count, 'factors': factors})
    except Exception as e:
        return _api_fail(f'요인 분포 조회 실패: {e}', empty, exc=e)


def _enrichment(cur, table, col, ids, lot_cd, ph, empty_label='(없음)', nbins=None):
    edges = _bin_edges(cur, table, col, nbins) if nbins else None
    key   = _key_expr(col, empty_label, edges)

    def counts(where, params):
        cur.execute(f'SELECT {key} AS k, COUNT(*) FROM {table} '
                    f'WHERE {where} GROUP BY k', params)
        return {r[0]: r for r in cur.fetchall()}

    sel  = counts(f'id IN ({ph})', list(ids))
    allc = counts('"LOT_CD" = %s', [lot_cd])

    s_tot = sum(sel.values()) or 1
    a_tot = sum(allc.values()) or 1

    out = []
    for k, sc in sel.items():
        ac = allc.get(k, 0)
        s_ratio = sc / s_tot
        a_ratio = (ac / a_tot) if ac else 0.0
        lift = (s_ratio / a_ratio) if a_ratio > 0 else None
        out.append({
            'key': k, 'sel_n': sc, 'all_n': ac,
            'sel_pct': round(s_ratio * 100, 1),
            'all_pct': round(a_ratio * 100, 1),
            'lift': round(lift, 2) if lift else None,
            'flag': bool(lift and lift >= LIFT_MIN and sc >= COUNT_MIN),
        })
    out.sort(key=lambda x: (x['lift'] or 0), reverse=True)
    return out


def _top_correlations(cur, table, param, lot_cd, ids, ph, top_n=5):
    num_cols = [c for c in _fetch_numeric_cols(table) if c != param]
    if not num_cols:
        return []

    corr_sel = ", ".join(f'CORR("{param}", "{c}")' for c in num_cols)
    cur.execute(f'SELECT {corr_sel} FROM {table} WHERE "LOT_CD" = %s', [lot_cd])
    crow = cur.fetchone()

    ranked = [(c, crow[i]) for i, c in enumerate(num_cols) if crow[i] is not None]
    ranked.sort(key=lambda x: abs(x), reverse=True)
    top = ranked[:top_n]
    if not top:
        return []

    cols = [c for c, _ in top]
    agg  = ", ".join(f'AVG("{c}")' for c in cols)
    cur.execute(f'SELECT {agg} FROM {table} WHERE id IN ({ph})', list(ids))
    srow = cur.fetchone()
    cur.execute(f'SELECT {agg} FROM {table} WHERE "LOT_CD" = %s', [lot_cd])
    arow = cur.fetchone()

    return [{
        'col': c,
        'r2': round(v * v, 3),
        'dir': '같은 방향' if v > 0 else '반대 방향',
        'sel_avg': _f(srow[i]),
        'all_avg': _f(arow[i]),
    } for i, (c, v) in enumerate(top)]


def _build_summary(oper_id, lot_cd, param, ids):
    table = _an_table(oper_id)
    ph    = ",".join(["%s"] * len(ids))

    with connections['analysis_db'].cursor() as cur:
        have = _existing_cols(table)

        cur.execute(f'''
            SELECT COUNT(*), AVG("{param}"), STDDEV("{param}"),
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "{param}")
            FROM {table} WHERE id IN ({ph})
        ''', list(ids))
        s_n, s_avg, s_std, s_med = cur.fetchone()

        cur.execute(f'''
            SELECT COUNT(*), AVG("{param}"), STDDEV("{param}"),
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "{param}")
            FROM {table} WHERE "LOT_CD" = %s
        ''', [lot_cd])
        a_n, a_avg, a_std, a_med = cur.fetchone()

        factors = []
        for col, label, empty_label, nbins in _factor_items():
            if col not in have:
                continue
            try:
                rows = _enrichment(cur, table, col, ids, lot_cd, ph,
                                   empty_label, nbins)
            except Exception as e:
                print(f'[analysis] 편중도 건너뜀 ({col}): {e}')
                continue
            factors.append({'col': col, 'label': label, 'rows': rows})

        try:
            corr = _top_correlations(cur, table, param, lot_cd, ids, ph)
        except Exception as e:
            print(f'[analysis] 상관 상위 계산 실패: {e}')
            corr = []

    dev = None
    if s_avg is not None and a_avg is not None and a_avg:
        dev = round((float(s_avg) - float(a_avg)) / abs(float(a_avg)) * 100, 2)

    return {
        'oper_id': oper_id, 'lot_cd': lot_cd, 'param': param,
        'target': {
            'sel': {'n': s_n, 'avg': _f(s_avg), 'med': _f(s_med), 'std': _f(s_std)},
            'all': {'n': a_n, 'avg': _f(a_avg), 'med': _f(a_med), 'std': _f(a_std)},
            'dev_pct': dev,
        },
        'factors': factors,
        'corr': corr,
    }


def _summary_to_text(s):
    L = []
    L.append("=== 데이터 ===")
    L.append(f"공정 {s['oper_id']} / 제품 {s['lot_cd']} / 분석 파라미터 {s['param']}")
    L.append("")

    t   = s['target']
    dev = t.get('dev_pct')
    if dev is None:
        direction = ''
    elif dev < 0:
        direction = ' → 선택 구간이 전체보다 낮음'
    elif dev > 0:
        direction = ' → 선택 구간이 전체보다 높음'
    else:
        direction = ''

    L.append("[선택 구간 vs 전체]")
    L.append(f"  웨이퍼 수 : 선택 {t['sel']['n']}장 / 전체 {t['all']['n']}장")
    L.append(f"  평균      : 선택 {t['sel']['avg']} / 전체 {t['all']['avg']}"
             f" (편차 {dev}%){direction}")
    L.append(f"  중앙값    : 선택 {t['sel']['med']} / 전체 {t['all']['med']}")
    L.append(f"  표준편차  : 선택 {t['sel']['std']} / 전체 {t['all']['std']}")
    L.append("")

    flagged = []
    for f in s['factors']:
        for r in [r for r in f['rows'] if r['flag']][:3]:
            flagged.append(
                f"  {f['label']} = {r['key']} : 선택 {r['sel_pct']}% / 전체 {r['all_pct']}%"
                f" → {r['lift']}배 집중 ({r['sel_n']}장)")

    if flagged:
        L.append("[편중된 조건] 전체 대비 선택 구간에 몰려 있는 항목")
        L.extend(flagged)
    else:
        L.append("[편중된 조건] 없음")
        L.append("  검사한 모든 요인에서 전체 대비 유의하게 몰린 조건이 발견되지 않음")
    L.append("")

    L.append(f"[검사한 요인] {', '.join(f['label'] for f in s['factors'])}")
    L.append("")

    if s.get('corr'):
        L.append("[함께 움직인 파라미터] 전체 데이터 기준 상관 상위")
        for c in s['corr']:
            L.append(f"  {c['col']} : R² {c['r2']} ({c['dir']}),"
                     f" 선택 평균 {c['sel_avg']} / 전체 평균 {c['all_avg']}")
        L.append("")

    absent = []
    if not s.get('corr'):
        absent.append("파라미터 간 상관")
    absent.append("소모품(Pad/Head/Disk) 수명")
    absent.append("압력·회전수·슬러리 등 위에 나열되지 않은 모든 설비 조건")
    absent.append("스펙 상하한, 목표값")

    L.append("[이 요약에 없는 정보] 아래는 제공되지 않았으므로 언급하지 말 것")
    for a in absent:
        L.append(f"  - {a}")

    return "\n".join(L)


LLM_SYSTEM = "당신은 반도체 CMP 공정 엔지니어를 돕는 데이터 분석 보조자입니다.\n주어진 데이터 수치만 사용하고 없는 항목은 절대 언급하지 마세요."


def _call_company_llm(system, user):
    resp = requests.post(
        settings.LLM_URL + '/chat/completions',
        json={
            'model': settings.LLM_MODEL,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user',   'content': user},
            ],
            'temperature': 0.2,
        },
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {settings.LLM_API_KEY}',
        },
        timeout=getattr(settings, 'LLM_TIMEOUT', 60),
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']


@csrf_exempt
def analysis_insight(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    try:
        body = json.loads(request.body)
    except Exception:
        return _api_fail('요청 형식 오류')

    oper_id = body.get('oper_id') or body.get('oper')
    lot_cd  = body.get('lot_cd')
    param   = body.get('param')
    ids     = body.get('ids', [])
    use_llm = body.get('use_llm', True)
    table   = _an_table(oper_id)

    if not ids:
        return JsonResponse({'error': '선택된 구간이 없습니다'})
    if not _safe_name(param):
        return JsonResponse({'error': '파라미터가 선택되지 않았습니다'})

    try:
        if param.upper() not in _numeric_col_set(table) and param.upper() not in _existing_cols(table):
            return JsonResponse({
                'error': f'{param} 은 데이터가 없어 분석할 수 없습니다.'})

        summary = _build_summary(oper_id, lot_cd, param, ids)
        text    = _summary_to_text(summary)

        answer, llm_error = None, None
        if use_llm:
            try:
                answer = _call_company_llm(LLM_SYSTEM, text)
            except NotImplementedError:
                llm_error = '사내 LLM API가 연결되지 않았습니다.'
            except Exception as e:
                llm_error = f'LLM 호출 실패: {e}'

        return JsonResponse({
            'summary':   summary,
            'prompt':    text,
            'answer':    answer,
            'llm_error': llm_error,
        })
    except Exception as e:
        return _api_fail(f'분석 실패: {e}', exc=e)


CHAT_MAX_PARAMS  = 3
CHAT_HISTORY_MAX = 6
HIST_BINS        = 8


def _mentioned_params(question, table, base_param=None):
    cols = _fetch_numeric_cols(table)
    q    = question.upper()

    hits = []
    for c in sorted(cols, key=len, reverse=True):
        if c in q and not any(c in h for h in hits):
            hits.append(c)
        if len(hits) >= CHAT_MAX_PARAMS:
            break

    if not hits and base_param and base_param in cols:
        hits = [base_param]
    return hits


def _param_stats(cur, table, param, lot_cd, ids, ph):
    def agg(where, params):
        cur.execute(f'''
            SELECT COUNT("{param}"), AVG("{param}"), STDDEV("{param}"),
                   MIN("{param}"), MAX("{param}"),
                   PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY "{param}"),
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY "{param}"),
                   PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY "{param}")
            FROM {table} WHERE {where} AND "{param}" IS NOT NULL
        ''', params)
        n, avg, std, mn, mx, q1, med, q3 = cur.fetchone()
        return {'n': n or 0, 'avg': _f(avg), 'std': _f(std),
                'min': _f(mn), 'max': _f(mx),
                'q1': _f(q1), 'med': _f(med), 'q3': _f(q3)}

    sel = agg(f'id IN ({ph})', list(ids))
    alw = agg('"LOT_CD" = %s', [lot_cd])

    return {'param': param, 'sel': sel, 'all': alw,
            'hist': _param_hist(cur, table, param, lot_cd, ids, ph, alw)}


def _param_hist(cur, table, param, lot_cd, ids, ph, alw):
    if alw['min'] is None or alw['max'] is None:
        return []
    lo, hi = float(alw['min']), float(alw['max'])
    if hi <= lo:
        return []

    width = (hi - lo) / HIST_BINS
    cur.execute(f'''
        SELECT width_bucket("{param}", %s, %s, %s) AS b, COUNT(*)
        FROM {table} WHERE id IN ({ph}) AND "{param}" IS NOT NULL
        GROUP BY b ORDER BY b
    ''', [lo, hi, HIST_BINS] + list(ids))
    counts = {r[0]: r for r in cur.fetchall()}

    out = []
    for b in range(1, HIST_BINS + 1):
        a = lo + width * (b - 1)
        z = lo + width * b
        n = counts.get(b, 0) + (counts.get(HIST_BINS + 1, 0) if b == HIST_BINS else 0)
        out.append({'lo': round(a, 3), 'hi': round(z, 3), 'n': n})
    return out


def _param_by_eqp(cur, table, param, lot_cd, ids, ph):
    cur.execute(f'''
        SELECT "EQP_ID", COUNT(*), AVG("{param}")
        FROM {table} WHERE id IN ({ph}) AND "{param}" IS NOT NULL
        GROUP BY "EQP_ID" ORDER BY AVG("{param}") DESC
    ''', list(ids))
    return [{'eqp': r[0], 'n': r, 'avg': _f(r[2])} for r in cur.fetchall()]


def _stats_to_text(st, by_eqp=None):
    L = [f"[{st['param']}]"]
    s, a = st['sel'], st['all']

    L.append(f"  선택 구간 {s['n']}장 : 평균 {s['avg']}, 표준편차 {s['std']}, "
             f"범위 {s['min']}~{s['max']}")
    L.append(f"    사분위 Q1 {s['q1']} / 중앙값 {s['med']} / Q3 {s['q3']}")
    L.append(f"  전체 {a['n']}장 : 평균 {a['avg']}, 표준편차 {a['std']}, "
             f"범위 {a['min']}~{a['max']}")
    L.append(f"    사분위 Q1 {a['q1']} / 중앙값 {a['med']} / Q3 {a['q3']}")

    if st['hist']:
        L.append("  선택 구간 분포 (전체 범위를 8구간으로 나눔)")
        for h in st['hist']:
            bar = '■' * min(h['n'], 30)
            L.append(f"    {h['lo']} ~ {h['hi']} : {h['n']}장 {bar}")

    if by_eqp:
        L.append("  선택 구간 장비별 평균")
        for e in by_eqp:
            L.append(f"    {e['eqp']} : {e['avg']} ({e['n']}장)")

    return "\n".join(L)


CHAT_SYSTEM = "당신은 반도체 CMP 공정 엔지니어의 데이터 분석을 돕는 보조자입니다."


@csrf_exempt
def analysis_chat(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    try:
        body = json.loads(request.body)
    except Exception:
        return _api_fail('요청 형식 오류')

    oper_id  = body.get('oper_id') or body.get('oper')
    lot_cd   = body.get('lot_cd')
    param    = body.get('param')
    ids      = body.get('ids', [])
    question = (body.get('question') or '').strip()
    history  = body.get('history', [])

    if not ids:
        return JsonResponse({'error': '선택된 구간이 없습니다'})
    if not question:
        return JsonResponse({'error': '질문이 비어 있습니다'})

    table = _an_table(oper_id)
    ph    = ",".join(["%s"] * len(ids))

    try:
        targets = _mentioned_params(question, table, param)

        blocks = []
        with connections['analysis_db'].cursor() as cur:
            have = _existing_cols(table)
            for p in targets:
                try:
                    st  = _param_stats(cur, table, p, lot_cd, ids, ph)
                    eqp = (_param_by_eqp(cur, table, p, lot_cd, ids, ph)
                           if 'EQP_ID' in have else None)
                    blocks.append(_stats_to_text(st, eqp))
                except Exception as e:
                    print(f'[analysis] 대화 통계 건너뜀 ({p}): {e}')

        ctx = [f"공정 {oper_id} / 제품 {lot_cd} / 선택 웨이퍼 {len(ids)}장", ""]
        if blocks:
            ctx.append("=== 계산 결과 ===")
            ctx.extend(blocks)
        else:
            ctx.append("(질문에서 파라미터를 찾지 못해 통계를 계산하지 않았습니다)")

        ctx.append("")
        ctx.append("[이 데이터에 없는 정보] 위에 나열되지 않은 파라미터,")
        ctx.append("스펙 상하한, 소모품 수명, 설비 조건은 제공되지 않았습니다.")

        messages = [{'role': 'system', 'content': CHAT_SYSTEM}]
        for h in history[-CHAT_HISTORY_MAX:]:
            role = 'assistant' if h.get('role') == 'assistant' else 'user'
            messages.append({'role': role, 'content': str(h.get('content', ''))})
        messages.append({
            'role': 'user',
            'content': "\n".join(ctx) + f"\n\n=== 질문 ===\n{question}",
        })

        answer, llm_error = None, None
        try:
            answer = _call_llm_messages(messages)
        except Exception as e:
            llm_error = f'LLM 호출 실패: {e}'

        return JsonResponse({
            'answer':    answer,
            'llm_error': llm_error,
            'params':    targets,
            'context':   "\n".join(ctx),
        })
    except Exception as e:
        return _api_fail(f'대화 처리 실패: {e}', exc=e)


def _call_llm_messages(messages):
    resp = requests.post(
        settings.LLM_URL + '/chat/completions',
        json={
            'model': settings.LLM_MODEL,
            'messages': messages,
            'temperature': 0.2,
        },
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {settings.LLM_API_KEY}',
        },
        timeout=getattr(settings, 'LLM_TIMEOUT', 60),
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']
