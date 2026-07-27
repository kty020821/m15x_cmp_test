"""
════════════════════════════════════════════════════════════
equipment/pm_detect.py
────────────────────────────────────────────────────────────
소모품 카운터로 PM 시점 역산

  별도 PM 이력이 없으므로, 장비별로 소모품 카운터를
  request_dtts(=DATE) 오름차순으로 정렬했을 때 값이 이전보다
  작아지는 시점을 그 부품의 PM(교체) 으로 본다.
  (교체하면 사용량 카운터가 리셋되기 때문)

  PM 은 매일 하지 않으므로 '시점 근접' 조건은 없다.
  카운터가 작아졌으면 무조건 PM 이고, 그 이후 진행된 웨이퍼는
  모두 '해당 부품 PM 이후' 구간에 속한다.
  이상 웨이퍼가 그 구간에 들어가면 PM 관련으로 판정한다.

  monitoring.py 와 함께 사용.
════════════════════════════════════════════════════════════
"""

from django.db import connections


# ══════════════════════════════════════════════════════════
# 모델별 소모품 카운터 컬럼
#   ★ 실제 컬럼명으로 채울 것. (부품 표시명, 컬럼명)
#     사용량 누적형(교체 시 값이 작아짐)이어야 한다.
# ══════════════════════════════════════════════════════════
PM_COUNTER_COLS = {
    'OPTA': [
        # ('Pad',  'AMAT_PAD_1'),
        # ('Head', 'AMAT_HEAD_1'),
        # ('Disk', 'AMAT_DISK_1'),
    ],
    'EBARA': [
        # ('Pad',  'EBARA_PAD_USE'),
    ],
    'KCT_NTA': [
        # ('Pad',  'KCT_PAD_CNT'),
    ],
    'KCT_NTH': [
    ],
}


def _conn():
    return connections['analysis_db']


def _model_of(cur, table):
    cur.execute(f'SELECT DISTINCT "EQP_MODEL" FROM {table} '
                f'WHERE "EQP_MODEL" IS NOT NULL')
    return [str(r[0]).upper() for r in cur.fetchall()]


def _counter_cols(cur, table):
    """이 테이블에 실제로 존재하는 소모품 카운터 컬럼"""
    cur.execute("""
        SELECT upper(column_name) FROM information_schema.columns
        WHERE table_name = %s
    """, [table])
    have = {r[0] for r in cur.fetchall()}

    out, seen = [], set()
    for model in _model_of(cur, table):
        for label, col in PM_COUNTER_COLS.get(model, []):
            cu = col.upper()
            if cu in have and cu not in seen:
                out.append((label, cu))
                seen.add(cu)
    return out


# ══════════════════════════════════════════════════════════
# PM 시점 탐지
# ══════════════════════════════════════════════════════════
def find_pm_times(cur, table, eqp_id, col):
    """
    한 장비·한 부품 카운터의 모든 PM(리셋) 시각.
    DATE 오름차순으로 훑어 값이 직전보다 작아지는 지점.
    (조금이라도 작아지면 PM)
    """
    cur.execute(f'''
        SELECT "DATE", "{col}" FROM {table}
        WHERE "EQP_ID" = %s AND "{col}" IS NOT NULL
        ORDER BY "DATE"
    ''', [eqp_id])
    rows = cur.fetchall()

    times, last = [], None
    for dt, val in rows:
        v = float(val)
        if last is not None and v < last:      # 작아졌다 = PM
            times.append(dt)
        last = v
    return times


def pm_before(table, eqp_id, anomaly_time):
    """
    이상 시점 이전의 가장 최근 PM 이 있었던 부품 목록.
    반환: [{'part': 'Pad', 'pm_time': datetime}, ...]

    '이상 웨이퍼가 이 부품 PM 이후 구간에 있다' 를 뜻한다.
    """
    if eqp_id is None or anomaly_time is None:
        return []

    out = []
    with _conn().cursor() as cur:
        for label, col in _counter_cols(cur, table):
            times = find_pm_times(cur, table, eqp_id, col)
            before = [t for t in times if t <= anomaly_time]
            if before:
                out.append({'part': label, 'pm_time': max(before)})
    return out


def anomaly_time_range(table, wafer_ids):
    """이상 웨이퍼들의 진행 시각 범위"""
    if not wafer_ids:
        return None, None
    ph = ",".join(["%s"] * len(wafer_ids))
    with _conn().cursor() as cur:
        cur.execute(f'SELECT MIN("DATE"), MAX("DATE") FROM {table} '
                    f'WHERE id IN ({ph})', wafer_ids)
        return cur.fetchone()
