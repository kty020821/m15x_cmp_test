"""
════════════════════════════════════════════════════════════
equipment/action_rules.py
────────────────────────────────────────────────────────────
조치 권고 — 코드가 규칙을 판정하고, 서술만 LLM 에 맡긴다

  판정을 LLM 에 시키면 조건을 잘못 읽고 엉뚱한 권고를 낼 수 있다.
  "THK 높은데 Time 이 max 이하" 같은 건 참/거짓이 명확하므로
  코드가 확정하고, LLM 은 그 결과를 문장으로 풀어주기만 한다.

  규칙 (사용자 정의)
    R1  이상 시점 ≈ PM 시점            → 장비 점검
    R2  Polishing Time 이 기준 이탈     → 장비 점검
    R3  THK 높은데 Time 이 30일 max 이하 → APC 수정
    R4  idle 첫 웨이퍼 THK 낮은데
        같은 lot 이 동일 Time 진행       → idle 첫 장 오프셋
════════════════════════════════════════════════════════════
"""

import re
from django.db import connections

from datetime import timedelta

from .pm_detect import find_pm_times, _counter_cols, anomaly_time_range
from .monitoring import is_time_param, _baseline

# 변경점(이상 구간 시작) 직전 이 시간 이내의 PM 만 관련으로 본다.
# PM 직후 특성을 잡는 게 아니라, 변경점 무렵의 주요 이벤트로서 PM 을 띄우는 것.
PM_LOOKBACK_HOURS = 2


def _conn():
    return connections['analysis_db']


def _an_table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def _cols(table):
    with _conn().cursor() as cur:
        cur.execute("""
            SELECT upper(column_name) FROM information_schema.columns
            WHERE table_name = %s
        """, [table])
        return {r[0] for r in cur.fetchall()}


def _time_params(table):
    """이 테이블의 polishing time 계열 컬럼"""
    return [c for c in _cols(table) if is_time_param(c)]


def _is_thk(param):
    return bool(re.search(r'THK|OCD', param.upper()))


# ══════════════════════════════════════════════════════════
# 규칙별 판정
#   각 함수는 해당하면 dict, 아니면 None 을 반환
# ══════════════════════════════════════════════════════════
def rule_pm(table, oper_id, anomaly):
    """
    R1. 변경점(이상 구간 시작) 무렵에 PM 이 있었나.

    PM 직후 특성을 잡으려는 게 아니라, 갑작스런 변경점의 원인 후보로
    'PM 이라는 이벤트' 를 띄우는 것. 그래서 이상 구간 시작 시각(t0)
    직전 PM_LOOKBACK_HOURS 이내에 카운터 리셋이 있으면 관련으로 본다.
    """
    eqp_id = anomaly.get('eqp_id')
    if not eqp_id:
        return None
    t0, _t1 = anomaly_time_range(table, anomaly['wafer_ids'])
    if t0 is None:
        return None

    lo = t0 - timedelta(hours=PM_LOOKBACK_HOURS)

    hit = []
    with _conn().cursor() as cur:
        for label, col in _counter_cols(cur, table):
            times = find_pm_times(cur, table, eqp_id, col)
            # 변경점 직전 창(lo ~ t0) 안의 PM 만
            if any(lo <= t <= t0 for t in times):
                hit.append(label)

    if not hit:
        return None
    return {
        'rule': 'R1_PM',
        'action': '장비 점검',
        'reason': f"{eqp_id} 의 {', '.join(hit)} PM 이 변경점 직전에 있었음",
        'parts': hit,
    }


def rule_time_out(table, oper_id, lot_cd, target, anomaly):
    """R2. polishing time 이 기준(30일 min/max)을 벗어났나"""
    eqp_id = anomaly.get('eqp_id')
    ph = ",".join(["%s"] * len(anomaly['wafer_ids']))
    hits = []

    with _conn().cursor() as cur:
        for tp in _time_params(table):
            base = _baseline(cur, table, tp, lot_cd, target)
            if base['min'] is None or base['max'] is None:
                continue
            cur.execute(f'''
                SELECT MIN("{tp}"), MAX("{tp}") FROM {table}
                WHERE id IN ({ph}) AND "{tp}" IS NOT NULL
            ''', anomaly['wafer_ids'])
            smin, smax = cur.fetchone()
            if smin is None:
                continue
            if float(smin) < float(base['min']) or float(smax) > float(base['max']):
                hits.append({
                    'param': tp,
                    'sel_range': [round(float(smin), 2), round(float(smax), 2)],
                    'base_range': [round(float(base['min']), 2),
                                   round(float(base['max']), 2)],
                })

    if not hits:
        return None
    return {
        'rule': 'R2_TIME_OUT',
        'action': '장비 점검',
        'reason': 'polishing time 이 30일 기준 범위를 벗어남',
        'params': hits,
    }


def rule_apc(table, oper_id, lot_cd, target, anomaly):
    """
    R3. THK 는 높은데 polishing time 은 30일 max 이하 → APC 수정

    두께를 더 깎아야 하는데 시간을 안 늘렸다는 뜻 →
    시간(설비)이 아니라 APC(목표 보정) 쪽 문제.
    """
    param = anomaly['param']
    if not _is_thk(param):
        return None

    # THK 가 '높은' 이상인지 확인 (SHIFT 방향 또는 상단 이탈)
    d = anomaly.get('detail', {})
    high = (d.get('direction') == '높음') or \
           (d.get('day_max') is not None and d.get('base_max') is not None
            and d['day_max'] > d['base_max'])
    if not high:
        return None

    ph = ",".join(["%s"] * len(anomaly['wafer_ids']))
    exceeded = []          # 시간이 max 를 넘긴 time param
    within   = []          # 시간이 max 이하인 time param

    with _conn().cursor() as cur:
        for tp in _time_params(table):
            base = _baseline(cur, table, tp, lot_cd, target)
            if base['max'] is None:
                continue
            cur.execute(f'''
                SELECT MAX("{tp}") FROM {table}
                WHERE id IN ({ph}) AND "{tp}" IS NOT NULL
            ''', anomaly['wafer_ids'])
            smax = cur.fetchone()[0]
            if smax is None:
                continue
            if float(smax) > float(base['max']):
                exceeded.append(tp)
            else:
                within.append(tp)

    # 시간을 늘린 적 없이(모두 max 이하) THK 가 높다 → APC
    if within and not exceeded:
        return {
            'rule': 'R3_APC',
            'action': 'APC 수정',
            'reason': f"THK 가 높은데 polishing time 은 30일 최대 이하 "
                      f"({', '.join(within)}) — 시간 여유가 있는데 안 깎임",
        }
    return None


def rule_idle_offset(table, oper_id, anomaly):
    """
    R4. idle 첫 웨이퍼 THK 낮은데 같은 lot 이 동일 time 진행
        → idle 첫 장에 오프셋 필요

    idle_1 웨이퍼가 나머지와 같은 시간을 돌았는데 THK 만 낮다면,
    시간 문제가 아니라 idle 직후 상태 차이 → 첫 장 보정이 답.
    """
    param = anomaly['param']
    if not _is_thk(param):
        return None
    have = _cols(table)
    if 'IDLE' not in have:
        return None

    ph = ",".join(["%s"] * len(anomaly['wafer_ids']))
    time_params = _time_params(table)
    if not time_params:
        return None
    tp = time_params[0]        # 대표 time 하나로 비교

    with _conn().cursor() as cur:
        # 선택 구간에 idle 첫 장(idle_1)이 있는지
        cur.execute(f'''
            SELECT "LOT_ID", "{param}", "{tp}"
            FROM {table}
            WHERE id IN ({ph}) AND "IDLE" = 'idle_1' AND "{param}" IS NOT NULL
        ''', anomaly['wafer_ids'])
        idle_rows = cur.fetchall()
        if not idle_rows:
            return None

        matched = []
        for lot_id, idle_thk, idle_time in idle_rows:
            if idle_time is None:
                continue
            # 같은 lot 의 idle 아닌 웨이퍼 평균 THK / time
            cur.execute(f'''
                SELECT AVG("{param}"), AVG("{tp}")
                FROM {table}
                WHERE "LOT_ID" = %s
                  AND (COALESCE("IDLE",'') = '' OR "IDLE" = 'Normal')
                  AND "{param}" IS NOT NULL
            ''', [lot_id])
            rest_thk, rest_time = cur.fetchone()
            if rest_thk is None or rest_time is None:
                continue
            # 같은 시간인데(±5%) idle 첫 장 THK 만 낮다
            same_time = abs(float(idle_time) - float(rest_time)) <= abs(float(rest_time)) * 0.05
            lower_thk = float(idle_thk) < float(rest_thk)
            if same_time and lower_thk:
                matched.append({
                    'lot_id': lot_id,
                    'idle_thk': round(float(idle_thk), 2),
                    'rest_thk': round(float(rest_thk), 2),
                })

    if not matched:
        return None
    return {
        'rule': 'R4_IDLE_OFFSET',
        'action': 'idle 첫 웨이퍼 오프셋',
        'reason': 'idle 첫 장이 같은 lot 과 동일 시간인데 THK 만 낮음',
        'cases': matched,
    }


# ══════════════════════════════════════════════════════════
# 통합
# ══════════════════════════════════════════════════════════
def recommend(oper_id, lot_cd, target, anomaly):
    """
    한 이상 건에 대해 해당하는 조치 규칙을 모두 판정.
    반환: [{rule, action, reason, ...}, ...]  (없으면 빈 리스트)
    """
    table = _an_table(oper_id)
    out = []
    for fn, args in [
        (rule_pm,        (table, oper_id, anomaly)),
        (rule_time_out,  (table, oper_id, lot_cd, target, anomaly)),
        (rule_apc,       (table, oper_id, lot_cd, target, anomaly)),
        (rule_idle_offset, (table, oper_id, anomaly)),
    ]:
        try:
            r = fn(*args)
            if r:
                out.append(r)
        except Exception as e:
            print(f'  [{anomaly.get("param")}] {fn.__name__} 오류: {e}')
    return out
