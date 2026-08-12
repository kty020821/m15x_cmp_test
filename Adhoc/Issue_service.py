"""
equipment/issue_service.py
════════════════════════════════════════════════════════════
이슈 구간 분석

  "이 기간이 이상했다" 혹은 "이 랏들이 이상했다" 를 사용자가 지목하면,
  그 구간이 실제로 이상한지 판정하고 변곡점을 찾아낸다.

  적재된 cmp_analysis_* 테이블이면 무엇이든 대상이 된다 —
  정기 적재분도, 1회성 조회 결과(ADHOC_*)도 같은 코드로 분석한다.

────────────────────────────────────────────────────────────
구간 지정 방법 (mode)

  range   기간   date_from ~ date_to
  lots    랏     LOT_ID 목록 — "이 랏들만 이상했나" 를 볼 때
  wafers  웨이퍼 id 목록 — 차트에서 드래그한 결과 등

  어느 방법이든 '선택 구간' 과 '나머지(기준)' 로 갈라 비교한다.
  ★ 기준을 전체가 아니라 '나머지' 로 잡는 이유
    선택 구간이 전체에 포함되면 이상값이 기준선을 끌어올려
    차이가 실제보다 작게 나온다. 빼고 비교해야 제대로 드러난다.

판정 (인라인 모니터링과 같은 관점)
  L 수준이탈  선택 평균이 기준 대비 몇 σ
  R 범위이탈  기준 min/max 밖 웨이퍼 수
  S 산포확대  표준편차가 몇 배
  E 단독이탈  장비·챔버 한 대만 벗어남

변곡점
  시간순으로 놓고 "여기서 수준이 바뀌었다" 는 지점을 찾는다.
  이진 분할(binary segmentation) — 모든 분할점에서 전후 평균 차이를
  t 통계량으로 재고, 가장 큰 지점이 기준을 넘으면 변곡점으로 확정한 뒤
  좌우 구간에서 다시 찾는다. 외부 라이브러리 없이 동작한다.
════════════════════════════════════════════════════════════
"""

import re
from datetime import datetime

from django.db import connections

# ── 판정 기준 (monitor_service 와 같은 값으로 유지) ────────
SIGMA_WARN   = 1.0
SIGMA_ALERT  = 2.0
OUT_WARN     = 1
OUT_ALERT    = 3
EQP_SIGMA    = 2.0
SPREAD_WARN  = 1.5
SPREAD_ALERT = 2.0
MIN_N        = 5

# ── 변곡점 탐지 ───────────────────────────────────────────
CP_MIN_SEG   = 5      # 한 구간에 이만큼은 있어야 분할을 시도한다
CP_THRESHOLD = 3.0    # t 통계량이 이 이상이면 변곡점
CP_MAX       = 5      # 최대 몇 개까지 찾을지
CP_MIN_SIGMA = 0.8    # 전후 평균 차이가 이 σ 미만이면 무시 (미세 변동 제외)

# LOT 판정
LOT_MIN_N    = 3      # 랏당 웨이퍼가 이보다 적으면 신뢰도 낮음 표기

# ── LOT_ID 자릿수 ─────────────────────────────────────────
#   LOT_ID 는 기본 7자리인데 간혹 9자리가 섞인다(뒤에 덧붙는 자리).
#   같은 랏인데 표기가 갈리면 선택·집계·판정이 전부 어긋나므로
#   비교와 그룹핑은 항상 앞 7자리로 맞춘다.
#   (웨이퍼 단위 식별이 필요하면 이 7자리 뒤에 wf_id 를 붙인다)
LOT_KEY_LEN = 7
LOT_KEY_SQL = f'left("LOT_ID", {LOT_KEY_LEN})'


def lot_key(v):
    """랏 표기를 비교용 키로 — 앞 7자리"""
    return str(v or '').strip().upper()[:LOT_KEY_LEN]


def _conn():
    return connections['analysis_db']


def _table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def _exists(cur, t):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", [t])
    return bool(cur.fetchone()[0])


def _cols(cur, t):
    cur.execute("""
        SELECT upper(column_name) FROM information_schema.columns
        WHERE table_name = %s
    """, [t])
    return {r[0] for r in cur.fetchall()}


def _f(v, nd=3):
    return round(float(v), nd) if v is not None else None


def _no_table_msg(table, oper_id):
    """
    결과 테이블이 없을 때의 안내.

    1회성 조회(ADHOC_*)는 조회 결과가 0행이면 테이블 자체가 만들어지지 않고,
    보관 기간(KEEP_DAYS)이 지나면 정리된다. 원인을 같이 알려 준다.
    """
    if str(oper_id or '').upper().startswith('ADHOC_'):
        return (f'{table} 이 없습니다. 1회성 조회 결과가 남아 있지 않습니다 — '
                f'조회 결과가 0행이었거나, 보관 기간이 지나 정리됐거나, '
                f'요청이 아직 완료되지 않았습니다. '
                f'같은 조건으로 다시 조회한 뒤 분석해 주세요.')
    return f'{table} 이 없습니다. 해당 공정이 아직 적재되지 않았습니다.'


def _safe_name(v):
    return bool(v) and bool(re.match(r'^[0-9A-Za-z_]+$', str(v)))


# ══════════════════════════════════════════════════════════
# 대상 정보 — 화면이 구간을 지정할 수 있게 후보를 준다
# ══════════════════════════════════════════════════════════
def context(oper_id, lot_cd=None):
    """
    이 테이블에서 고를 수 있는 것들.
      params   숫자 파라미터
      lot_cds  device
      lot_ids  실제 랏 (선택 대상)
      date_min/max  데이터가 있는 기간
    """
    table = _table(oper_id)
    num = {'smallint', 'integer', 'bigint', 'decimal',
           'numeric', 'real', 'double precision'}
    meta = {'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
            'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
            'WF_ID', 'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY',
            'REWORK_N', 'MEAS_N'}

    with _conn().cursor() as cur:
        if not _exists(cur, table):
            return {'ok': False, 'error': _no_table_msg(table, oper_id)}

        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = %s ORDER BY ordinal_position
        """, [table])
        rows = cur.fetchall()
        params = [c.upper() for c, d in rows
                  if d.lower() in num and c.upper() not in meta
                  and not c.upper().endswith(('_OFFSET', '_FORMULA'))]
        have = {c.upper() for c, _ in rows}

        cur.execute(f'SELECT DISTINCT "LOT_CD" FROM {table} '
                    f'WHERE "LOT_CD" IS NOT NULL ORDER BY 1')
        lot_cds = [str(r[0]) for r in cur.fetchall()]

        where, args = '', []
        if lot_cd:
            where, args = 'WHERE "LOT_CD" = %s', [lot_cd]

        cur.execute(f'SELECT MIN("DATE"), MAX("DATE"), COUNT(*) '
                    f'FROM {table} {where}', args)
        dmin, dmax, n = cur.fetchone()

        lot_ids = []
        if 'LOT_ID' in have:
            # 랏 선택 후보 — 시간순이어야 눈으로 찾기 쉽다.
            # ★ 7자리 키로 묶는다. 9자리 표기가 섞이면 같은 랏이
            #   목록에 두 번 나와 고를 때 혼란스럽다.
            cur.execute(f'''
                SELECT {LOT_KEY_SQL}, COUNT(*), MIN("DATE")
                FROM {table} {where}
                {"AND" if where else "WHERE"} "LOT_ID" IS NOT NULL
                GROUP BY {LOT_KEY_SQL} ORDER BY MIN("DATE")
            ''', args)
            lot_ids = [{'lot_id': r[0], 'n': r[1],
                        'date': str(r[2])[:19] if r[2] else ''}
                       for r in cur.fetchall()]

    return {'ok': True, 'params': params, 'lot_cds': lot_cds,
            'lot_ids': lot_ids, 'rows': n,
            'date_min': str(dmin)[:19] if dmin else None,
            'date_max': str(dmax)[:19] if dmax else None}


# ══════════════════════════════════════════════════════════
# 구간 지정 → SQL 조건
# ══════════════════════════════════════════════════════════
def _sel_clause(sel, have):
    """
    선택 구간을 (조건문, 인자, 설명) 으로.
    선택에 해당하지 않는 나머지는 NOT (조건) 으로 쓴다.

    mode
      range   기간
      lots    랏 목록
      wafers  웨이퍼 id 목록
      both    기간 + 랏을 함께 (combine 으로 결합 방식 지정)
                and : 그 기간 '안의' 그 랏들만   ← 좁힌다
                or  : 그 기간 '또는' 그 랏들     ← 넓힌다
    """
    mode = (sel or {}).get('mode', 'range')

    def _range():
        d1 = str(sel.get('date_from') or '').strip()
        d2 = str(sel.get('date_to') or '').strip()
        if not d1 or not d2:
            raise ValueError('이슈 구간의 시작·종료 일시를 지정하세요')
        if d1 > d2:
            d1, d2 = d2, d1
        # ★ 날짜만 들어오면(YYYY-MM-DD) 그날 하루 전체로 넓힌다.
        #   종료일에 시각을 안 붙이면 00:00:00 으로 해석되어
        #   그날 데이터가 통째로 빠진다.
        label = f'기간 {d1} ~ {d2}'
        if len(d1) == 10:
            d1 = d1 + ' 00:00:00'
        if len(d2) == 10:
            d2 = d2 + ' 23:59:59'
        return ('("DATE" >= %s AND "DATE" <= %s)', [d1, d2], label)

    def _lots():
        raw = [str(v).strip() for v in (sel.get('lot_ids') or []) if str(v).strip()]
        if not raw:
            raise ValueError('이슈 랏(LOT_ID)을 하나 이상 지정하세요')
        if 'LOT_ID' not in have:
            raise ValueError('이 테이블에 LOT_ID 컬럼이 없습니다')
        # ★ 7자리 키로 비교한다 — 9자리 표기가 섞여도 같은 랏으로 잡힌다
        keys = sorted({lot_key(v) for v in raw})
        ph = ",".join(["%s"] * len(keys))
        return (f'({LOT_KEY_SQL} IN ({ph}))', keys,
                f'랏 {len(keys)}개 ({", ".join(keys[:5])}'
                f'{" 외" if len(keys) > 5 else ""})')

    if mode == 'range':
        return _range()

    if mode == 'lots':
        return _lots()

    if mode == 'both':
        r_sql, r_args, r_desc = _range()
        l_sql, l_args, l_desc = _lots()
        combine = str(sel.get('combine') or 'and').lower()
        if combine == 'or':
            return (f'({r_sql} OR {l_sql})', r_args + l_args,
                    f'{r_desc} 또는 {l_desc}')
        # 기본은 AND — "그 기간 안의 그 랏들" 이 가장 흔한 요구다
        return (f'({r_sql} AND {l_sql})', r_args + l_args,
                f'{r_desc} 안의 {l_desc}')

    if mode == 'wafers':
        ids = [int(v) for v in (sel.get('ids') or []) if str(v).strip().isdigit()]
        if not ids:
            raise ValueError('선택된 웨이퍼가 없습니다')
        ph = ",".join(["%s"] * len(ids))
        return (f'(id IN ({ph}))', ids, f'웨이퍼 {len(ids)}장')

    raise ValueError(f'알 수 없는 구간 지정 방식: {mode}')


def _part_clauses(sel, have):
    """
    기간·랏 조건을 각각 돌려준다 (없으면 None).

    ★ 화면에서 색을 셋으로 나누기 위한 것 —
      이슈 구간 전체는 한 색, 그중 지정한 랏은 다른 색으로 보여 주려면
      점마다 '구간에 속하는지' 와 '지정 랏인지' 를 따로 알아야 한다.
      판정용 선택 집합(_sel_clause)은 combine 규칙에 따라 합쳐진 것이라
      그것만으로는 둘을 구분할 수 없다.
    """
    mode = (sel or {}).get('mode', 'range')
    rng = lots = None

    if mode in ('range', 'both'):
        d1 = str(sel.get('date_from') or '').strip()
        d2 = str(sel.get('date_to') or '').strip()
        if d1 and d2:
            if d1 > d2:
                d1, d2 = d2, d1
            if len(d1) == 10:
                d1 = d1 + ' 00:00:00'
            if len(d2) == 10:
                d2 = d2 + ' 23:59:59'
            rng = ('("DATE" >= %s AND "DATE" <= %s)', [d1, d2])

    if mode in ('lots', 'both') and 'LOT_ID' in have:
        keys = sorted({lot_key(v) for v in (sel.get('lot_ids') or [])
                       if str(v).strip()})
        if keys:
            ph = ",".join(["%s"] * len(keys))
            lots = (f'({LOT_KEY_SQL} IN ({ph}))', keys)

    return rng, lots


def _stats(cur, table, param, where, args):
    cur.execute(f'''
        SELECT COUNT("{param}"), AVG("{param}"), STDDEV("{param}"),
               MIN("{param}"), MAX("{param}"),
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "{param}")
        FROM {table} WHERE {where} AND "{param}" IS NOT NULL
    ''', args)
    n, avg, std, mn, mx, med = cur.fetchone()
    return {'n': n or 0, 'avg': _f(avg), 'std': _f(std),
            'min': _f(mn), 'max': _f(mx), 'med': _f(med)}


# ══════════════════════════════════════════════════════════
# 변곡점 탐지
# ══════════════════════════════════════════════════════════
# 변곡점 탐지에 쓸 시계열 길이 상한.
#   탐지 비용은 길이에 비례하고, 재귀까지 겹치면 긴 시계열에서 응답이 늦어진다.
#   수천 점을 다 봐도 '수준이 바뀐 지점' 판단은 달라지지 않으므로 줄여서 본다.
CP_MAX_POINTS = 1500


def _prefix(vals):
    """
    누적합·누적제곱합.

    ★ 이게 성능의 핵심이다.
      예전에는 분할점마다 전후 평균·분산을 전체 순회로 구해 O(n²) 이었다.
      시계열이 수천 점이면(1년치 조회의 랏 수) 수백만~수천만 번 연산이 되어
      응답이 사실상 멈춘다. 누적합을 한 번 만들어 두면 어떤 분할점이든
      평균·분산을 O(1) 로 구할 수 있어 전체가 O(n) 이 된다.
    """
    n = len(vals)
    s1 = [0.0] * (n + 1)
    s2 = [0.0] * (n + 1)
    for i, v in enumerate(vals):
        s1[i + 1] = s1[i] + v
        s2[i + 1] = s2[i] + v * v
    return s1, s2


def _seg_stats(s1, s2, a, b):
    """[a, b) 구간의 (개수, 평균, 표본분산) — 누적합에서 O(1)"""
    n = b - a
    if n <= 0:
        return 0, 0.0, 0.0
    tot = s1[b] - s1[a]
    sq  = s2[b] - s2[a]
    m = tot / n
    if n < 2:
        return n, m, 0.0
    var = (sq - tot * tot / n) / (n - 1)
    return n, m, max(var, 0.0)


def _t_at(s1, s2, n, i):
    """분할점 i 에서 전후 평균 차이의 t 통계량 — O(1)"""
    n1, m1, v1 = _seg_stats(s1, s2, 0, i)
    n2, m2, v2 = _seg_stats(s1, s2, i, n)
    if n1 < 2 or n2 < 2:
        return 0.0, None, None
    sp = ((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2)
    if sp <= 0:
        return (0.0, m1, m2) if m1 == m2 else (999.0, m1, m2)
    se = (sp * (1 / n1 + 1 / n2)) ** 0.5
    return (abs(m2 - m1) / se if se > 0 else 0.0), m1, m2


def _downsample(series, cap=CP_MAX_POINTS):
    """긴 시계열을 균등 간격으로 줄인다 (수준 변화 판단에는 영향이 없다)"""
    if len(series) <= cap:
        return series
    step = len(series) / cap
    return [series[int(i * step)] for i in range(cap)]


def _find_change_points(series, base_std, depth=0, offset=0, found=None,
                        _pre=None):
    """
    이진 분할로 변곡점을 찾는다.

    series : [{'x': 라벨, 'v': 값}, ...]  시간순
    base_std : σ 환산용 기준 표준편차

    가장 강한 분할점을 찾아 확정하고, 좌우 구간에서 다시 찾는다.
    미세한 변동까지 잡으면 노이즈가 되므로 CP_MIN_SIGMA 로 거른다.

    ★ 누적합(_prefix)으로 각 분할점을 O(1) 에 평가한다 — 전체 O(n).
      전체 순회 방식은 O(n²) 라 시계열이 길면 응답이 멈춘다.
    """
    if found is None:
        found = []
        series = _downsample(series)          # 최상위에서 한 번만 줄인다
    if len(series) < CP_MIN_SEG * 2 or len(found) >= CP_MAX or depth > 3:
        return found

    vals = [s['v'] for s in series]
    n = len(vals)
    s1, s2 = _prefix(vals)

    best_i, best_t, best_m1, best_m2 = None, 0.0, None, None
    for i in range(CP_MIN_SEG, n - CP_MIN_SEG + 1):
        t, m1, m2 = _t_at(s1, s2, n, i)
        if t > best_t:
            best_i, best_t, best_m1, best_m2 = i, t, m1, m2

    if best_i is None or best_t < CP_THRESHOLD:
        return found

    shift = (best_m2 - best_m1)
    shift_sigma = (shift / float(base_std)) if base_std else None
    if shift_sigma is not None and abs(shift_sigma) < CP_MIN_SIGMA:
        return found            # 통계적으로는 유의해도 실무적으로 미미

    found.append({
        'at': series[best_i]['x'],
        'index': offset + best_i,
        'before_avg': _f(best_m1), 'after_avg': _f(best_m2),
        'shift': _f(shift), 'shift_sigma': round(shift_sigma, 2) if shift_sigma else None,
        't': round(best_t, 2),
        'n_before': best_i, 'n_after': n - best_i,
        'direction': '상승' if shift > 0 else '하락',
    })

    _find_change_points(series[:best_i], base_std, depth + 1, offset, found)
    _find_change_points(series[best_i:], base_std, depth + 1,
                        offset + best_i, found)
    found.sort(key=lambda c: c['index'])
    return found


# ══════════════════════════════════════════════════════════
# 본체
# ══════════════════════════════════════════════════════════
def scan_all(oper_id, lot_cd, sel, unit='lot', max_params=400,
             params=None, with_series=False, series_points=1500):
    """
    등록된 전 파라미터를 한 번에 스캔한다.

    "이 구간에서 무엇이 변했나" 에 답하는 것이 목적이다.
    파라미터를 하나씩 고를 필요 없이, 변곡이 있었던 항목을 찾아 준다.

    ★ 성능 설계 — 파라미터가 100개를 넘으므로 쿼리 수를 줄여야 한다.
      · 통계는 파라미터를 묶어 한 쿼리로 집계 (CHUNK 개씩)
      · 변곡점용 시계열은 랏 평균으로 한 번에 가져와
        (랏 수 × 파라미터 수) 크기로 줄인다.
        웨이퍼 단위로 전 파라미터를 읽으면 512MiB 에서 위험하다.
      · 랏 평균은 노이즈도 적어 큰 흐름을 보기에 오히려 낫다.

    ★ PART(소모품)는 판정에서 뺀다 — 누적·리셋되는 값이라
      구간 비교가 의미 없다. 목록에는 참고로 남긴다.

    ★ params 를 주면 그 파라미터만 처리한다.
      화면이 전체를 몇 덩어리로 나눠 호출해 진행률을 보여주기 위한 것 —
      한 번에 100개를 처리하면 몇십 초 동안 아무 표시도 못 한다.

    ★ with_series=True 면 각 항목에 차트용 시계열을 함께 담는다.
      리포트(HTML) 생성에만 쓴다 — 화면 표에는 필요 없고 응답만 무거워진다.
    """
    from . import param_types as pt

    table = _table(oper_id)
    CHUNK = 20

    with _conn().cursor() as cur:
        if not _exists(cur, table):
            raise ValueError(_no_table_msg(table, oper_id))
        have = _cols(cur, table)

        all_params = _numeric_params(cur, table)[:max_params]
        if params:
            want = {str(p).upper().strip() for p in params}
            params = [p for p in all_params if p in want]
        else:
            params = all_params
        if not params:
            raise ValueError('스캔할 파라미터가 없습니다')

        base_where, base_args = '1=1', []
        if lot_cd and 'LOT_CD' in have:
            base_where, base_args = '"LOT_CD" = %s', [lot_cd]

        sel_sql, sel_args, sel_desc = _sel_clause(sel, have)
        in_where  = f'({base_where}) AND ({sel_sql})'
        out_where = f'({base_where}) AND NOT ({sel_sql})'
        in_args   = base_args + sel_args
        out_args  = base_args + sel_args

        # ── 1. 선택/나머지 통계 (묶어서 조회) ────────────
        stat_in  = _bulk_stats(cur, table, params, in_where, in_args, CHUNK)
        stat_out = _bulk_stats(cur, table, params, out_where, out_args, CHUNK)

        # ── 2. 나머지 범위 밖 웨이퍼 수 ──────────────────
        out_cnt = _bulk_out_count(cur, table, params, in_where, in_args,
                                  stat_out, CHUNK)

        # ── 3. 변곡점용 시계열 (랏 평균, 한 번에) ────────
        parts = _part_clauses(sel, have)
        series_map, sel_flag = _bulk_series(cur, table, params,
                                            base_where, base_args,
                                            sel_sql, sel_args, have, unit,
                                            parts=parts)

    # ── 4. 파라미터별 판정 ───────────────────────────────
    items = []
    for p in params:
        ptype = pt.classify(p)
        s_in, s_out = stat_in.get(p, {}), stat_out.get(p, {})
        ser = series_map.get(p, [])
        it = _judge_one(p, ptype, s_in, s_out, out_cnt.get(p, 0),
                        ser, sel_flag)
        if with_series:
            # 리포트 차트용 — 점이 너무 많으면 파일이 커지므로 줄인다.
            # ★ 원본 점 수도 남긴다: 웨이퍼 단위인데 차트가 성기면
            #   단위가 잘못된 게 아니라 다운샘플 때문임을 구분할 수 있어야 한다.
            it['n_raw'] = len(ser)
            it['series'] = _downsample(ser, series_points)
        items.append(it)

    items.sort(key=lambda x: -x['severity'])

    n_bad  = sum(1 for i in items if i['status'] == '이상')
    n_warn = sum(1 for i in items if i['status'] == '주의')
    n_cp   = sum(1 for i in items if i['cp_in_sel'])

    return {
        'oper_id': oper_id, 'lot_cd': lot_cd, 'sel_desc': sel_desc,
        'unit': unit, 'n_param': len(items), 'n_total': len(all_params),
        'n_bad': n_bad, 'n_warn': n_warn, 'n_cp': n_cp,
        'items': items,
    }


def _numeric_params(cur, table):
    """스캔 대상 숫자 컬럼 (메타·파생 제외)"""
    num = {'smallint', 'integer', 'bigint', 'decimal',
           'numeric', 'real', 'double precision'}
    meta = {'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
            'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
            'WF_ID', 'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY',
            'REWORK_N', 'MEAS_N'}
    cur.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = %s ORDER BY ordinal_position
    """, [table])
    return [c.upper() for c, d in cur.fetchall()
            if d.lower() in num and c.upper() not in meta
            and not c.upper().endswith(('_OFFSET', '_FORMULA'))]


def _bulk_stats(cur, table, params, where, args, chunk):
    """여러 파라미터의 통계를 한 쿼리로 — 쿼리 수를 1/chunk 로 줄인다"""
    out = {}
    for i in range(0, len(params), chunk):
        grp = params[i:i + chunk]
        sel = ", ".join(
            f'COUNT("{p}"), AVG("{p}"), STDDEV("{p}"), MIN("{p}"), MAX("{p}")'
            for p in grp)
        cur.execute(f'SELECT {sel} FROM {table} WHERE {where}', args)
        row = cur.fetchone()
        for j, p in enumerate(grp):
            n, avg, std, mn, mx = row[j * 5:j * 5 + 5]
            out[p] = {'n': n or 0, 'avg': _f(avg), 'std': _f(std),
                      'min': _f(mn), 'max': _f(mx)}
    return out


def _bulk_out_count(cur, table, params, where, args, stat_out, chunk):
    """나머지 범위 밖 웨이퍼 수 — CASE 로 묶어 한 쿼리에"""
    out = {}
    usable = [p for p in params
              if stat_out.get(p, {}).get('min') is not None
              and stat_out.get(p, {}).get('max') is not None]
    for i in range(0, len(usable), chunk):
        grp = usable[i:i + chunk]
        parts, vals = [], []
        for p in grp:
            parts.append(f'SUM(CASE WHEN "{p}" IS NOT NULL '
                         f'AND ("{p}" < %s OR "{p}" > %s) THEN 1 ELSE 0 END)')
            vals += [stat_out[p]['min'], stat_out[p]['max']]
        cur.execute(f'SELECT {", ".join(parts)} FROM {table} WHERE {where}',
                    vals + args)
        row = cur.fetchone()
        for j, p in enumerate(grp):
            out[p] = int(row[j] or 0)
    return out


def _bulk_series(cur, table, params, base_where, base_args,
                 sel_sql, sel_args, have, unit, parts=None):
    """
    전 파라미터의 시계열을 한 번에 읽는다.

    기본은 랏 평균 — (랏 수 × 파라미터 수) 라 가볍고 노이즈도 적다.
    웨이퍼 단위는 파라미터가 많으면 메모리가 위험해 상세 보기에서만 쓴다.

    parts 를 주면 구간/랏 소속을 따로 담는다 (3색 표시용).
    """
    rng, lots = (parts or (None, None))
    extra, extra_args = [], []
    if rng:
        extra.append(rng[0]); extra_args += rng[1]
    else:
        extra.append('FALSE')
    if lots:
        extra.append(lots[0]); extra_args += lots[1]
    else:
        extra.append('FALSE')

    if unit == 'lot' and 'LOT_ID' in have:
        aggs = ", ".join(f'AVG("{p}")' for p in params)
        eagg = ", ".join(f'BOOL_OR({e})' for e in extra)
        cur.execute(f'''
            SELECT MIN("DATE"), {LOT_KEY_SQL}, BOOL_OR({sel_sql}), {eagg}, {aggs}
            FROM {table} WHERE {base_where}
            GROUP BY {LOT_KEY_SQL} ORDER BY MIN("DATE")
        ''', sel_args + extra_args + base_args)
        rows = cur.fetchall()
        labels = [{'x': str(r[0])[:19], 'label': r[1], 'in_sel': bool(r[2]),
                   'in_range': bool(r[3]), 'in_lots': bool(r[4])}
                  for r in rows]
        offset = 5
    else:
        cols = ", ".join(f'"{p}"' for p in params)
        esel = ", ".join(f'({e})' for e in extra)
        cur.execute(f'''
            SELECT "DATE", ({sel_sql}), {esel}, {cols}
            FROM {table} WHERE {base_where} ORDER BY "DATE"
        ''', sel_args + extra_args + base_args)
        rows = cur.fetchall()
        labels = [{'x': str(r[0])[:19], 'label': '', 'in_sel': bool(r[1]),
                   'in_range': bool(r[2]), 'in_lots': bool(r[3])}
                  for r in rows]
        offset = 4

    series_map = {}
    for j, p in enumerate(params):
        pts = []
        for i, r in enumerate(rows):
            v = r[offset + j]
            if v is not None:
                pts.append({'x': labels[i]['x'], 'v': float(v),
                            'in_sel': labels[i]['in_sel'],
                            'in_range': labels[i]['in_range'],
                            'in_lots': labels[i]['in_lots']})
        series_map[p] = pts
    return series_map, labels


def _judge_one(param, ptype, s_in, s_out, out_cnt, series, sel_flag):
    """파라미터 1건 판정 — analyze() 와 같은 기준"""
    it = {
        'param': param, 'ptype': ptype,
        'sel': s_in, 'base': s_out, 'out_cnt': out_cnt,
        'sigma': None, 'spread': None,
        'status': '정상', 'checks': [], 'reasons': [],
        'cps': [], 'cp_in_sel': False, 'cp_note': '', 'severity': 0,
    }

    if not s_in.get('n'):
        it['status'] = '데이터없음'
        it['reasons'] = ['지정 구간에 값이 없습니다']
        return it
    if not s_out.get('n') or s_out.get('avg') is None:
        it['status'] = '기준없음'
        it['reasons'] = ['비교할 나머지 데이터가 없습니다']
        return it

    b_avg, b_std = s_out['avg'], s_out['std']

    # 변곡점은 타입과 무관하게 찾는다 (PART 도 추이는 볼 가치가 있다)
    ds = _downsample(series)
    cps = _find_change_points(ds, b_std) if len(ds) >= CP_MIN_SEG * 2 else []
    for c in cps:
        i = c['index']
        c['in_sel'] = bool(ds[i]['in_sel']) if 0 <= i < len(ds) else False
    it['cps'] = cps
    it['cp_in_sel'] = any(c.get('in_sel') for c in cps)

    if len(cps) >= 3 and len({c['direction'] for c in cps}) == 1:
        it['cp_note'] = f"{cps[0]['direction']} 드리프트"

    # 소모품은 σ 판정을 하지 않는다 (누적·리셋)
    if ptype == 'PART':
        it['status'] = '참고'
        it['reasons'] = ['소모품 계열 — 구간 비교 판정 제외 (변곡점만 참고)']
        if it['cp_in_sel']:
            c = [x for x in cps if x.get('in_sel')][0]
            it['reasons'].append(f"{c['at']} {c['direction']} "
                                 f"({c['shift_sigma']:+.1f}σ)")
        return it

    level = 0
    reasons, checks = [], []

    if b_std and b_std > 0:
        sigma = (s_in['avg'] - b_avg) / b_std
        it['sigma'] = round(sigma, 2)
        if abs(sigma) >= SIGMA_ALERT:
            level = 2
            reasons.append(f'평균이 나머지 대비 {sigma:+.1f}σ '
                           f'({s_in["avg"]} vs {b_avg})')
            checks.append('L')
        elif abs(sigma) >= SIGMA_WARN:
            level = max(level, 1)
            reasons.append(f'평균이 나머지 대비 {sigma:+.1f}σ')
            checks.append('L')

    if out_cnt >= OUT_ALERT:
        level = 2
        reasons.append(f'나머지 범위 밖 {out_cnt}장')
        checks.append('R')
    elif out_cnt >= OUT_WARN:
        level = max(level, 1)
        reasons.append(f'나머지 범위 밖 {out_cnt}장')
        checks.append('R')

    if b_std and b_std > 0 and s_in.get('std') and s_in['n'] >= MIN_N:
        ratio = s_in['std'] / b_std
        it['spread'] = round(ratio, 2)
        if ratio >= SPREAD_ALERT:
            level = 2
            reasons.append(f'산포가 {ratio:.1f}배 — 조건 혼입 의심')
            checks.append('S')
        elif ratio >= SPREAD_WARN:
            level = max(level, 1)
            reasons.append(f'산포가 {ratio:.1f}배')
            checks.append('S')

    # ★ 지정 구간 안에서 수준이 바뀐 것이 이 화면의 핵심 신호다
    if it['cp_in_sel']:
        c = [x for x in cps if x.get('in_sel')][0]
        level = max(level, 1)
        reasons.append(f"{c['at']} 무렵 {c['direction']} "
                       f"({c['before_avg']} → {c['after_avg']}, "
                       f"{c['shift_sigma']:+.1f}σ)")
        checks.append('C')

    it['status']  = ['정상', '주의', '이상'][level]
    it['checks']  = checks
    it['reasons'] = reasons or ['나머지와 유의한 차이 없음']

    sev = level * 100
    if it['sigma'] is not None:
        sev += min(abs(it['sigma']), 10) * 5
    sev += min(out_cnt, 25) * 2
    if it['spread']:
        sev += max(0, it['spread'] - 1) * 10
    if it['cp_in_sel']:
        sev += 40          # 지정 구간 내 변곡은 가장 직접적인 근거
    it['severity'] = round(sev, 1)
    return it


def analyze(oper_id, lot_cd, param, sel, unit='wafer'):
    """
    이슈 구간 분석.

      unit='wafer' 웨이퍼 단위로 변곡점 탐지 (세밀)
      unit='lot'   랏 평균으로 (노이즈가 적어 큰 흐름이 보인다)

    반환: 판정 · 선택/기준 통계 · 변곡점 · 랏별 · 장비별 · 시계열
    """
    if not _safe_name(param):
        raise ValueError('파라미터 이름 형식 오류')

    table = _table(oper_id)
    with _conn().cursor() as cur:
        if not _exists(cur, table):
            raise ValueError(_no_table_msg(table, oper_id))
        have = _cols(cur, table)
        if param.upper() not in have:
            raise ValueError(f'{param} 컬럼이 없습니다')

        # 대상 범위 (device 한정)
        base_where, base_args = '1=1', []
        if lot_cd and 'LOT_CD' in have:
            base_where, base_args = '"LOT_CD" = %s', [lot_cd]

        sel_sql, sel_args, sel_desc = _sel_clause(sel, have)
        in_where  = f'({base_where}) AND ({sel_sql})'
        out_where = f'({base_where}) AND NOT ({sel_sql})'
        in_args   = base_args + sel_args
        out_args  = base_args + sel_args

        s_in  = _stats(cur, table, param, in_where, in_args)
        s_out = _stats(cur, table, param, out_where, out_args)

        r = {
            'oper_id': oper_id, 'lot_cd': lot_cd, 'param': param,
            'sel_desc': sel_desc, 'unit': unit,
            'sel': s_in, 'base': s_out,
            'sigma': None, 'out_cnt': 0, 'spread': None,
            'checks': [], 'reasons': [], 'status': '정상',
            'change_points': [], 'cp_note': '',
            'lots': [], 'eqp': [], 'series': [],
        }

        if not s_in['n']:
            r['status'] = '데이터없음'
            r['reasons'] = ['선택한 구간에 해당하는 데이터가 없습니다']
            return r
        if not s_out['n']:
            r['status'] = '기준없음'
            r['reasons'] = ['선택 구간을 뺀 나머지 데이터가 없어 비교할 수 없습니다']
            return r

        level = 0
        reasons, checks = [], []
        b_avg, b_std = s_out['avg'], s_out['std']

        # ── L 수준이탈 ───────────────────────────────────
        if b_std and b_std > 0:
            sigma = (s_in['avg'] - b_avg) / b_std
            r['sigma'] = round(sigma, 2)
            if abs(sigma) >= SIGMA_ALERT:
                level = 2
                reasons.append(f'선택 구간 평균이 나머지 대비 {sigma:+.1f}σ '
                               f'({s_in["avg"]} vs {b_avg})')
                checks.append('L-수준이탈')
            elif abs(sigma) >= SIGMA_WARN:
                level = max(level, 1)
                reasons.append(f'선택 구간 평균이 나머지 대비 {sigma:+.1f}σ')
                checks.append('L-수준이탈')

        # ── R 범위이탈 ───────────────────────────────────
        cur.execute(f'''
            SELECT COUNT(*) FROM {table}
            WHERE {in_where} AND "{param}" IS NOT NULL
              AND ("{param}" < %s OR "{param}" > %s)
        ''', in_args + [s_out['min'], s_out['max']])
        out_cnt = cur.fetchone()[0]
        r['out_cnt'] = out_cnt
        if out_cnt >= OUT_ALERT:
            level = 2
            reasons.append(f'나머지 범위({s_out["min"]}~{s_out["max"]}) 밖 '
                           f'웨이퍼 {out_cnt}장')
            checks.append('R-범위이탈')
        elif out_cnt >= OUT_WARN:
            level = max(level, 1)
            reasons.append(f'나머지 범위 밖 웨이퍼 {out_cnt}장')
            checks.append('R-범위이탈')

        # ── S 산포확대 ───────────────────────────────────
        if b_std and b_std > 0 and s_in['std'] and s_in['n'] >= MIN_N:
            ratio = s_in['std'] / b_std
            r['spread'] = round(ratio, 2)
            if ratio >= SPREAD_ALERT:
                level = 2
                reasons.append(f'선택 구간 산포가 나머지의 {ratio:.1f}배 '
                               f'(σ {s_in["std"]} vs {b_std}) — 조건 혼입 의심')
                checks.append('S-산포확대')
            elif ratio >= SPREAD_WARN:
                level = max(level, 1)
                reasons.append(f'선택 구간 산포가 나머지의 {ratio:.1f}배')
                checks.append('S-산포확대')

        # ── E 단독이탈 (장비·챔버) ───────────────────────
        key = 'EQP_CH_ID' if 'EQP_CH_ID' in have else (
              'EQP_ID' if 'EQP_ID' in have else None)
        if key:
            cur.execute(f'''
                SELECT "{key}", COUNT(*), AVG("{param}")
                FROM {table}
                WHERE {in_where} AND "{param}" IS NOT NULL
                  AND COALESCE("{key}", '') <> ''
                GROUP BY "{key}" ORDER BY 1
            ''', in_args)
            for eqp, n, avg in cur.fetchall():
                es = round((float(avg) - b_avg) / b_std, 2) \
                     if b_std and b_std > 0 else None
                r['eqp'].append({'eqp': eqp, 'n': n, 'avg': _f(avg),
                                 'sigma': es})
            if len(r['eqp']) >= 2:
                hot  = [e for e in r['eqp']
                        if e['sigma'] is not None and abs(e['sigma']) >= EQP_SIGMA]
                calm = [e for e in r['eqp']
                        if e['sigma'] is not None and abs(e['sigma']) < SIGMA_WARN]
                if len(hot) == 1 and calm:
                    level = 2
                    reasons.append(f"{hot[0]['eqp']} 단독 이탈 "
                                   f"({hot[0]['sigma']:+.1f}σ, {hot[0]['n']}장)")
                    checks.append('E-단독이탈')

        r['status']  = ['정상', '주의', '이상'][level]
        r['checks']  = checks
        r['reasons'] = reasons or ['나머지 구간과 유의한 차이가 없습니다']

        # ── 랏별 판정 — "특정 랏만 이상했나" ─────────────
        if 'LOT_ID' in have:
            r['lots'] = _lot_breakdown(cur, table, param, base_where, base_args,
                                       sel_sql, sel_args, b_avg, b_std)

        # ── 시계열 + 변곡점 ──────────────────────────────
        parts = _part_clauses(sel, have)
        r['series'], r['change_points'], r['cp_note'] = _series_and_cp(
            cur, table, param, base_where, base_args,
            sel_sql, sel_args, b_std, unit, have, parts=parts)

        # 선택 구간 안에서 일어난 변곡점 — 이슈 구간 판정의 직접 근거
        inside = [c for c in r['change_points'] if c.get('in_sel')]
        if inside:
            c = inside[0]
            r['reasons'].append(
                f"선택 구간 내 {c['at']} 무렵 수준이 {c['direction']} "
                f"({c['before_avg']} → {c['after_avg']}, {c['shift_sigma']:+.1f}σ)")
            if 'C-변곡점' not in r['checks']:
                r['checks'].append('C-변곡점')

    return r


def _lot_breakdown(cur, table, param, base_where, base_args,
                   sel_sql, sel_args, b_avg, b_std):
    """
    랏별 평균과 이탈 정도.
    선택 구간에 든 랏은 in_sel=True 로 표시해, 지목한 랏만 이상한지
    아니면 다른 랏도 같이 이상한지 한눈에 볼 수 있게 한다.
    """
    # ★ 7자리 키로 그룹 — 9자리가 섞여도 같은 랏으로 묶인다
    cur.execute(f'''
        SELECT {LOT_KEY_SQL}, COUNT("{param}"), AVG("{param}"), STDDEV("{param}"),
               MIN("DATE"),
               BOOL_OR({sel_sql}) AS in_sel
        FROM {table}
        WHERE ({base_where}) AND "{param}" IS NOT NULL AND "LOT_ID" IS NOT NULL
        GROUP BY {LOT_KEY_SQL} ORDER BY MIN("DATE")
    ''', sel_args + base_args)

    out = []
    for lot_id, n, avg, std, dt, in_sel in cur.fetchall():
        sig = round((float(avg) - b_avg) / b_std, 2) \
              if (b_std and b_std > 0 and avg is not None) else None
        st = '정상'
        if sig is not None:
            if abs(sig) >= SIGMA_ALERT:
                st = '이상'
            elif abs(sig) >= SIGMA_WARN:
                st = '주의'
        out.append({
            'lot_id': lot_id, 'n': n, 'avg': _f(avg), 'std': _f(std),
            'sigma': sig, 'status': st, 'in_sel': bool(in_sel),
            'date': str(dt)[:19] if dt else '',
            'low_n': n < LOT_MIN_N,
        })
    return out


def _series_and_cp(cur, table, param, base_where, base_args,
                   sel_sql, sel_args, b_std, unit, have, parts=None):
    """
    시계열(차트용)과 변곡점.

    ★ parts=(range_clause, lots_clause) 를 주면 각 점에 in_range / in_lots 를
      따로 담는다. 화면에서 '이슈 구간 전체'와 '그중 지정한 랏'을
      다른 색으로 보여 주기 위한 것이다.
    """
    rng, lots = (parts or (None, None))
    extra_sel, extra_args = [], []
    if rng:
        extra_sel.append(rng[0]); extra_args += rng[1]
    else:
        extra_sel.append('FALSE')
    if lots:
        extra_sel.append(lots[0]); extra_args += lots[1]
    else:
        extra_sel.append('FALSE')

    if unit == 'lot' and 'LOT_ID' in have:
        agg = ", ".join(f'BOOL_OR({e})' for e in extra_sel)
        cur.execute(f'''
            SELECT {LOT_KEY_SQL}, MIN("DATE"), AVG("{param}"), COUNT(*),
                   BOOL_OR({sel_sql}), {agg}
            FROM {table}
            WHERE ({base_where}) AND "{param}" IS NOT NULL
            GROUP BY {LOT_KEY_SQL} ORDER BY MIN("DATE")
        ''', sel_args + extra_args + base_args)
        series = [{'x': str(r[1])[:19], 'label': r[0], 'v': float(r[2]),
                   'n': r[3], 'in_sel': bool(r[4]),
                   'in_range': bool(r[5]), 'in_lots': bool(r[6])}
                  for r in cur.fetchall() if r[2] is not None]
    else:
        sel_extra = ", ".join(f'({e})' for e in extra_sel)
        # ★ 웨이퍼 식별자는 '랏 7자리 + wf_id' 로 만든다.
        #   LOT_ID 에 9자리가 섞여 있어도 같은 규칙으로 붙어야
        #   다른 화면·다른 조회 결과와 웨이퍼가 서로 맞는다.
        has_wf = 'WF_ID' in have and 'LOT_ID' in have
        wf_sel = (f", {LOT_KEY_SQL} || '.' || CAST(\"WF_ID\" AS VARCHAR)"
                  if has_wf else ", NULL")
        cur.execute(f'''
            SELECT id, "DATE", "{param}", ({sel_sql}), {sel_extra}{wf_sel}
            FROM {table}
            WHERE ({base_where}) AND "{param}" IS NOT NULL
            ORDER BY "DATE"
        ''', sel_args + extra_args + base_args)
        series = [{'x': str(r[1])[:19], 'id': r[0], 'v': float(r[2]),
                   'in_sel': bool(r[3]),
                   'in_range': bool(r[4]), 'in_lots': bool(r[5]),
                   'label': r[6] or ''}
                  for r in cur.fetchall()]

    # ★ 변곡점은 다운샘플한 시계열에서 찾는다 (탐지 비용을 일정하게 유지)
    ds = _downsample(series)
    cps = _find_change_points(ds, b_std) if len(ds) >= CP_MIN_SEG * 2 else []

    # 변곡점이 선택 구간 안에서 일어났는지 표시 —
    #   "이슈 구간에서 수준이 바뀌었다" 를 확인하는 핵심 정보다.
    #   인덱스는 다운샘플된 시계열 기준이므로 그쪽에서 읽는다.
    for c in cps:
        i = c['index']
        c['in_sel'] = bool(ds[i]['in_sel']) if 0 <= i < len(ds) else False

    # ★ 같은 방향 변곡점이 여러 개면 계단이 아니라 드리프트다.
    #   이진 분할은 완만한 추세를 여러 계단으로 쪼개므로, 그대로 보여주면
    #   "변곡점이 3개" 로 잘못 읽힌다. 성격을 함께 알려준다.
    note = ''
    if len(cps) >= 3 and len({c['direction'] for c in cps}) == 1:
        total = sum(c['shift'] or 0 for c in cps)
        note = (f"변곡점이 {len(cps)}개인데 모두 {cps[0]['direction']} 방향입니다 — "
                f"계단식 변화가 아니라 서서히 이동하는 드리프트로 보입니다 "
                f"(누적 {_f(total)})")
    elif len(cps) == 1:
        c = cps[0]
        note = (f"{c['at']} 무렵 수준이 {c['direction']}했습니다 "
                f"({c['before_avg']} → {c['after_avg']}, {c['shift_sigma']:+.1f}σ)")

    # 차트가 무거워지지 않게 표본을 줄인다 (판정은 전체로 이미 끝났다)
    MAX_PTS = 3000
    if len(series) > MAX_PTS:
        step = len(series) // MAX_PTS + 1
        series = series[::step]

    return series, cps, note
