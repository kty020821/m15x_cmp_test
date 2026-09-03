"""
equipment/monitor_service.py
════════════════════════════════════════════════════════════
Inline Monitoring — 전 공정 자동 점검

  적재된 cmp_analysis_* (PostgreSQL) 만 읽는다.
  Lake/구닥스에 접근하지 않으므로 웹 프로세스에서 안전하게 돈다.
  기준정보는 셋업 페이지가 관리하는 cmp_cfg_param 을 읽는다
  (구닥스가 불안정해 자체 DB 로 옮김. 예전 스냅샷도 호환으로 읽는다).

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
  구분은 기준정보의 PARAM_TYPE (없으면 이름 규칙 폴백).

점검 대상 파라미터
  1순위  기준정보(cmp_cfg_param)에 등록된 그 공정의 PARAM 전부
         + 챔버 짝 자동 확장 (PA↔PC, PB↔PD, PL↔PR)
         기준정보에는 점검할 항목만 넣고 관리하므로 Y/N 표시가 필요 없다.
         use_yn 이 'N' 인 것만 제외한다.
  폴백   스냅샷이 없으면 계측값 숫자 컬럼 전부
         (메타·파생 _OFFSET/_FORMULA 제외, MONITOR_ALL_NUMERIC=False 면
          THK/TIME/DEFECT 로 분류된 것만)

  어느 쪽이 적용됐고 무엇이 왜 빠졌는지는 explain_params(oper_id) 로 확인.

출력
  심각도 점수(severity)로 정렬 — 전 공정이면 결과가 수백 건이라
  정렬이 없으면 결국 아무도 안 본다.
  연속 며칠째인지(streak) 표기 — 하루만이면 노이즈, 3일이면 진짜.
════════════════════════════════════════════════════════════
"""

import json
import re
from datetime import datetime, timedelta

from django.db import connections

from . import param_types as pt

# ══ 판정 기준 — 조정은 여기서만 ═══════════════════════════
# ── 판정 임계값 ──────────────────────────────────────────
#   ★ 정규분포에서 |z| >= k 인 비율:
#       1.0σ → 32%   1.5σ → 13%   2.0σ → 4.6%   3.0σ → 0.3%
#     파라미터 300개 × device 2 = 600건이면 1.0σ 기준으로 190건이
#     '정상인데도' 걸린다. 그래서 전부 이상해 보였다.
#   ★ 실제로 볼 것만 남기려면 2σ 이상이어야 한다.
SIGMA_WARN   = 2.0     # 평균 이탈 주의 (600건 중 우연히 ~27건)
SIGMA_ALERT  = 3.0     # 평균 이탈 이상 (우연히 ~2건)
OUT_WARN     = 3       # 30일 범위 밖 웨이퍼 주의 (1장은 흔하다)
OUT_ALERT    = 5       # 이상
EQP_SIGMA    = 2.5     # 장비/챔버 단독 이탈 기준
SPREAD_WARN  = 2.0     # 표준편차가 기준의 몇 배면 주의
SPREAD_ALERT = 3.0     # 이상
DRIFT_DAYS   = 7       # 드리프트 판정 기간
DRIFT_SIGMA  = 1.5     # 기간 동안 σ 단위로 이만큼 이동하면 드리프트
MIN_N        = 5       # 최근일 웨이퍼가 이보다 적으면 신뢰도 낮음

# ★ 근거가 하나뿐이면 '주의' 로만 둔다.
#   평균 이탈·산포 확대·범위 밖이 겹칠 때가 실제 문제인 경우가 많다.
#   하나만 걸린 것을 '이상' 으로 올리면 목록이 이상으로 가득 찬다.
NEED_CHECKS_FOR_ALERT = 2

# ★ Defect 은 '개수가 늘었다' 하나로 판단하는 게 정상이다.
#   산포·범위 같은 다른 근거가 붙을 일이 없어, 근거 2가지 규칙을
#   그대로 적용하면 아무리 늘어도 '주의' 에서 멈춘다.
DEF_NEED_CHECKS = 1

# ★ 상위 5% 초과 웨이퍼 기준도 Defect 은 따로 둔다.
#   계측값은 하루 25장을 재지만 Defect 은 두세 장이라,
#   같은 '5장' 기준을 쓰면 아무리 나빠도 걸리지 않는다.
DEF_OUT_WARN  = 1      # 상위 5% 초과 웨이퍼 주의
DEF_OUT_ALERT = 2      # 이상

# ── 주기적 반복 ──────────────────────────────────────────
#   ★ 매주 같은 요일에 오르내리는 것은 그 공정의 성질이지 이상이 아니다.
#     매번 알림이 뜨면 진짜 이상이 묻힌다.
PERIODIC_MIN_DAYS    = 14    # 이보다 짧으면 주기를 논할 수 없다
PERIODIC_MIN_SAMPLES = 3     # 같은 요일이 최소 몇 번은 있어야 한다
PERIODIC_MIN_SIGMA   = 0.8   # 그 요일이 이만큼 벗어나 있어야 '패턴'
PERIODIC_EXPLAIN     = 0.6   # 지금 이탈의 이만큼이 그 패턴으로 설명되면 제외

# 소모품(PART) 판정 여부
#   Pad/Head/Disk 사용량은 누적되며 단조 증가하고 PM 에서 리셋된다.
#   '30일 평균 대비 σ' 로 보면 매일 이탈로 잡혀 판정이 무의미하므로
#   기본은 참고 표시만 한다. 필요하면 True 로 켠다.
JUDGE_PART   = False

# Defect 전용
DEF_MULT_WARN  = 2.0   # 최근 구간 평균이 30일 중앙값의 몇 배면 주의
DEF_MULT_ALERT = 3.0   # 이상
DEF_ZERO_BASE  = 0.5   # 기준 중앙값이 이 이하면 '평소 거의 없음' 으로 본다
DEF_WINDOW     = 1     # defect 점검 구간 (일). 1 이면 다른 타입과 같이 최근 하루.
                       #   ★ 여러 날을 묶으면 표본은 늘지만, 어느 날 일이
                       #     생겼는지가 흐려진다. 파장이 궁금하면 값을 올린다.
DEF_MIN_N      = 2     # defect 은 검사 장수가 원래 적다 (계측용 MIN_N 과 별도)

CONFIG_TABLE  = 'cmp_gooddocs_config'   # 예전 구닥스 스냅샷 (이관 전 호환)
RESULT_TABLE  = 'cmp_monitor_result'    # 최근 1회 점검 결과
HISTORY_TABLE = 'cmp_monitor_history'   # 연속일수 계산용 이력

# 챔버 짝 (analysis_service.CHAMBER_TWINS 와 같은 규칙.
#  analysis_service 는 사내 모듈 import 가 있어 웹에서 못 부르므로 복사)
CHAMBER_TWINS = [('PA', 'PC'), ('PB', 'PD'), ('PL', 'PR')]

# ★ 타입 분류 규칙은 param_types.py 가 단일 소재지다.
#   예전에는 여기에 정규식(RE_THK/RE_TIME/RE_DEFECT)을 따로 두어
#   기준정보 셋업 쪽과 어긋났다. 규칙 수정은 param_types.py 에서만.

# ── 폴백 대상 범위 ────────────────────────────────────────
#   True  : 계측값으로 보이는 숫자 컬럼 전부 (메타·파생 제외)
#   False : THK/TIME/DEFECT 로 분류된 것만 (좁은 예전 동작)
#
#   기준정보가 있으면 이 설정과 무관하게 기준정보 목록이 우선한다.
#   기준정보가 없을 때 "이름에 THK/TIME 이 든 것만 점검된다" 는 문제를
#   막기 위한 것이다.
MONITOR_ALL_NUMERIC = True

# 계측값이 아닌 식별/관리 컬럼 (views_analysis.META_COLS 와 같은 기준)
META_COLS = {
    'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
    'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
    'WF_ID', 'IDLE', 'REWORK_N', 'MEAS_N', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY',
}

# 파생/보조 컬럼 — 점검해도 의미가 약하고 결과만 부풀린다
#   _OFFSET  : APC 보정량 (원인 분석용 참고값)
#   _FORMULA : 적용 수식 (문자열이라 보통 숫자도 아니다)
RE_DERIVED = re.compile(r'_OFFSET$|_FORMULA$')

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
    """이름으로 타입 판정 — 규칙은 param_types.classify 에 있다"""
    return pt.classify(param)


def _config_map(cur, oper_id):
    """
    기준정보에서 {PARAM: PARAM_TYPE}.

    ★ 1순위는 셋업 페이지가 관리하는 cmp_cfg_param 이다.
      구닥스가 불안정해 기준정보를 자체 DB 로 옮겼고, 그쪽이 원본이다.
      2순위는 예전 구닥스 스냅샷(cmp_gooddocs_config) — 이관 전 호환용.

    ★ 등록된 파라미터는 전부 점검 대상이다.
      기준정보에는 점검할 항목만 넣고 관리하므로 별도 Y/N 표시가 없어도 된다.
      use_yn / INLINE_YN 이 있으면 '명시적으로 N' 인 것만 제외한다.

    어느 쪽에서도 못 찾으면 None (→ 이름 규칙 폴백)
    """
    # ── 1순위: 셋업 페이지 기준정보 ──────────────────────
    if _exists(cur, 'cmp_cfg_param'):
        cur.execute('''
            SELECT DISTINCT upper(param), upper(COALESCE(param_type, ''))
            FROM cmp_cfg_param
            WHERE upper(oper_id) = %s
              AND COALESCE(param, '') <> ''
              AND upper(COALESCE(use_yn, 'Y')) <> 'N'
        ''', [str(oper_id).upper()])
        got = {r[0]: (r[1] or '') for r in cur.fetchall() if r[0]}
        if got:
            return got

    # ── 2순위: 구닥스 스냅샷 (이관 전 호환) ───────────────
    if not _exists(cur, CONFIG_TABLE):
        return None
    cols = {c.upper() for c, _ in _cols(cur, CONFIG_TABLE)}
    if 'PARAM' not in cols or 'OPER_ID' not in cols:
        return None

    type_sel = '"PARAM_TYPE"' if 'PARAM_TYPE' in cols else "''"
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


def _step_columns(oper_id):
    """
    기준정보에 등록된 Response·Defect 로 만들어질 컬럼 이름.

    ★ 컬럼 이름 규칙은 config_service.step_column 한 곳에만 있다 —
      여기서 따로 만들면 적재 결과와 점검 대상이 갈린다.
    """
    out = []
    try:
        from . import config_service as cfg
        for kind, builder in (('resp', cfg.build_response_config_df),
                              ('def',  cfg.build_defect_config_df)):
            df = builder()
            if df is None or df.empty:
                continue
            sub = df[df['OPER_ID'] == str(oper_id).upper()]
            for _, r in sub.iterrows():
                col = str(r.get('COLUMN') or '').strip()
                if col and col not in out:
                    out.append(col)
    except Exception as e:
        print(f'[monitor] 계측 컬럼 목록 조회 생략: '
              f'{e.__class__.__name__}: {e}')
    return out


def monitored_params(cur, table, oper_id):
    """
    점검할 (파라미터, 타입) 목록과 출처.
    반환: ([(param, type), ...], '기준정보' | '기본규칙')

    선정 순서
      1) 기준정보에 그 공정 PARAM 이 있으면 그것 전부 (+ 챔버 짝 확장)
      2) 없으면 폴백 — MONITOR_ALL_NUMERIC 에 따라
         계측값 숫자 컬럼 전부 / THK·TIME·DEFECT 만

    어느 쪽이 적용됐는지는 화면 '판정 방식' 과 explain_params() 로 확인.
    """
    numeric = {c.upper() for c, d in _cols(cur, table)
               if d.lower() in NUMERIC_TYPES}

    conf = _config_map(cur, oper_id)

    # ── Inline 계측(Response·Defect) 컬럼 합치기 ──────────
    #   ★ 이 컬럼들은 cmp_cfg_param 이 아니라 cmp_cfg_response /
    #     cmp_cfg_defect 에 등록된다. 그대로 두면 기준정보 경로에서
    #     통째로 빠져 점검이 안 된다.
    #   ★ 등록된 스텝·파라미터로 만들어질 컬럼 이름을 그대로 계산해
    #     실제 테이블에 있는 것만 대상에 넣는다.
    step_cols = _step_columns(oper_id)
    if step_cols:
        conf = dict(conf or {})
        for col in step_cols:
            conf.setdefault(col, '')      # 타입은 이름 규칙으로 자동 판정

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
            expanded[p] = pt.resolve(p, t)
        out = [(p, t) for p, t in expanded.items() if p in numeric]
        if out:
            return sorted(out), '기준정보'

    # ── 폴백 ─────────────────────────────────────────────
    #   스냅샷이 없을 때. 메타/파생 컬럼만 걷어내고 계측값은 전부 본다.
    #   (예전에는 THK·TIME·DEFECT 로 분류된 것만 점검해서, 이름에 그
    #    단어가 없는 계측 파라미터가 조용히 빠졌다)
    out = []
    for c in sorted(numeric):
        if c in META_COLS or RE_DERIVED.search(c):
            continue
        t = _type_from_name(c)
        if MONITOR_ALL_NUMERIC or t in ('THK', 'TIME', 'PRESSURE', 'DEFECT'):
            out.append((c, t))
    return out, '기본규칙'


def explain_params(oper_id):
    """
    이 공정에서 무엇이 점검 대상으로 잡히고 무엇이 왜 빠졌는지 출력한다.

      python manage.py shell
      >>> from equipment import monitor_service as ms
      >>> ms.explain_params('공정ID')

    ★ 가장 흔한 오해
      기준정보(cmp_cfg_param)에 그 공정이 등록돼 있으면 거기 파라미터가
      전부 점검 대상이 되고, 이름 규칙(RE_THK 등)은 아예 타지 않는다.
      대상을 바꾸려면 셋업 페이지에서 기준정보를 고치면 즉시 반영된다.
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

        if source == '기본규칙':
            snap = _exists(cur, 'cmp_cfg_param') or _exists(cur, CONFIG_TABLE)
            print('\n  ※ 기준정보가 아니라 이름 규칙으로 고르고 있습니다.')
            if not snap:
                print('    원인: 기준정보 테이블(cmp_cfg_param)이 없습니다.')
                print('    해결: 셋업 페이지(/monitor 옆 기준정보 셋업)에서')
                print('          이 공정을 등록하세요.')
            else:
                print('    원인: 기준정보는 있으나 이 공정의 PARAM 을 찾지')
                print('          못했습니다. OPER_ID 표기가 일치하는지 확인하세요.')
            print(f'    현재 범위: MONITOR_ALL_NUMERIC='
                  f'{MONITOR_ALL_NUMERIC} '
                  f'({"계측값 전부" if MONITOR_ALL_NUMERIC else "THK/TIME/DEFECT만"})')

        if source == '기준정보':
            print('\n  ※ 기준정보에 등록된 파라미터가 전부 대상이며,')
            print('    이름 규칙(폴백)은 사용되지 않습니다.')
            print('    대상을 바꾸려면 셋업 페이지에서 고치면 즉시 반영됩니다.')
            print(f'\n  기준정보 등록 {len(conf)}개:')
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
                if source == '기준정보':
                    why = '기준정보 미등록'
                elif p in META_COLS:
                    why = '메타 컬럼'
                elif RE_DERIVED.search(p):
                    why = '파생 컬럼(_OFFSET/_FORMULA)'
                else:
                    why = f'이름 규칙상 {t} → 대상 아님'
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
def _periodic(series, day, base_avg, base_std):
    """
    지금 값이 '주기적으로 반복되는 패턴' 인지 본다.

    ★ 매주 화요일마다 오르는 것처럼 규칙적으로 반복되는 이탈은
      그 공정의 성질이지 이상이 아니다. 그런데 매번 알림이 뜨면
      진짜 이상이 묻힌다.

    ★ 방법: 요일별로 평균을 내어, 오늘 요일이 원래 그런 요일인지 본다.
      - 오늘 요일의 과거 평균이 전체 평균에서 같은 방향으로 벗어나 있고
      - 그 정도가 지금 이탈과 비슷하며
      - 과거에 그 요일이 충분히 여러 번 있었다면
      주기적 반복으로 본다.

    ★ 요일만 보는 이유: PM·물량 계획이 주 단위로 도는 일이 많고,
      일별 평균 30~45개로 더 긴 주기를 추정하면 오탐이 늘어난다.
      (더 긴 주기가 필요해지면 그때 확장한다)

    반환: None 이면 주기성 없음.
          있으면 {'kind': 'weekday', 'label': ..., 'ratio': ...}
    """
    if not base_std or float(base_std) <= 0 or base_avg is None:
        return None

    pts = [s for s in series
           if s.get('avg') is not None and s.get('d')]
    if len(pts) < PERIODIC_MIN_DAYS:
        return None

    try:
        today = datetime.strptime(str(day)[:10], '%Y-%m-%d').date()
    except Exception:
        return None

    # 요일별로 모은다 (오늘 자신은 뺀다 — 과거 패턴과 비교해야 한다)
    by_wd = {}
    for s in pts:
        try:
            d = datetime.strptime(str(s['d'])[:10], '%Y-%m-%d').date()
        except Exception:
            continue
        if d >= today:
            continue
        by_wd.setdefault(d.weekday(), []).append(float(s['avg']))

    same = by_wd.get(today.weekday(), [])
    if len(same) < PERIODIC_MIN_SAMPLES:
        return None

    std = float(base_std)
    wd_avg = sum(same) / len(same)
    wd_sigma = (wd_avg - float(base_avg)) / std

    # 이 요일이 원래 벗어나 있는가
    if abs(wd_sigma) < PERIODIC_MIN_SIGMA:
        return None

    # 다른 요일과 견주어 이 요일만 두드러지는가 —
    #   전 요일이 고르게 벗어나 있으면 그건 요일 효과가 아니다
    others = [v for wd, vals in by_wd.items() if wd != today.weekday()
              for v in vals]
    if len(others) >= PERIODIC_MIN_SAMPLES:
        oth_sigma = (sum(others) / len(others) - float(base_avg)) / std
        if abs(wd_sigma - oth_sigma) < PERIODIC_MIN_SIGMA:
            return None

    names = ['월', '화', '수', '목', '금', '토', '일']
    return {
        'kind': 'weekday',
        'label': f'{names[today.weekday()]}요일',
        'wd_sigma': round(wd_sigma, 2),
        'n': len(same),
    }


def _no_data(lot_cd, param, ptype, why):
    """
    판정할 데이터가 없을 때 돌려주는 행.

    ★ 예전에는 None 을 반환해 결과 테이블에서 행이 통째로 사라졌다.
      그러면 '점검 대상에는 있는데 결과에는 없다' 가 되어 원인을 알 수 없다.
      (특히 값이 전부 NULL 인 컬럼도 숫자 타입이면 대상에 포함되므로
       이 경우가 드물지 않다)
      상태로 드러내서 적재 쪽 문제임을 바로 알 수 있게 한다.
    """
    return {
        'oper_id': None, 'lot_cd': lot_cd, 'param': param, 'ptype': ptype,
        'day': None, 'day_from': None, 'span': None,
        'day_stat': {'n': 0, 'avg': None, 'std': None,
                     'min': None, 'max': None},
        'base_stat': {'n': 0, 'avg': None, 'std': None, 'min': None,
                      'max': None, 'med': None, 'p95': None},
        'low_n': True, 'sigma': None, 'out_cnt': 0,
        'spread': None, 'drift': None, 'eqp': [], 'series': [],
        'checks': [], 'status': '데이터없음', 'reasons': [why],
        'severity': 0,
    }


def _check_param(cur, table, lot_cd, param, ptype, has_eqp, has_ch):
    # ★ 판정 구간은 "달력 하루" 가 아니라 "최신 데이터로부터 24시간" 이다.
    #   달력 기준이면 최신 데이터가 07:00 일 때 00:00~07:00 —
    #   7시간치로 판정하게 된다. 매시간 적재하므로 점검할 때마다
    #   표본 크기가 들쭉날쭉해지고, 그만큼 판정도 흔들린다.
    cur.execute(f'''
        SELECT MAX("DATE") FROM {table}
        WHERE "LOT_CD" = %s AND "{param}" IS NOT NULL
    ''', [lot_cd])
    last_ts = cur.fetchone()[0]
    day = last_ts.date() if last_ts else None
    if day is None:
        return _no_data(lot_cd, param, ptype,
                        f'이 LOT_CD 에 {param} 값이 하나도 없음 — '
                        f'적재 단계에서 값이 들어오지 않았는지 확인')

    # ── 판정 구간 ────────────────────────────────────────
    # 기본은 '최근일' 하루.
    # ★ defect 은 DEF_WINDOW 일을 묶을 수 있다. 샘플 검사라 하루만
    #   보면 표본이 한두 장뿐일 수 있어서다. 다만 여러 날을 묶으면
    #   어느 날 일이 생겼는지가 흐려지므로 기본값은 1(하루)이다.
    is_def = (ptype == 'DEFECT' and int(DEF_WINDOW) > 1)
    hours = int(DEF_WINDOW) * 24 if is_def else 24
    ts_from = last_ts - timedelta(hours=hours)
    ts_to = last_ts
    span_label = f'최근 {DEF_WINDOW}일' if is_def else '최근 24시간'
    # 화면·이력 표시는 날짜로 (기존 형식 유지)
    d_from = ts_from.date()
    # ★ 표본 기준은 구간 길이가 아니라 타입으로 정한다.
    #   Defect 은 샘플 검사라 하루 검사 장수가 원래 적다 —
    #   구간을 하루로 줄여도 그 사실은 그대로다.
    min_n = DEF_MIN_N if ptype == 'DEFECT' else MIN_N

    # 기준선 = 판정 구간 이전 전체
    cur.execute(f'''
        SELECT COUNT(*), AVG("{param}"), STDDEV("{param}"),
               MIN("{param}"), MAX("{param}"),
               PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY "{param}"),
               PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY "{param}")
        FROM {table}
        WHERE "LOT_CD" = %s AND "DATE" <= %s AND "{param}" IS NOT NULL
    ''', [lot_cd, ts_from])
    b_n, b_avg, b_std, b_min, b_max, b_med, b_p95 = cur.fetchone()

    # 판정 구간
    cur.execute(f'''
        SELECT COUNT(*), AVG("{param}"), STDDEV("{param}"),
               MIN("{param}"), MAX("{param}")
        FROM {table}
        WHERE "LOT_CD" = %s AND "DATE" > %s AND "DATE" <= %s
          AND "{param}" IS NOT NULL
    ''', [lot_cd, ts_from, ts_to])
    d_n, d_avg, d_std, d_min, d_max = cur.fetchone()

    # ★ 판정 구간을 결과에 남긴다.
    #   차트가 '어디까지가 점검 대상인지' 를 표시할 수 있어야
    #   구간 뒤에 점이 보일 때 그게 정상인지 판단할 수 있다.
    r_span = {'from': str(d_from), 'to': str(day), 'label': span_label,
              # ★ 시각까지 — '어제 07시부터 오늘 07시까지' 임을 알 수 있게
              'ts_from': str(ts_from)[:19], 'ts_to': str(ts_to)[:19]}
    if not d_n:
        return _no_data(lot_cd, param, ptype,
                        f'{span_label}({str(ts_from)[:16]} ~ '
                        f'{str(ts_to)[:16]}) 에 값이 없음 — '
                        f'해당 기간 측정이 없었을 수 있음')

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
    r['span'] = r_span
    # ★ 차트는 이 파라미터에 값이 있는 날짜만 그린다.
    #   예전엔 LOT_CD 만 걸러서, 그 파라미터가 없는 더 최신 날짜까지
    #   그려졌다 — '점검 구간 뒤에 점이 있다' 는 그 때문이다.
    r['series'] = _series(cur, table, lot_cd, param)

    if not b_n or b_avg is None:
        r['status'] = '기준없음'
        r['reasons'] = ['비교할 이전 데이터가 없음 (신규 파라미터/공정)']
        return r

    level = 0          # 0 정상 / 1 주의 / 2 이상
    reasons, checks = [], []

    # ── 소모품(PART) 은 판정하지 않는다 ───────────────────
    #   사용량이 누적되며 단조 증가하고 PM 에서 리셋되므로
    #   '30일 평균 대비 σ' 로 보면 매일 이탈로 잡힌다.
    #   추이는 스파크라인과 상세 차트로 볼 수 있게 값은 그대로 담는다.
    if ptype == 'PART' and not JUDGE_PART:
        if b_std and float(b_std) > 0:
            r['sigma'] = round((float(d_avg) - float(b_avg)) / float(b_std), 2)
        r['status'] = '참고'
        r['checks'] = []
        r['reasons'] = ['소모품 계열 — 값이 누적되고 PM 에서 리셋되므로 '
                        'σ 판정을 적용하지 않습니다 (추이만 참고)']
        r['severity'] = 0
        return r

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
                WHERE "LOT_CD" = %s AND "DATE" > %s AND "DATE" <= %s
                  AND "{param}" > %s
            ''', [lot_cd, ts_from, ts_to, b_p95])
            over = cur.fetchone()[0]
            r['out_cnt'] = over
            # ★ Defect 은 검사 장수가 적어 계측값과 같은 기준을 쓸 수 없다
            if over >= DEF_OUT_ALERT:
                level = 2
                reasons.append(f'30일 상위 5%({_f(b_p95)}) 초과 웨이퍼 {over}장 '
                               f'({span_label})')
                checks.append('D-상위초과')
            elif over >= DEF_OUT_WARN:
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
            WHERE "LOT_CD" = %s AND "DATE" > %s AND "DATE" <= %s
              AND "{param}" IS NOT NULL
              AND ("{param}" < %s OR "{param}" > %s)
        ''', [lot_cd, ts_from, ts_to, b_min, b_max])
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
            WHERE "LOT_CD" = %s AND "DATE" > %s AND "DATE" <= %s
              AND "{param}" IS NOT NULL
              AND COALESCE("{key_col}", '') <> ''
            GROUP BY "{key_col}" ORDER BY 1
        ''', [lot_cd, ts_from, ts_to])
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

    # ── 주기적 반복이면 등급을 낮춘다 ────────────────────
    #   ★ 이번 이탈이 '늘 그 요일에 그러던 것' 과 같은 방향·비슷한 크기면
    #     새로 생긴 일이 아니다. 지우지는 않고 '반복' 으로 표시해
    #     기본 목록에서만 빠지게 한다 — 정말 달라졌을 때를 놓치면 안 된다.
    #   ★ Defect 은 제외한다 — sigma 칸에 '배수' 를 담고 있어
    #     σ 기준으로 만든 주기 판정과 단위가 맞지 않는다.
    per = None
    if level > 0 and r['sigma'] is not None and ptype != 'DEFECT':
        per = _periodic(r.get('series') or [], day, b_avg, b_std)
        if per:
            same_dir = (r['sigma'] > 0) == (per['wd_sigma'] > 0)
            # 지금 이탈이 그 요일의 평소 이탈로 설명되는 정도
            explained = abs(per['wd_sigma']) / max(abs(r['sigma']), 1e-9)
            if same_dir and explained >= PERIODIC_EXPLAIN:
                level = 0
                r['periodic'] = per
                reasons.append(
                    f"{per['label']}마다 반복되는 패턴 "
                    f"(그 요일 평소 {per['wd_sigma']:+.1f}σ · "
                    f"{per['n']}회) — 새로 생긴 이탈이 아닙니다")
                checks.append('P-반복')

    # ── 등급 보정 ────────────────────────────────────────
    #   ★ 근거가 하나뿐이면 '이상' 으로 올리지 않는다.
    #     평균 이탈만, 또는 산포 확대만 걸린 것은 우연히도 자주 나온다.
    #     둘 이상이 겹칠 때가 실제로 볼 만한 경우다.
    #   ★ 표본이 적으면 한 단계 낮춘다 — 5장으로 낸 σ 는 믿기 어렵다.
    kinds = {c.split('-')[0] for c in checks}
    need = DEF_NEED_CHECKS if ptype == 'DEFECT' else NEED_CHECKS_FOR_ALERT
    if level == 2 and len(kinds) < need:
        level = 1
        reasons.append(f'근거가 하나뿐이라 주의로 낮춤 '
                       f'(이상 판정은 {need}가지 이상)')

    #   ★ Defect 은 표본이 적은 게 정상이라 여기서 낮추지 않는다.
    #     샘플 검사라 하루 두세 장인 날이 흔하다.
    if r['low_n'] and level > 0 and ptype != 'DEFECT':
        level -= 1
        reasons.append('표본이 적어 한 단계 낮춤')

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


def diagnose():
    """
    각 공정의 적재·점검 상태를 한 번에 본다.

    ★ shell 없이 화면에서 확인하려고 만든 것.
      '적재는 됐는데 점검이 옛 데이터로 돈다' 같은 질문은
      아래 네 값만 보면 대부분 바로 갈린다.
        · 테이블 존재 여부
        · 행수 / 최근 DATE   ← 웹이 실제로 보는 데이터
        · 점검 대상 파라미터 수
        · 대상 선정 방식(기준정보 / 기본규칙)
    """
    items, orphans = [], []

    with _conn().cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s "
                    "ORDER BY 1", ['cmp_analysis_%'])
        all_tables = [r[0] for r in cur.fetchall()]

        # 등록된 공정 기준으로 본다 (테이블명에서 OPER_ID 를 역산하면
        # 특수문자가 '_' 로 바뀌어 원래 ID 로 돌아오지 않는다)
        opers = []
        try:
            from . import config_service as cfg
            opers = [(o['oper_id'], o['oper_desc']) for o in cfg.list_opers()]
        except Exception as e:
            print(f'[monitor] 기준정보 조회 실패: {e.__class__.__name__}: {e}')

        known = set()
        for oper_id, desc in opers:
            table = _table(oper_id)
            known.add(table)
            it = {'oper_id': oper_id, 'oper_desc': desc or '', 'table': table,
                  'exists': table in all_tables, 'rows': 0, 'last_date': None,
                  'lot_cds': [], 'n_param': 0, 'source': '', 'note': ''}

            if not it['exists']:
                it['note'] = '적재 테이블이 없습니다 — 배치가 안 돌았거나 OPER_ID 표기가 다릅니다'
                items.append(it)
                continue

            cols = {c.upper() for c, _ in _cols(cur, table)}
            cur.execute(f'SELECT COUNT(*) FROM {table}')
            it['rows'] = cur.fetchone()[0]

            if 'DATE' in cols:
                cur.execute(f'SELECT MAX("DATE") FROM {table}')
                d = cur.fetchone()[0]
                it['last_date'] = str(d)[:19] if d else None
            if 'LOT_CD' in cols:
                cur.execute(f'SELECT DISTINCT "LOT_CD" FROM {table} '
                            f'WHERE "LOT_CD" IS NOT NULL ORDER BY 1')
                it['lot_cds'] = [str(r[0]) for r in cur.fetchall()]

            try:
                params, source = monitored_params(cur, table, oper_id)
                it['n_param'] = len(params)
                it['source'] = source
            except Exception as e:
                it['note'] = f'점검 대상 조회 실패: {e}'

            if not it['rows']:
                it['note'] = '테이블은 있으나 행이 없습니다'
            elif not it['n_param']:
                it['note'] = '점검 대상 파라미터가 없습니다 — 기준정보를 확인하세요'
            items.append(it)

        orphans = sorted(t for t in all_tables if t not in known)

    return {'items': items, 'orphans': orphans,
            'n_table': len(all_tables), 'n_oper': len(opers)}


def clear_results(oper_id=None, with_history=False):
    """
    저장된 점검 결과를 지운다.

      oper_id       주면 그 공정만, 생략하면 전체
      with_history  True 면 연속일수 계산용 이력까지 삭제
                    (연속일수를 다시 0부터 세게 된다)

    화면은 페이지를 열 때 저장된 결과를 불러오므로, 점검 대상 규칙을
    바꾼 뒤에는 옛 결과가 남아 혼동을 준다. 그때 초기화한다.
    """
    with _conn().cursor() as cur:
        _ensure_tables(cur)
        if oper_id:
            cur.execute(f'DELETE FROM {RESULT_TABLE} WHERE oper_id = %s',
                        [oper_id])
            n = cur.rowcount
            if with_history:
                cur.execute(f'DELETE FROM {HISTORY_TABLE} WHERE oper_id = %s',
                            [oper_id])
        else:
            cur.execute(f'DELETE FROM {RESULT_TABLE}')
            n = cur.rowcount
            if with_history:
                cur.execute(f'DELETE FROM {HISTORY_TABLE}')
    print(f'[monitor] 결과 {n:,}건 삭제'
          f'{" (이력 포함)" if with_history else ""}')
    return n


# 목록의 스파크라인에 쓸 점 수 — 이보다 촘촘해도 140px 안에서 안 보인다
SPARK_POINTS = 30


def load_results():
    """
    저장된 최근 점검 결과.

    ★ series(일별 시계열)를 통째로 내려보내면 파라미터 수백 개 × device 만큼
      쌓여 응답이 수십 MB 가 된다. 페이지를 여는 것만으로 버벅이던 원인이다.
      목록에는 스파크라인용 최소 점만 남기고, 상세 차트는 행을 펼칠 때
      따로 조회한다(wafer_detail).
    """
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
                r = json.loads(p)
            except Exception:
                continue

            # 목록에 필요 없는 큰 덩어리는 덜어 낸다
            ser = r.get('series') or []
            if len(ser) > SPARK_POINTS:
                step = len(ser) / SPARK_POINTS
                ser = [ser[int(i * step)] for i in range(SPARK_POINTS)] + ser[-1:]
            r['series'] = ser
            # eqp(장비별 요약)는 장비 수만큼이라 작다 — 그대로 둔다.
            #   행을 펼쳤을 때 바로 보여야 하므로 여기서 빼면 다시 조회해야 한다.
            out.append(r)

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
