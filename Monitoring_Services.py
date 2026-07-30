"""
equipment/monitor_service.py
════════════════════════════════════════════════════════════
Inline Monitoring — 전 공정 자동 점검

  적재된 cmp_analysis_* (PostgreSQL) 만 읽는다.
  Lake/구닥스에 접근하지 않으므로 웹 프로세스에서 안전하게 돈다.
  구닥스 기준정보는 배치가 저장한 스냅샷(cmp_gooddocs_config)을 읽는다.

────────────────────────────────────────────────────────────
점검 단위
  공정 × LOT_CD × 파라미터
  ★ LOT_CD 를 섞으면 안 된다 — 5E2 와 5E9 는 목표 두께가 달라서
    합쳐 평균 내면 멀쩡한 것도 이상해 보이고 이상한 것도 묻힌다.

기준선
  스펙이 아니라 30일 데이터 자신. (테이블이 30일 롤링이므로
  '최근일 이전 전체' = 기준선)

검사 5종 — 하나만 보면 반드시 놓친다
  L 수준이탈  최근일 평균이 기준 대비 몇 σ
  R 범위이탈  30일 min/max 밖 웨이퍼 수 (평균은 멀쩡한데 몇 장만 튄 경우)
  E 단독이탈  장비·챔버 한 대만 벗어남 (전체 평균이 이를 숨긴다)
  S 산포확대  평균은 그대로, 표준편차만 커짐 (조건 혼입·챔버 불균일)
  D 드리프트  최근 7일 기울기 (아직 안 벗어났지만 곧 벗어날 것)

Defect 파라미터는 위 규칙을 쓰지 않는다
  카운트 데이터라 0이 많고 분포가 한쪽으로 쏠려 σ 판정이 무의미하다.
  중앙값 배수 · 상위 분위수 초과 · 0→검출 전환으로 본다.
  구분은 구닥스 PARAM_TYPE (없으면 이름 규칙 폴백).

점검 대상 파라미터
  1순위  구닥스 스냅샷(cmp_gooddocs_config)에 등록된 그 공정의 PARAM 전부
         + 챔버 짝 자동 확장 (PA↔PC, PB↔PD, PL↔PR)
         구닥스에는 점검할 항목만 넣고 관리하므로 Y/N 표시가 필요 없다.
         INLINE_YN 컬럼이 있으면 '명시적으로 N' 인 것만 제외한다.
  폴백   스냅샷이 없으면 이름 규칙 (THK/OCD/REV, 숫자 붙은 TIME, defect 어휘)

  어느 쪽이 적용됐고 무엇이 왜 빠졌는지는 explain_params(oper_id) 로 확인.

출력
  심각도 점수(severity)로 정렬 — 전 공정이면 결과가 수백 건이라
  정렬이 없으면 결국 아무도 안 본다.
  연속 며칠째인지(streak) 표기 — 하루만이면 노이즈, 3일이면 진짜.
════════════════════════════════════════════════════════════
"""

import json
import re
from datetime import datetime

from django.db import connections

# ══ 판정 기준 — 조정은 여기서만 ═══════════════════════════
SIGMA_WARN   = 1.0     # 평균 이탈 주의
SIGMA_ALERT  = 2.0     # 평균 이탈 이상
OUT_WARN     = 1       # 30일 범위 밖 웨이퍼 주의
OUT_ALERT    = 3       # 이상
EQP_SIGMA    = 2.0     # 장비/챔버 단독 이탈 기준
SPREAD_WARN  = 1.5     # 표준편차가 기준의 몇 배면 주의
SPREAD_ALERT = 2.0     # 이상
DRIFT_DAYS   = 7       # 드리프트 판정 기간
DRIFT_SIGMA  = 1.0     # 기간 동안 σ 단위로 이만큼 이동하면 드리프트
MIN_N        = 5       # 최근일 웨이퍼가 이보다 적으면 신뢰도 낮음

# Defect 전용
DEF_MULT_WARN  = 2.0   # 최근 구간 평균이 30일 중앙값의 몇 배면 주의
DEF_MULT_ALERT = 3.0   # 이상
DEF_ZERO_BASE  = 0.5   # 기준 중앙값이 이 이하면 '평소 거의 없음' 으로 본다
DEF_WINDOW     = 7     # ★ defect 은 '최근일' 이 아니라 최근 N일로 묶는다
DEF_MIN_N      = 2     # defect 은 검사 장수가 원래 적다 (계측용 MIN_N 과 별도)

CONFIG_TABLE  = 'cmp_gooddocs_config'   # 배치가 저장하는 구닥스 스냅샷
RESULT_TABLE  = 'cmp_monitor_result'    # 최근 1회 점검 결과
HISTORY_TABLE = 'cmp_monitor_history'   # 연속일수 계산용 이력

# 챔버 짝 (analysis_service.CHAMBER_TWINS 와 같은 규칙.
#  analysis_service 는 사내 모듈 import 가 있어 웹에서 못 부르므로 복사)
CHAMBER_TWINS = [('PA', 'PC'), ('PB', 'PD'), ('PL', 'PR')]

# 이름 규칙 폴백 (구닥스에 PARAM_TYPE 이 아직 없을 때)
#   ※ DEFECT 규칙을 넓게 잡으면 PADCNT/DISKCNT 같은 소모품 카운터가
#     오탐된다. defect 임이 분명한 어휘만 쓰고, 나머지는 구닥스
#     PARAM_TYPE 으로 지정하게 둔다.
#   ※ REV(제거량 계열)도 THK 와 같은 범주로 본다 — 두께에서 파생된
#     계측값이라 판정 규칙(평균±σ)이 동일하다.
RE_THK    = re.compile(r'THK|OCD|REV')
RE_TIME   = re.compile(r'\d_?TIME|TIME_?\d')
RE_DEFECT = re.compile(r'DEFECT|SCRATCH|PARTICLE|RESIDUE|^DEF_|_DEF_|_DEF$')

NUMERIC_TYPES = {'smallint', 'integer', 'bigint', 'decimal',
                 'numeric', 'real', 'double precision'}


def _conn():
    return connections['analysis_db']


def _table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def _exists(cur, t):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", [t])
    return bool(cur.fetchone()[0])


def _cols(cur, t):
    cur.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = %s
    """, [t])
    return cur.fetchall()


def _f(v, nd=3):
    return round(float(v), nd) if v is not None else None


# ══════════════════════════════════════════════════════════
# 점검 대상 파라미터 + 타입
# ══════════════════════════════════════════════════════════
def _expand_twins(params):
    """PA_03_TIME 이 대상이면 PC_03_TIME 도 대상 (양방향)"""
    out, have = list(params), set(params)
    for p in params:
        for a, b in CHAMBER_TWINS:
            for left, right in ((a, b), (b, a)):
                m = re.match(rf'^{left}(\d*)_(.+)$', p, re.IGNORECASE)
                if m:
                    twin = f'{right}{m.group(1)}_{m.group(2)}'.upper()
                    if twin not in have:
                        out.append(twin)
                        have.add(twin)
    return out


def _type_from_name(param):
    """이름 규칙 폴백 — DEFECT 를 먼저 본다 (DEF_THK 같은 이름 대비)"""
    p = param.upper()
    if RE_DEFECT.search(p):
        return 'DEFECT'
    if RE_THK.search(p):
        return 'THK'
    if RE_TIME.search(p):
        return 'TIME'
    return 'ETC'


def _config_map(cur, oper_id):
    """
    구닥스 스냅샷에서 {PARAM: PARAM_TYPE}.

    ★ 구닥스 기준정보에 등록된 파라미터는 전부 점검 대상이다.
      구닥스에는 점검할 항목만 넣고 관리하므로 별도 Y/N 표시가 필요 없다.
      (점검 시간도 짧아 전수 점검이 문제되지 않는다)

      INLINE_YN 컬럼이 있으면 '명시적으로 N' 인 것만 제외한다 —
      나중에 일부만 빼고 싶을 때를 위한 선택적 장치다.
      값이 비어 있으면 포함한다.

    스냅샷이나 PARAM/OPER_ID 컬럼이 없으면 None (→ 이름 규칙 폴백)
    """
    if not _exists(cur, CONFIG_TABLE):
        return None
    cols = {c.upper() for c, _ in _cols(cur, CONFIG_TABLE)}
    if 'PARAM' not in cols or 'OPER_ID' not in cols:
        return None

    type_sel = '"PARAM_TYPE"' if 'PARAM_TYPE' in cols else "''"
    # 명시적 제외만 걸러낸다 (빈 값 = 포함)
    excl = ("""AND upper(COALESCE("INLINE_YN", '')) """
            """NOT IN ('N','NO','0','X','FALSE')"""
            if 'INLINE_YN' in cols else '')
    cur.execute(f'''
        SELECT DISTINCT upper("PARAM"), upper(COALESCE({type_sel}, ''))
        FROM {CONFIG_TABLE}
        WHERE upper("OPER_ID") = %s
          AND COALESCE("PARAM", '') <> ''
          {excl}
    ''', [str(oper_id).upper()])
    got = {r[0]: (r[1] or '') for r in cur.fetchall() if r[0]}
    return got or None


def monitored_params(cur, table, oper_id):
    """
    점검할 (파라미터, 타입) 목록과 출처.
    반환: ([(param, type), ...], '구닥스' | '기본규칙')
    """
    numeric = {c.upper() for c, d in _cols(cur, table)
               if d.lower() in NUMERIC_TYPES}

    conf = _config_map(cur, oper_id)
    if conf:
        # 챔버 짝 확장 — 짝은 원본의 타입을 물려받는다
        expanded = {}
        for p in _expand_twins(list(conf.keys())):
            t = conf.get(p) or ''
            if not t:
                for src, st in conf.items():
                    if src[1:] == p[1:] or src[2:] == p[2:]:
                        t = st
                        break
            expanded[p] = t or _type_from_name(p)
        out = [(p, t) for p, t in expanded.items() if p in numeric]
        if out:
            return sorted(out), '구닥스'

    # 폴백 — 두께 / polishing time / defect 계열만
    out = []
    for c in sorted(numeric):
        t = _type_from_name(c)
        if t in ('THK', 'TIME', 'DEFECT'):
            out.append((c, t))
    return out, '기본규칙'


def explain_params(oper_id):
    """
    이 공정에서 무엇이 점검 대상으로 잡히고 무엇이 왜 빠졌는지 출력한다.

      python manage.py shell
      >>> from equipment import monitor_service as ms
      >>> ms.explain_params('공정ID')

    ★ 가장 흔한 오해
      구닥스 스냅샷(cmp_gooddocs_config)이 있으면 그 공정에 등록된
      파라미터가 전부 점검 대상이 되고, 이름 규칙(RE_THK 등)은 아예
      타지 않는다. 그래서 대상에 넣거나 빼려면 구닥스 기준정보를 고치고
      배치를 다시 돌려 스냅샷을 갱신해야 한다.
    """
    table = _table(oper_id)
    with _conn().cursor() as cur:
        if not _exists(cur, table):
            print(f'{table} 없음 — 적재되지 않은 공정입니다.')
            return

        cols    = _cols(cur, table)
        numeric = {c.upper() for c, d in cols if d.lower() in NUMERIC_TYPES}
        nonnum  = {c.upper(): d for c, d in cols
                   if d.lower() not in NUMERIC_TYPES}

        conf = _config_map(cur, oper_id)
        params, source = monitored_params(cur, table, oper_id)
        chosen = {p for p, _ in params}

        print(f'[{oper_id}] 대상 선정 방식: {source}')
        print(f'  테이블 숫자 컬럼 {len(numeric)}개 / 점검 대상 {len(chosen)}개')

        if source == '구닥스':
            print('\n  ※ 구닥스 스냅샷에 등록된 파라미터가 전부 대상이며,')
            print('    이름 규칙(폴백)은 사용되지 않습니다. 대상을 바꾸려면')
            print('    구닥스 기준정보를 고치고 배치를 다시 돌려 스냅샷을')
            print('    갱신해야 합니다.')
            print(f'\n  구닥스 등록 {len(conf)}개:')
            for p in sorted(conf):
                state = 'OK' if p in numeric else '← 테이블에 없거나 숫자 아님'
                print(f'    {p:<34}{(conf[p] or "(타입미지정)"):<14}{state}')

        print(f'\n  점검 대상 {len(params)}개:')
        for p, t in params:
            print(f'    {p:<34}{t}')

        skipped = sorted(numeric - chosen)
        if skipped:
            print(f'\n  제외된 숫자 컬럼 {len(skipped)}개:')
            for p in skipped:
                t = _type_from_name(p)
                why = ('구닥스 미등록' if source == '구닥스'
                       else f'이름 규칙상 {t} → 대상 아님')
                print(f'    {p:<34}{t:<10}{why}')

        # 계측값처럼 보이는데 타입이 어긋나 후보에서 빠진 컬럼
        suspicious = sorted(c for c in nonnum
                            if re.search(r'THK|OCD|REV|TIME|DEF', c))
        if suspicious:
            print(f'\n  ★ 계측값처럼 보이는데 숫자 타입이 아닌 컬럼 '
                  f'{len(suspicious)}개:')
            for c in suspicious:
                print(f'    {c:<34}{nonnum[c]}')
            print('    → analysis_service.repair_numeric_columns() 로 복구 가능')

    return {'source': source, 'chosen': sorted(chosen),
            'config': sorted(conf) if conf else []}


# ══════════════════════════════════════════════════════════
# 공통 조회
# ══════════════════════════════════════════════════════════
def _series(cur, table, lot_cd, param):
    """일별 평균/건수 — 미니차트와 드리프트 판정에 함께 쓴다"""
    cur.execute(f'''
        SELECT "DATE"::date AS d, AVG("{param}"), COUNT(*)
        FROM {table}
        WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL
        GROUP BY d ORDER BY d
    ''', [lot_cd])
    return [{'d': str(r[0]), 'avg': _f(r[1]), 'n': r[2]} for r in cur.fetchall()]


def _drift(series, base_std):
    """
    최근 DRIFT_DAYS 일별 평균의 기울기.
    기간 전체 이동량이 DRIFT_SIGMA σ 이상이면 드리프트로 본다.
    """
    pts = [s for s in series if s['avg'] is not None][-DRIFT_DAYS:]
    if len(pts) < 4 or not base_std or float(base_std) <= 0:
        return None
    n = len(pts)
    xs = list(range(n))
    ys = [s['avg'] for s in pts]
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    total = slope * (n - 1)                      # 기간 전체 이동량
    return {'slope': round(slope, 4), 'days': n,
            'total_sigma': round(total / float(base_std), 2)}


# ══════════════════════════════════════════════════════════
# 파라미터 1건 점검
# ══════════════════════════════════════════════════════════
def _check_param(cur, table, lot_cd, param, ptype, has_eqp, has_ch):
    cur.execute(f'''
        SELECT MAX("DATE"::date) FROM {table}
        WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL
    ''', [lot_cd])
    day = cur.fetchone()[0]
    if day is None:
        return None

    # ── 판정 구간 ────────────────────────────────────────
    # 계측값은 '최근일' 하루.
    # ★ defect 은 전수가 아니라 샘플링이라 하루만 보면 검사가 아예 없거나
    #   한두 장으로 판정하게 된다. 최근 DEF_WINDOW 일을 한 구간으로 묶는다.
    is_def = (ptype == 'DEFECT')
    if is_def:
        cur.execute(f"SELECT %s::date - {int(DEF_WINDOW) - 1}", [day])
        d_from = cur.fetchone()[0]
        span_label = f'최근 {DEF_WINDOW}일'
    else:
        d_from = day
        span_label = '최근일'
    min_n = DEF_MIN_N if is_def else MIN_N

    # 기준선 = 판정 구간 이전 전체
    cur.execute(f'''
        SELECT COUNT(*), AVG("{param}"), STDDEV("{param}"),
               MIN("{param}"), MAX("{param}"),
               PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY "{param}"),
               PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY "{param}")
        FROM {table}
        WHERE "LOT_CD" = %s AND "DATE"::date < %s AND "{param}" IS NOT NULL
    ''', [lot_cd, d_from])
    b_n, b_avg, b_std, b_min, b_max, b_med, b_p95 = cur.fetchone()

    # 판정 구간
    cur.execute(f'''
        SELECT COUNT(*), AVG("{param}"), STDDEV("{param}"),
               MIN("{param}"), MAX("{param}")
        FROM {table}
        WHERE "LOT_CD" = %s AND "DATE"::date BETWEEN %s AND %s
          AND "{param}" IS NOT NULL
    ''', [lot_cd, d_from, day])
    d_n, d_avg, d_std, d_min, d_max = cur.fetchone()
    if not d_n:
        return None

    r = {
        'oper_id': None, 'lot_cd': lot_cd, 'param': param, 'ptype': ptype,
        'day': str(day), 'day_from': str(d_from), 'span': span_label,
        'day_stat':  {'n': d_n, 'avg': _f(d_avg), 'std': _f(d_std),
                      'min': _f(d_min), 'max': _f(d_max)},
        'base_stat': {'n': b_n or 0, 'avg': _f(b_avg), 'std': _f(b_std),
                      'min': _f(b_min), 'max': _f(b_max),
                      'med': _f(b_med), 'p95': _f(b_p95)},
        'low_n': d_n < min_n,
        'sigma': None, 'out_cnt': 0, 'spread': None, 'drift': None,
        'eqp': [], 'checks': [], 'reasons': [], 'severity': 0,
    }
    r['series'] = _series(cur, table, lot_cd, param)

    if not b_n or b_avg is None:
        r['status'] = '기준없음'
        r['reasons'] = ['비교할 이전 데이터가 없음 (신규 파라미터/공정)']
        return r

    level = 0          # 0 정상 / 1 주의 / 2 이상
    reasons, checks = [], []

    # ── Defect 은 별도 규칙 ───────────────────────────────
    if ptype == 'DEFECT':
        med = float(b_med or 0)
        avg = float(d_avg)
        if med <= DEF_ZERO_BASE:
            # 평소 거의 안 나오던 항목 — 검출 자체가 신호
            if avg > 0:
                level = 2 if (d_max or 0) > (b_p95 or 0) else 1
                reasons.append(
                    f'평소 거의 검출되지 않던 항목에서 검출 '
                    f'({span_label} 평균 {_f(avg)}, 30일 중앙값 {_f(med)})')
                checks.append('D-검출전환')
        else:
            mult = avg / med if med else None
            if mult is not None:
                r['sigma'] = round(mult, 2)      # 화면에는 '배수' 로 표기
                if mult >= DEF_MULT_ALERT:
                    level = 2
                    reasons.append(f'{span_label} 평균이 30일 중앙값의 {mult:.1f}배 '
                                   f'({_f(avg)} vs {_f(med)})')
                    checks.append('D-급증')
                elif mult >= DEF_MULT_WARN:
                    level = max(level, 1)
                    reasons.append(f'{span_label} 평균이 30일 중앙값의 {mult:.1f}배')
                    checks.append('D-증가')
        # 상위 분위수 초과 웨이퍼
        if b_p95 is not None:
            cur.execute(f'''
                SELECT COUNT(*) FROM {table}
                WHERE "LOT_CD" = %s AND "DATE"::date BETWEEN %s AND %s
                  AND "{param}" > %s
            ''', [lot_cd, d_from, day, b_p95])
            over = cur.fetchone()[0]
            r['out_cnt'] = over
            if over >= OUT_ALERT:
                level = 2
                reasons.append(f'30일 상위 5%({_f(b_p95)}) 초과 웨이퍼 {over}장 '
                               f'({span_label})')
                checks.append('D-상위초과')
            elif over >= OUT_WARN:
                level = max(level, 1)
                reasons.append(f'30일 상위 5% 초과 웨이퍼 {over}장')
                checks.append('D-상위초과')

    # ── 계측값(THK/TIME/ETC) 규칙 ─────────────────────────
    else:
        # L 수준이탈
        if b_std and float(b_std) > 0:
            sigma = (float(d_avg) - float(b_avg)) / float(b_std)
            r['sigma'] = round(sigma, 2)
            if abs(sigma) >= SIGMA_ALERT:
                level = 2
                reasons.append(f'최근일 평균이 30일 기준 대비 {sigma:+.1f}σ '
                               f'({_f(d_avg)} vs {_f(b_avg)})')
                checks.append('L-수준이탈')
            elif abs(sigma) >= SIGMA_WARN:
                level = max(level, 1)
                reasons.append(f'최근일 평균이 30일 기준 대비 {sigma:+.1f}σ')
                checks.append('L-수준이탈')

        # R 범위이탈
        cur.execute(f'''
            SELECT COUNT(*) FROM {table}
            WHERE "LOT_CD" = %s AND "DATE"::date BETWEEN %s AND %s
              AND "{param}" IS NOT NULL
              AND ("{param}" < %s OR "{param}" > %s)
        ''', [lot_cd, d_from, day, b_min, b_max])
        out_cnt = cur.fetchone()[0]
        r['out_cnt'] = out_cnt
        if out_cnt >= OUT_ALERT:
            level = 2
            reasons.append(f'30일 범위({_f(b_min)}~{_f(b_max)}) 밖 웨이퍼 {out_cnt}장')
            checks.append('R-범위이탈')
        elif out_cnt >= OUT_WARN:
            level = max(level, 1)
            reasons.append(f'30일 범위 밖 웨이퍼 {out_cnt}장')
            checks.append('R-범위이탈')

        # S 산포확대 — 평균이 멀쩡해도 잡아야 한다
        if b_std and float(b_std) > 0 and d_std and d_n >= MIN_N:
            ratio = float(d_std) / float(b_std)
            r['spread'] = round(ratio, 2)
            if ratio >= SPREAD_ALERT:
                level = 2
                reasons.append(f'최근일 산포가 기준의 {ratio:.1f}배 '
                               f'(σ {_f(d_std)} vs {_f(b_std)}) — 조건 혼입 의심')
                checks.append('S-산포확대')
            elif ratio >= SPREAD_WARN:
                level = max(level, 1)
                reasons.append(f'최근일 산포가 기준의 {ratio:.1f}배')
                checks.append('S-산포확대')

        # D 드리프트 — 아직 안 벗어났어도 예고
        dr = _drift(r['series'], b_std)
        if dr:
            r['drift'] = dr
            if abs(dr['total_sigma']) >= DRIFT_SIGMA:
                level = max(level, 1)
                reasons.append(f"최근 {dr['days']}일간 {dr['total_sigma']:+.1f}σ "
                               f"방향성 있게 이동 중")
                checks.append('D-드리프트')

    # ── E 단독이탈 (타입 공통) ────────────────────────────
    key_col = 'EQP_CH_ID' if has_ch else ('EQP_ID' if has_eqp else None)
    if key_col:
        cur.execute(f'''
            SELECT "{key_col}", COUNT(*), AVG("{param}")
            FROM {table}
            WHERE "LOT_CD" = %s AND "DATE"::date BETWEEN %s AND %s
              AND "{param}" IS NOT NULL
              AND COALESCE("{key_col}", '') <> ''
            GROUP BY "{key_col}" ORDER BY 1
        ''', [lot_cd, d_from, day])
        for eqp, n, avg in cur.fetchall():
            es = None
            if b_std and float(b_std) > 0:
                es = round((float(avg) - float(b_avg)) / float(b_std), 2)
            r['eqp'].append({'eqp': eqp, 'n': n, 'avg': _f(avg), 'sigma': es})

        if len(r['eqp']) >= 2:
            hot  = [e for e in r['eqp']
                    if e['sigma'] is not None and abs(e['sigma']) >= EQP_SIGMA]
            calm = [e for e in r['eqp']
                    if e['sigma'] is not None and abs(e['sigma']) < SIGMA_WARN]
            if len(hot) == 1 and calm:
                level = 2
                reasons.append(f"{hot[0]['eqp']} 단독 이탈 "
                               f"({hot[0]['sigma']:+.1f}σ, {hot[0]['n']}장) "
                               f"— 다른 설비는 기준 근처")
                checks.append('E-단독이탈')

    if r['low_n']:
        reasons.append(f'{span_label} 웨이퍼 {d_n}장 — 표본이 적어 신뢰도 낮음')

    r['status']  = ['정상', '주의', '이상'][level]
    r['checks']  = checks
    r['reasons'] = reasons or ['30일 기준 범위 내']

    # ── 심각도 점수 — 목록 정렬용 ─────────────────────────
    sev = level * 100
    if r['sigma'] is not None:
        sev += min(abs(r['sigma']), 10) * 5
    sev += min(r['out_cnt'], 25) * 2
    if r['spread']:
        sev += max(0, (r['spread'] - 1)) * 10
    if 'E-단독이탈' in checks:
        sev += 30
    if ptype == 'DEFECT':
        sev += 10                      # 같은 등급이면 defect 을 위로
    if r['low_n']:
        sev *= 0.6                     # 표본 적으면 신뢰도만큼 낮춘다
    r['severity'] = round(sev, 1)
    return r


# ══════════════════════════════════════════════════════════
# 공정 단위 점검
# ══════════════════════════════════════════════════════════
def run_check(oper_id, oper_label=''):
    """한 공정 전체 점검 → 결과 저장 후 반환"""
    table = _table(oper_id)
    out = {'oper_id': oper_id, 'oper_label': oper_label or oper_id,
           'source': '', 'results': [], 'note': ''}

    with _conn().cursor() as cur:
        if not _exists(cur, table):
            out['note'] = '적재된 데이터 없음'
            return out

        cols_up = {c.upper() for c, _ in _cols(cur, table)}
        if 'LOT_CD' not in cols_up or 'DATE' not in cols_up:
            out['note'] = 'LOT_CD/DATE 컬럼 없음'
            return out

        has_eqp = 'EQP_ID' in cols_up
        has_ch  = 'EQP_CH_ID' in cols_up

        params, source = monitored_params(cur, table, oper_id)
        out['source'] = source
        if not params:
            out['note'] = ('점검 대상 파라미터 없음 — '
                           'explain_params(oper_id) 로 원인 확인')
            return out

        cur.execute(f'SELECT DISTINCT "LOT_CD" FROM {table} '
                    f'WHERE "LOT_CD" IS NOT NULL ORDER BY 1')
        lot_cds = [r[0] for r in cur.fetchall()]

        for lot_cd in lot_cds:
            for param, ptype in params:
                try:
                    res = _check_param(cur, table, lot_cd, param, ptype,
                                       has_eqp, has_ch)
                except Exception as e:
                    print(f'[monitor] {oper_id}/{lot_cd}/{param} 실패: {e}')
                    continue
                if res:
                    res['oper_id']    = oper_id
                    res['oper_label'] = out['oper_label']
                    out['results'].append(res)

    _save(oper_id, out['results'])
    _attach_streak(out['results'])
    return out


# ══════════════════════════════════════════════════════════
# 저장 / 이력 / 조회
# ══════════════════════════════════════════════════════════
def _ensure_tables(cur):
    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS {RESULT_TABLE} (
          id BIGSERIAL PRIMARY KEY,
          run_ts TIMESTAMP, oper_id VARCHAR(100), lot_cd VARCHAR(50),
          param VARCHAR(200), ptype VARCHAR(20), status VARCHAR(20),
          severity DOUBLE PRECISION, payload TEXT
        )
    ''')
    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
          id BIGSERIAL PRIMARY KEY,
          day DATE, oper_id VARCHAR(100), lot_cd VARCHAR(50),
          param VARCHAR(200), status VARCHAR(20)
        )
    ''')
    cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{HISTORY_TABLE}_key '
                f'ON {HISTORY_TABLE} (oper_id, lot_cd, param, day)')


def _save(oper_id, results):
    with _conn().cursor() as cur:
        _ensure_tables(cur)
        cur.execute(f'DELETE FROM {RESULT_TABLE} WHERE oper_id = %s', [oper_id])
        ts = datetime.now()
        for r in results:
            cur.execute(f'''
                INSERT INTO {RESULT_TABLE}
                  (run_ts, oper_id, lot_cd, param, ptype, status, severity, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ''', [ts, oper_id, r['lot_cd'], r['param'], r.get('ptype', ''),
                  r['status'], r.get('severity', 0),
                  json.dumps(r, ensure_ascii=False, default=str)])

            # 이력은 (데이터 일자, 키) 단위로 1건만 — 같은 날 재점검해도 중복 없음
            cur.execute(f'''
                DELETE FROM {HISTORY_TABLE}
                WHERE day = %s AND oper_id = %s AND lot_cd = %s AND param = %s
            ''', [r['day'], oper_id, r['lot_cd'], r['param']])
            cur.execute(f'''
                INSERT INTO {HISTORY_TABLE} (day, oper_id, lot_cd, param, status)
                VALUES (%s,%s,%s,%s,%s)
            ''', [r['day'], oper_id, r['lot_cd'], r['param'], r['status']])


def _attach_streak(results):
    """
    이상/주의가 며칠 연속인지. 하루만이면 노이즈, 3일이면 실제 변화.
    최근 데이터 일자부터 거꾸로 세되 정상이 나오면 멈춘다.
    """
    if not results:
        return
    with _conn().cursor() as cur:
        _ensure_tables(cur)
        for r in results:
            if r['status'] not in ('이상', '주의'):
                r['streak'] = 0
                continue
            cur.execute(f'''
                SELECT day, status FROM {HISTORY_TABLE}
                WHERE oper_id=%s AND lot_cd=%s AND param=%s
                ORDER BY day DESC LIMIT 30
            ''', [r['oper_id'], r['lot_cd'], r['param']])
            streak = 0
            for _day, st in cur.fetchall():
                if st in ('이상', '주의'):
                    streak += 1
                else:
                    break
            r['streak'] = streak


def load_results():
    """저장된 최근 점검 결과 전체"""
    with _conn().cursor() as cur:
        if not _exists(cur, RESULT_TABLE):
            return {'run_ts': None, 'results': []}
        cur.execute(f'SELECT MAX(run_ts) FROM {RESULT_TABLE}')
        ts = cur.fetchone()[0]
        cur.execute(f'SELECT payload FROM {RESULT_TABLE} '
                    f'ORDER BY severity DESC, oper_id, lot_cd, param')
        out = []
        for (p,) in cur.fetchall():
            try:
                out.append(json.loads(p))
            except Exception:
                pass
    _attach_streak(out)
    return {'run_ts': str(ts) if ts else None, 'results': out}


def wafer_detail(oper_id, lot_cd, param, days=30):
    """행을 펼쳤을 때 보여줄 웨이퍼 단위 데이터"""
    table = _table(oper_id)
    with _conn().cursor() as cur:
        if not _exists(cur, table):
            return {'points': []}
        cols_up = {c.upper() for c, _ in _cols(cur, table)}
        if param.upper() not in cols_up:
            return {'points': []}

        extra = [c for c in ('EQP_ID', 'EQP_CH_ID', 'LOT_ID', 'WF_ID', 'IDLE')
                 if c in cols_up]
        sel = "".join(f', "{c}"' for c in extra)
        cur.execute(f'''
            SELECT id, "DATE", "{param}"{sel}
            FROM {table}
            WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL
            ORDER BY "DATE"
        ''', [lot_cd])
        rows = cur.fetchall()

    pts = []
    for r in rows:
        item = {'id': r[0],
                'date': r[1].strftime('%Y-%m-%d %H:%M:%S') if r[1] else None,
                'val': float(r[2]) if r[2] is not None else None}
        for i, c in enumerate(extra):
            item[c] = r[3 + i]
        pts.append(item)
    return {'points': pts, 'extra': extra}
