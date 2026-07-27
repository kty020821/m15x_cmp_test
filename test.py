"""
════════════════════════════════════════════════════════════
equipment/monitoring.py
────────────────────────────────────────────────────────────
아침 Inline Trend 점검 자동화 — 1단계: 이상 탐지

  최근 30일 데이터 자체를 기준선으로 삼아,
  어제 하루 데이터가 기준선에서 벗어났는지 판정한다.

  판정 결과에는 해당 웨이퍼 id 목록이 들어가므로,
  기존 분석 엔진(편중도·상관·LLM)을 그대로 재사용할 수 있다.
  화면에서 드래그한 것과 동일한 입력이 되는 셈.

  실행: run_monitoring.py (야간 배치가 적재를 끝낸 뒤 호출)
════════════════════════════════════════════════════════════
"""

import re
import json
from datetime import date, timedelta

from django.db import connections


# ══════════════════════════════════════════════════════════
# 1. 감시 대상 파라미터
#    ★ 감시 범위를 넓히려면 여기에 패턴 추가
#      (Pressure 는 영향이 작아 현재 제외)
# ══════════════════════════════════════════════════════════
WATCH_PATTERNS = [
    r'THK',        # 두께
    r'OCD',        # 두께 계열 측정
    r'\d_TIME',    # polishing time (04_TIME, P1_TIME 등 숫자+TIME)
]

# polishing time 계열만 따로 식별 (조치 규칙에서 사용)
TIME_PATTERN = r'\d_TIME'


# ══════════════════════════════════════════════════════════
# 2. 판정 기준
#    ★ 오탐이 많으면 SHIFT_SIGMA 를 올리고,
#      놓치는 게 많으면 내린다
# ══════════════════════════════════════════════════════════
SHIFT_SIGMA   = 2.0    # 어제 평균이 기준 평균에서 몇 σ 벗어나면 이상으로 볼지
RANGE_MARGIN  = 0.0    # 기준 최대/최소를 얼마나 넘어야 이탈로 볼지 (σ 단위)

MIN_N_TODAY   = 10     # 어제 웨이퍼가 이보다 적으면 신뢰도 낮음으로 표시
MIN_N_EQP     = 5      # 장비별 판정에 필요한 최소 웨이퍼 수
MIN_N_BASE    = 100    # 기준선 계산에 필요한 최소 웨이퍼 수

BASELINE_DAYS = 30     # 기준선 기간


# ══════════════════════════════════════════════════════════
# 3. 공통 헬퍼
# ══════════════════════════════════════════════════════════
def _conn():
    return connections['analysis_db']


def list_analysis_tables():
    """적재된 분석 테이블 → [(테이블명, oper_id), ...]"""
    with _conn().cursor() as cur:
        cur.execute("""
            SELECT tablename FROM pg_tables
            WHERE tablename LIKE 'cmp_analysis_%'
            ORDER BY tablename
        """)
        rows = [r[0] for r in cur.fetchall()]
    return [(t, t.replace('cmp_analysis_', '').upper()) for t in rows]


def numeric_cols(table):
    """숫자형 컬럼 (측정값 후보)"""
    with _conn().cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = %s ORDER BY ordinal_position
        """, [table])
        rows = cur.fetchall()

    numeric = {'smallint', 'integer', 'bigint',
               'decimal', 'numeric', 'real', 'double precision'}
    return [c.upper() for c, t in rows if t.lower() in numeric]


def watch_params(table):
    """감시 대상 파라미터만 선별"""
    return [c for c in numeric_cols(table)
            if any(re.search(p, c) for p in WATCH_PATTERNS)]


def is_time_param(col):
    return bool(re.search(TIME_PATTERN, col))


def lot_cds(table):
    with _conn().cursor() as cur:
        cur.execute(f'SELECT DISTINCT "LOT_CD" FROM {table} WHERE "LOT_CD" IS NOT NULL')
        return [r[0] for r in cur.fetchall()]


def _f(v):
    return round(float(v), 3) if v is not None else None


# ══════════════════════════════════════════════════════════
# 4. 기준선 / 어제 통계
# ══════════════════════════════════════════════════════════
def _stats(cur, table, param, lot_cd, where_extra, params):
    cur.execute(f'''
        SELECT COUNT("{param}"), AVG("{param}"), STDDEV("{param}"),
               MIN("{param}"), MAX("{param}")
        FROM {table}
        WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL {where_extra}
    ''', [lot_cd] + params)
    n, avg, std, mn, mx = cur.fetchone()
    return {'n': n or 0, 'avg': avg, 'std': std, 'min': mn, 'max': mx}


def _baseline(cur, table, param, lot_cd, target):
    """기준선 = target 이전 BASELINE_DAYS 일 (target 당일은 제외)"""
    start = target - timedelta(days=BASELINE_DAYS)
    return _stats(cur, table, param, lot_cd,
                  'AND "DATE" >= %s AND "DATE" < %s', [start, target])


def _today(cur, table, param, lot_cd, target, eqp_id=None):
    """target 하루치. eqp_id 를 주면 해당 장비만"""
    extra  = 'AND "DATE" >= %s AND "DATE" < %s'
    params = [target, target + timedelta(days=1)]
    if eqp_id:
        extra += ' AND "EQP_ID" = %s'
        params.append(eqp_id)
    return _stats(cur, table, param, lot_cd, extra, params)


def _eqp_list(cur, table, param, lot_cd, target):
    """target 당일에 이 파라미터를 진행한 장비 목록"""
    cur.execute(f'''
        SELECT "EQP_ID", COUNT(*) FROM {table}
        WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL
          AND "DATE" >= %s AND "DATE" < %s
          AND "EQP_ID" IS NOT NULL
        GROUP BY "EQP_ID"
    ''', [lot_cd, target, target + timedelta(days=1)])
    return [(r[0], r[1]) for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════
# 5. 웨이퍼 id 추출
#    분석 엔진이 그대로 받아 쓰는 형태
# ══════════════════════════════════════════════════════════
def _ids_out_of_range(cur, table, param, lot_cd, target, base, eqp_id=None):
    """기준 최대/최소를 벗어난 어제 웨이퍼"""
    if base['min'] is None or base['max'] is None:
        return []

    margin = (float(base['std']) * RANGE_MARGIN) if base['std'] else 0.0
    lo = float(base['min']) - margin
    hi = float(base['max']) + margin

    sql = f'''
        SELECT id FROM {table}
        WHERE "LOT_CD" = %s AND "DATE" >= %s AND "DATE" < %s
          AND "{param}" IS NOT NULL
          AND ("{param}" < %s OR "{param}" > %s)
    '''
    params = [lot_cd, target, target + timedelta(days=1), lo, hi]
    if eqp_id:
        sql += ' AND "EQP_ID" = %s'
        params.append(eqp_id)

    cur.execute(sql, params)
    return [r[0] for r in cur.fetchall()]


def _ids_of_day(cur, table, param, lot_cd, target, eqp_id=None):
    """어제 진행된 웨이퍼 전체"""
    sql = f'''
        SELECT id FROM {table}
        WHERE "LOT_CD" = %s AND "DATE" >= %s AND "DATE" < %s
          AND "{param}" IS NOT NULL
    '''
    params = [lot_cd, target, target + timedelta(days=1)]
    if eqp_id:
        sql += ' AND "EQP_ID" = %s'
        params.append(eqp_id)

    cur.execute(sql, params)
    return [r[0] for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════
# 6. 판정
# ══════════════════════════════════════════════════════════
def _judge(cur, table, oper_id, lot_cd, param, target, base, scope, eqp_id=None):
    """
    한 범위(전체 또는 장비 1대)에 대한 판정.
    심각도는 σ 단위로 통일해 서로 비교 가능하게 한다.
    """
    today = _today(cur, table, param, lot_cd, target, eqp_id)
    if today['n'] == 0:
        return []

    min_n = MIN_N_EQP if eqp_id else MIN_N_TODAY
    low_confidence = today['n'] < min_n

    std = float(base['std']) if base['std'] else None
    out = []

    # ── 규칙 1. 기준 범위 이탈 ──────────────────────────
    ids = _ids_out_of_range(cur, table, param, lot_cd, target, base, eqp_id)
    if ids:
        # 가장 크게 벗어난 정도를 σ 로 환산
        worst = 0.0
        if std:
            b_min, b_max = float(base['min']), float(base['max'])
            t_min, t_max = float(today['min']), float(today['max'])
            worst = max((b_min - t_min) / std if t_min < b_min else 0.0,
                        (t_max - b_max) / std if t_max > b_max else 0.0)

        out.append({
            'oper_id': oper_id, 'lot_cd': lot_cd, 'param': param,
            'scope': scope, 'rule': 'RANGE',
            'severity': round(worst, 2),
            'n_hit': len(ids),
            'low_confidence': low_confidence,
            'detail': {
                'base_min': _f(base['min']), 'base_max': _f(base['max']),
                'base_avg': _f(base['avg']), 'base_std': _f(base['std']),
                'base_n':   base['n'],
                'day_min':  _f(today['min']), 'day_max': _f(today['max']),
                'day_avg':  _f(today['avg']), 'day_n':   today['n'],
            },
            'wafer_ids': ids,
        })

    # ── 규칙 2. 평균 이동 ───────────────────────────────
    if std and std > 0 and base['avg'] is not None:
        sigma = (float(today['avg']) - float(base['avg'])) / std
        if abs(sigma) >= SHIFT_SIGMA:
            out.append({
                'oper_id': oper_id, 'lot_cd': lot_cd, 'param': param,
                'scope': scope, 'rule': 'SHIFT',
                'severity': round(abs(sigma), 2),
                'n_hit': today['n'],
                'low_confidence': low_confidence,
                'detail': {
                    'base_avg': _f(base['avg']), 'base_std': _f(base['std']),
                    'base_n':   base['n'],
                    'day_avg':  _f(today['avg']), 'day_n': today['n'],
                    'sigma':    round(sigma, 2),
                    'direction': '높음' if sigma > 0 else '낮음',
                },
                'wafer_ids': _ids_of_day(cur, table, param, lot_cd, target, eqp_id),
            })

    return out


def detect_param(cur, table, oper_id, lot_cd, param, target):
    """
    한 파라미터에 대해 전체 + 장비별로 판정.

    전체 평균만 보면 장비 1대의 문제가 다른 장비에 희석돼 안 잡힌다.
    그래서 장비별로도 같은 판정을 돌리고, 전체는 정상인데 특정
    장비만 걸린 경우를 따로 표시한다 (그쪽이 오히려 명확한 신호).
    """
    base = _baseline(cur, table, param, lot_cd, target)
    if base['n'] < MIN_N_BASE or not base['std']:
        return []          # 기준선이 부실하면 판정하지 않는다

    results = _judge(cur, table, oper_id, lot_cd, param, target, base, 'ALL')
    all_hit = bool(results)

    for eqp_id, _n in _eqp_list(cur, table, param, lot_cd, target):
        hits = _judge(cur, table, oper_id, lot_cd, param, target,
                      base, f'EQP:{eqp_id}', eqp_id)
        for h in hits:
            h['eqp_id'] = eqp_id
            # 전체로는 안 걸렸는데 이 장비만 걸렸다 = 장비 단독 이슈
            h['eqp_only'] = not all_hit
        results.extend(hits)

    return results


def detect_all(target=None, opers=None):
    """
    전체 공정 × 제품 × 감시 파라미터 판정.
    target 기본값은 어제 (배치가 새벽에 도는 것을 전제).
    """
    if target is None:
        target = date.today() - timedelta(days=1)

    found = []
    with _conn().cursor() as cur:
        for table, oper_id in list_analysis_tables():
            if opers and oper_id not in opers:
                continue

            params = watch_params(table)
            if not params:
                continue

            for lot_cd in lot_cds(table):
                for param in params:
                    try:
                        found.extend(
                            detect_param(cur, table, oper_id, lot_cd, param, target))
                    except Exception as e:
                        print(f'  [{oper_id}/{lot_cd}/{param}] 판정 오류: {e}')

    found.sort(key=lambda x: x['severity'], reverse=True)
    return target, found


# ══════════════════════════════════════════════════════════
# 7. 저장
#    STATUS 로 확인 여부를 관리한다.
#    '이건 문제 아님'(DISMISSED) 이 쌓이면 임계값 조정 근거가 된다.
# ══════════════════════════════════════════════════════════
ANOMALY_TABLE = 'cmp_anomaly'


def ensure_table():
    with _conn().cursor() as cur:
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {ANOMALY_TABLE} (
                id              BIGSERIAL PRIMARY KEY,
                "DETECT_DATE"   DATE          NOT NULL,
                "OPER_ID"       VARCHAR(50)   NOT NULL,
                "LOT_CD"        VARCHAR(20)   NOT NULL,
                "PARAM"         VARCHAR(100)  NOT NULL,
                "SCOPE"         VARCHAR(60)   NOT NULL,
                "EQP_ID"        VARCHAR(50),
                "RULE"          VARCHAR(20)   NOT NULL,
                "SEVERITY"      DOUBLE PRECISION,
                "N_HIT"         INTEGER,
                "EQP_ONLY"      BOOLEAN DEFAULT FALSE,
                "LOW_CONF"      BOOLEAN DEFAULT FALSE,
                "DETAIL"        JSONB,
                "WAFER_IDS"     JSONB,
                "STATUS"        VARCHAR(20)   DEFAULT 'OPEN',
                "ACTION_NOTE"   TEXT,
                "CREATED_AT"    TIMESTAMP     DEFAULT NOW()
            )
        ''')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{ANOMALY_TABLE}_date '
                    f'ON {ANOMALY_TABLE} ("DETECT_DATE")')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{ANOMALY_TABLE}_status '
                    f'ON {ANOMALY_TABLE} ("STATUS")')


def save_anomalies(target, items):
    """해당 일자를 지우고 다시 넣는다 (재실행해도 중복되지 않게)"""
    ensure_table()
    with _conn().cursor() as cur:
        # 사람이 이미 확인한 건은 보존
        cur.execute(f'DELETE FROM {ANOMALY_TABLE} '
                    f'WHERE "DETECT_DATE" = %s AND "STATUS" = %s', [target, 'OPEN'])

        for it in items:
            cur.execute(f'''
                INSERT INTO {ANOMALY_TABLE}
                    ("DETECT_DATE","OPER_ID","LOT_CD","PARAM","SCOPE","EQP_ID",
                     "RULE","SEVERITY","N_HIT","EQP_ONLY","LOW_CONF",
                     "DETAIL","WAFER_IDS")
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ''', [
                target, it['oper_id'], it['lot_cd'], it['param'],
                it['scope'], it.get('eqp_id'),
                it['rule'], it['severity'], it['n_hit'],
                it.get('eqp_only', False), it.get('low_confidence', False),
                json.dumps(it['detail'], ensure_ascii=False),
                json.dumps(it['wafer_ids']),
            ])

    print(f'[{target}] 이상 {len(items)}건 저장')
