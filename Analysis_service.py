"""
equipment/analysis_service.py
════════════════════════════════════════════════════════════
산포 분석 데이터 적재 파이프라인

  APC / SRC / MES(LC) 조회 (Lake·StarRocks)
      → SRC pivot(long→wide) / APC 압축 / MES lot단위 정리
      → 머지
      → EQP_CH_ID / PRE_EQP_CH 결정
      → PostgreSQL 저장

실행: 루트의 run_analysis_load.py (사내 스케줄러가 호출)

────────────────────────────────────────────────────────────
[★ 이번 수정]
  1. 값이 전부 비어 있는 컬럼을 VARCHAR 로 만들지 않는다 (숫자로 유지)
     → 화면에서 그 컬럼에 AVG/CORR 를 걸 때 나던 500 의 근본 원인
  2. IDLE 라벨 진단 로그 — idle_1 이 어디서 사라지는지 추적
     (APC 단계 라벨 분포 vs SRC 머지 후 라벨 분포를 나란히 출력)
  3. PRE_EQP_CH 를 '사전공정장비_챔버' 형태로 결합 (EQP_CH_ID 와 동일 규칙)

[★ 사용 전 확인]
  1. 아래 import 에 사내 모듈(lakes, goodDocsGetData) 실제 경로 입력
  2. OPTA_FIXED_CH 에 챔버 고정 공정의 oper_id 입력
  3. settings.py 의 DATABASES['analysis_db'] 설정 필요
════════════════════════════════════════════════════════════
"""

import re
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from django.db import connections
from psycopg2.extras import execute_values

# ★ 사내 모듈 — 기존 코드에서 쓰던 import 문을 그대로 넣을 것
# from ??? import lakes
# from ??? import goodDocsGetData


# 단계별 행수 / 진단 로그
VERBOSE = True

# 전체 교체 시 허용하는 최소 비율 — 기존의 이보다 적으면 저장을 멈춘다.
#   조회가 일부만 성공한 채로 덮어써서 데이터가 조용히 줄어드는 것을 막는다.
SHRINK_GUARD = 0.5

# SRC 와 APC 를 어떻게 붙일지.
#   'inner'  APC 에 있는 웨이퍼만 남긴다 — rework 가 자동으로 걸러지지만,
#            APC(R2R 이력)가 늦게 쌓이면 그 최신 구간이 통째로 빠진다.
#   'left'   측정된 웨이퍼는 모두 남기고 APC 값만 비운다 —
#            최신 데이터가 잘리지 않지만 rework 웨이퍼가 섞일 수 있다.
#   ★ 최신 데이터가 잘리는 편이 더 곤란하므로 left 를 기본으로 둔다.
#     APC 값이 비어 있는 행은 화면에서 그 컬럼만 빈칸으로 보인다.
SRC_APC_JOIN = 'left'


def _rows(d):
    """행수 (None/빈 df 는 0)"""
    if d is None:
        return 0
    try:
        return len(d)
    except TypeError:
        return 0


def _idle_dist(sr):
    """IDLE 라벨 분포 → 'idle_1:3, idle_2:12, ...' (빈값 제외)"""
    if sr is None or len(sr) == 0:
        return '(없음)'
    v = sr.fillna('').astype(str).str.strip()
    v = v[v != '']
    if v.empty:
        return '(없음)'
    c = v.value_counts().sort_index()
    return ', '.join(f'{k}:{n}' for k, n in c.items())


# ══════════════════════════════════════════════════════════
# 0. Lake 공통
# ══════════════════════════════════════════════════════════
def get_lake():
    """Lake(StarRocks) 연결. 공정 여러 개 돌 때 한 번만 만들어 재사용."""
    lake = lakes.LakeHouse(real_user_id='')
    lake.ensure_running(cluster_type='starrocks')
    return lake


def run_query(lake, query):
    lake.auto_run_sync_paragraph(code=query)
    return lake.get_rst().toPandas()


# ══════════════════════════════════════════════════════════
# 조회 실패 기록
#
#   ★ LOT_CD 하나가 실패해도 나머지는 계속 조회한다.
#     그 대신 무엇이 실패했는지 남겨 두어야 '일부만 적재된' 상태를
#     모르고 지나치지 않는다.
#   ★ 반환값 형태를 바꾸면 호출부를 전부 고쳐야 하므로
#     모듈 수준에 모으고 build_analysis_df 가 마지막에 읽는다.
# ══════════════════════════════════════════════════════════
_FETCH_FAILS = []


def clear_fails():
    _FETCH_FAILS.clear()


def note_fail(stage, key, err):
    _FETCH_FAILS.append({
        'stage': stage, 'key': str(key),
        'error': f'{err.__class__.__name__}: {err}',
    })
    print(f'  [{stage}] {key} 조회 실패 — 건너뜁니다: '
          f'{err.__class__.__name__}: {err}')


def get_fails():
    return list(_FETCH_FAILS)


def _full_range(days=30, date_from=None, date_to=None):
    """
    조회 기간 전체를 한 덩어리로 — (dt_s, dt_e, mt_s, mt_e).

    ★ 예전엔 30일씩 잘라 여러 번 조회했다. Lake 는 대용량이라
      나눠 던져도 이득이 없고, 왕복 횟수만 늘어 오히려 느렸다.
      한 번에 던지는 편이 빠르고 코드도 단순하다.
    ★ 아주 긴 기간(수백 일)을 조회할 일이 생기면 그때 다시 검토한다.
    """
    if date_from or date_to:
        d2 = pd.to_datetime(date_to).date() if date_to else date.today()
        d1 = (pd.to_datetime(date_from).date() if date_from
              else d2 - timedelta(days=days))
        if d1 > d2:
            d1, d2 = d2, d1
    else:
        d2 = date.today()
        d1 = d2 - timedelta(days=days)

    return (d1.strftime('%Y%m%d'), d2.strftime('%Y%m%d'),
            d1.strftime('%Y%m'), d2.strftime('%Y%m'))


def _date_chunks(days=30, freq='30D', date_from=None, date_to=None):
    """
    조회 기간을 (dt_start, dt_end, mt_start, mt_end) 단위로 분할.

      기본        오늘부터 days 일 전까지 (정기 적재)
      date_from/to 를 주면 그 구간 (1회성 임의 기간 조회)
                  — 'YYYY-MM-DD' 또는 date 객체
    """
    if date_from or date_to:
        date_start = pd.to_datetime(date_from).date() if date_from else None
        date_end   = pd.to_datetime(date_to).date() if date_to else date.today()
        if date_start is None:
            date_start = date_end - timedelta(days=days)
        if date_start > date_end:
            date_start, date_end = date_end, date_start
    else:
        date_end   = date.today()
        date_start = date_end - timedelta(days=days)
    rng = pd.date_range(start=date_start, end=date_end, freq=freq)
    if len(rng) < 2 or rng[-1].date() < date_end:
        rng = rng.append(pd.DatetimeIndex([pd.Timestamp(date_end)]))
    return [
        (rng[i].strftime("%Y%m%d"), rng[i + 1].strftime("%Y%m%d"),
         rng[i].strftime("%Y%m"),   rng[i + 1].strftime("%Y%m"))
        for i in range(len(rng) - 1)
    ]


def _month_range(days=30, date_from=None, date_to=None):
    """
    조회 기간을 덮는 mt(YYYYMM) 시작·끝.

    ★ 청크별 mt 를 쓰면 첫 청크가 '202606~202607' 이 되어 8월 데이터가
      빠진다. mt 로만 기간을 좁히는 rep/def 는 전체 기간을 한 번에 줘야 한다.
    """
    if date_from or date_to:
        d2 = pd.to_datetime(date_to).date() if date_to else date.today()
        d1 = (pd.to_datetime(date_from).date() if date_from
              else d2 - timedelta(days=days))
        if d1 > d2:
            d1, d2 = d2, d1
    else:
        d2 = date.today()
        d1 = d2 - timedelta(days=days)
    return d1.strftime('%Y%m'), d2.strftime('%Y%m')


def _recipe_cond(recipe_list, col='recipe_id'):
    """recipe 1개면 =, 여러 개면 IN"""
    rl = [r for r in recipe_list if r]
    if not rl:
        return ""
    if len(rl) == 1:
        return f"and {col} = '{rl[0]}'"
    return f"and {col} in (" + ",".join(f"'{r}'" for r in rl) + ")"


def _recipe_like_cond(recipe_list, col='eqp_recipe_id'):
    """
    여러 레시피를 prefix(LIKE) 로 OR 조합.

    구닥스 RECIPE_ID 는 확장자가 붙기도 하고(.CAS), 실제 장비에서는
    파생 레시피가 돌기도 해서 접두 일치로 비교한다.
    챔버가 다른 레시피(_AB / _CD)는 구닥스에 각각 등록하면 여기서 모두 조회된다.

    ★ NULL 을 통과시킨다.
      이 컬럼은 DCP 서브쿼리(c) 를 left join 해서 얻는데, lot_id 형식이
      달라 조인이 안 붙으면 NULL 이 된다. LIKE 는 NULL 에 대해 참이 될 수
      없으므로, NULL 을 허용하지 않으면 레시피가 등록된 공정만 통째로
      0행이 되어 버린다 (레시피 미등록 공정은 이 조건 자체가 없어
      멀쩡하므로 '일부 공정만 조회 안 됨' 으로 보인다).
      레시피로 거르는 목적은 '다른 레시피를 제외' 하는 것이지
      '레시피를 모르는 웨이퍼를 버리는' 것이 아니다.
    """
    bases = []
    for r in recipe_list:
        if not r:
            continue
        b = str(r).split('.')[0]          # 확장자 제거
        if b not in bases:
            bases.append(b)
    if not bases:
        return ""
    ors = " or ".join(f"{col} like '{b}%'" for b in bases)
    return f"and ({col} is null or {ors})"


def _param_tuple(param_list):
    """구닥스 PARAM 목록 → SQL IN 용 튜플 문자열"""
    params = [str(p) for p in param_list if p]
    if not params:
        return "('')"
    return "(" + ",".join(f"'{p}'" for p in params) + ")"


# ══════════════════════════════════════════════════════════
# 1. 기준정보
#    ★ PostgreSQL(cmp_cfg_*)이 원본이다. 구닥스가 불안정해 자체 DB 로
#      옮겼고, 수정은 웹 셋업 페이지(config.html)에서만 한다.
#    컬럼(대문자, PARAM 마다 1행 · 나머지 값 반복):
#      FAB, LOT_CD, OPER_ID, OPER_DESC, EQ_MODEL, RECIPE_ID,
#      PARAM, PRE_OPER_ID, PRE_OPER_DESC, PRE_OPER_PARAM, PARAM_TYPE
# ══════════════════════════════════════════════════════════
def get_config():
    """
    기준정보 — PostgreSQL 우선, 비어 있으면 구닥스 폴백.

    config_service.build_config_df() 가 구닥스가 주던 것과 똑같은
    평면 DataFrame 을 만들어 주므로, get_oper_cond 이하 파이프라인은
    한 줄도 바뀌지 않는다.

    폴백은 이관 도중 배치가 멈추지 않게 하기 위한 것이고,
    셋업이 끝나면 타지 않는다.
    """
    # ★ v1(cmp_cfg_*)과 v2(cmp_cfg2_*)를 합쳐서 쓴다.
    #   config2 에만 등록한 공정이 적재에서 통째로 빠지던 문제 때문이다.
    #   같은 OPER_ID 가 양쪽에 있으면 v2 를 우선한다 — 새로 등록한 쪽이
    #   최신이라고 보는 것이 자연스럽다.
    frames, srcs = [], []

    # ★ 기준정보는 config_service 하나로 통합됐다 (config2 폐기)
    for name, mod, label in (('v1', 'config_service', 'cmp_cfg_*'),):
        try:
            m = __import__(f'{__package__}.{mod}', fromlist=[mod])
            d = m.build_config_df()
            if d is not None and len(d):
                d = d.copy()
                d['__src'] = name
                frames.append(d)
                srcs.append(f'{label} {len(d):,}행')
        except Exception as e:
            print(f'[config] {label} 조회 생략: {e.__class__.__name__}: {e}')

    if frames:
        df = pd.concat(frames, ignore_index=True)
        # v2 우선 — 같은 공정이 양쪽에 있으면 v1 쪽을 버린다
        v2_opers = set(df.loc[df['__src'] == 'v2', 'OPER_ID'])
        if v2_opers:
            drop = (df['__src'] == 'v1') & (df['OPER_ID'].isin(v2_opers))
            if drop.any():
                print(f'[config] v1·v2 중복 {drop.sum():,}행 — v2 를 씁니다')
            df = df[~drop]
        df = df.drop(columns=['__src'])
        print(f'[config] 기준정보 DB 사용 — {" + ".join(srcs)} → '
              f'{len(df):,}행 / 공정 {df["OPER_ID"].nunique()}개')
        return df

    print('[config] cmp_cfg_* / cmp_cfg2_* 가 모두 비어 있어 '
          '구닥스로 폴백합니다 (셋업 페이지에서 등록하세요)')

    # ── 폴백: 구닥스 ─────────────────────────────────────
    #   사내 모듈이 연결돼 있지 않으면 NameError 만 나서 원인을 알기 어렵다.
    #   무엇이 문제이고 무엇을 해야 하는지 분명히 알린다.
    try:
        return goodDocsGetData().dropna(axis=0)
    except NameError:
        raise RuntimeError(
            '기준정보를 읽지 못했습니다.\n'
            '  · cmp_cfg_* 테이블이 비어 있고\n'
            '  · 구닥스 조회 함수(goodDocsGetData)도 연결돼 있지 않습니다.\n'
            '해결: 셋업 페이지에서 공정을 등록하거나, '
            'analysis_service.py 상단의 사내 모듈 import 를 채워주세요.\n'
            '확인: config_service.build_config_df() 가 몇 행을 돌려주는지 보세요.')


def get_oper_list(df_info):
    """공정 단위 목록 (OPER_ID 별 대표 1행)"""
    return df_info.drop_duplicates(subset=['OPER_ID'])


# 모델별 챔버 파라미터 대칭 규칙
#   (좌 접두사, 우 접두사) — 좌를 등록하면 우를 자동 생성한다.
#   접두사 뒤에는 숫자가 붙을 수 있고(PL1), 그 뒤 나머지는 그대로 유지.
#     EBARA: PA=PC, PB=PD   (PA_03_TIME → PC_03_TIME)
#     KCT  : PL=PR          (PL_4_TIME → PR_4_TIME, PL1_4_TIME → PR1_4_TIME)
CHAMBER_TWINS = {
    'EBARA':   [('PA', 'PC'), ('PB', 'PD')],
    'KCT_NTA': [('PL', 'PR')],
    'KCT_NTH': [('PL', 'PR')],
}


def _expand_chamber_params(param_list, model):
    """
    챔버 파라미터 자동 확장.
    구닥스에 한쪽(PA/PB 또는 PL)만 등록해도 짝(PC/PD 또는 PR)을 채운다.

    접두사 바로 뒤가 '숫자들 + _' 형태일 때만 확장한다.
      PL_4_TIME  → 접두사 PL, 뒤 _4_TIME       → PR_4_TIME
      PL1_4_TIME → 접두사 PL, 숫자 1, 뒤 _4_TIME → PR1_4_TIME
    PART_CNT, PADCNT 처럼 접두사 뒤가 문자면 건드리지 않는다.
    """
    twins = CHAMBER_TWINS.get(str(model).upper())
    if not twins:
        return param_list

    out  = list(param_list)
    have = set(str(p) for p in param_list)

    for p in param_list:
        s = str(p)
        for left, right in twins:
            # 접두사 + (선택적 숫자) + _ + 나머지
            m = re.match(rf'^{left}(\d*)_(.+)$', s, re.IGNORECASE)
            if m:
                twin = f"{right}{m.group(1)}_{m.group(2)}"
                if twin not in have:
                    out.append(twin)
                    have.add(twin)
                break
    return out


def get_oper_cond(df_info, oper_id):
    """
    한 공정의 조회 조건 묶음.

    ★ LOT_CD(device)마다 레시피·파라미터·사전공정이 다르다.
      예전에는 전부 합쳐서 조회했다 — 5E2 를 조회하면서 5E9 의
      레시피·파라미터까지 조건에 넣었고, 사전공정은 첫 행 것만 써서
      나머지 device 의 사전공정은 통째로 무시됐다.
      이제 by_lot 에 device 별 조건을 따로 담는다.

    ★ 위쪽(lot_cd_list·param_list 등)은 예전 형태를 유지한다 —
      화면 목록이나 컬럼 이름 계산처럼 '전체' 가 필요한 곳이 있어서다.
      실제 조회는 by_lot 을 쓴다.
    """
    sub   = df_info[df_info['OPER_ID'] == oper_id]
    first = sub.iloc[0]

    # 챔버 파라미터 자동 확장 (EBARA: PA/PB→PC/PD, KCT: PL→PR)
    param_list = _expand_chamber_params(
        sub['PARAM'].unique().tolist(), first['EQ_MODEL'])

    # ── device 별 조건 ───────────────────────────────────
    by_lot = {}
    for lot_cd, g in sub.groupby('LOT_CD'):
        gf = g.iloc[0]
        by_lot[str(lot_cd)] = {
            'lot_cd':         str(lot_cd),
            'recipe_list':    [r for r in g['RECIPE_ID'].unique().tolist()
                               if str(r).strip()],
            'param_list':     _expand_chamber_params(
                                  g['PARAM'].unique().tolist(),
                                  gf['EQ_MODEL']),
            'eq_model':       gf['EQ_MODEL'],
            'pre_oper_id':    gf.get('PRE_OPER_ID', ''),
            'pre_oper_desc':  gf.get('PRE_OPER_DESC', ''),
            'pre_oper_param': gf.get('PRE_OPER_PARAM', ''),
        }

    return {
        'fab':            str(first['FAB']).lower(),      # 테이블명에 소문자로 들어감
        'lot_cd_list':    sub['LOT_CD'].unique().tolist(),
        'oper_id':        oper_id,
        'oper_desc':      first['OPER_DESC'],
        'eq_model':       first['EQ_MODEL'],
        'recipe_list':    sub['RECIPE_ID'].unique().tolist(),
        'param_list':     param_list,
        'pre_oper_id':    first['PRE_OPER_ID'],
        'pre_oper_desc':  first['PRE_OPER_DESC'],
        'pre_oper_param': first['PRE_OPER_PARAM'],
        # ★ device 별 조건 — 실제 조회는 이것을 쓴다
        'by_lot':         by_lot,
        # Inline 계측 — 등록돼 있을 수도, 없을 수도 있다.
        # 없으면 빈 목록이고 조회 자체를 건너뛴다.
        'resp_steps':     _step_cond(oper_id, 'resp'),
        'def_steps':      _step_cond(oper_id, 'def'),
    }


def _step_cond(oper_id, kind):
    """
    기준정보에서 Response·Defect 계측 스텝을 읽어 조회 조건으로 만든다.

    반환: [{'step_id','step_desc','lot_cds':[...],'params':[...]}]

    ★ 등록이 없으면 빈 목록을 돌려준다 — Response·Defect 는 선택 사항이고,
      없더라도 APC·SRC·MES 병합 테이블은 그대로 만들어져야 한다.
    ★ 기준정보를 못 읽어도 빈 목록이다. 계측 하나 때문에
      전체 적재가 멈추면 안 된다.
    """
    try:
        from . import config_service as cfg
        df = (cfg.build_response_config_df() if kind == 'resp'
              else cfg.build_defect_config_df())
    except Exception as e:
        print(f'[{kind}] 기준정보 조회 생략: {e.__class__.__name__}: {e}')
        return []

    if df is None or df.empty:
        return []
    df = df[df['OPER_ID'] == str(oper_id).upper()]
    if df.empty:
        return []

    out = {}
    for _, r in df.iterrows():
        prm = str(r['PARAM'] or '').strip()
        if not prm:
            continue                     # 관리 파라미터가 없으면 조회할 게 없다
        k = str(r['STEP_ID'] or '').strip().upper()
        if not k:
            continue
        o = out.setdefault(k, {'step_id': k,
                               'step_desc': str(r['STEP_DESC'] or '').strip(),
                               'lot_cds': [], 'params': []})
        if prm not in o['params']:
            o['params'].append(prm)
        lc = str(r['LOT_CD'] or '').strip().upper()
        if lc and lc not in o['lot_cds']:
            o['lot_cds'].append(lc)
    return list(out.values())


# ══════════════════════════════════════════════════════════
# 2. APC 조회
#    idle / layer_change 플래그 + APC 파라미터
#    ※ c.eqp_id 필수 (a 에는 없음)
# ══════════════════════════════════════════════════════════
def fetch_apc(lake, cond, days=30, date_from=None, date_to=None,
              on_progress=None):
    fab = cond['fab']
    dfs = []

    # ★ 기간을 나누지 않고 한 번에 던진다
    chunks = [_full_range(days, date_from=date_from, date_to=date_to)]
    total, done = max(1, len(chunks)), 0

    for dt_s, dt_e, mt_s, mt_e in chunks:
        query = f"""
select distinct *
from (
    select a.request_dtts, c.process_id, c.recipe_id, c.operation_id,
           c.lot_id, c.eqp_id, a.substrate_id, a.input_name, a.r2r_status,
           a.input_value, b.item_name, b.item_value,
           RANK() over(partition by a.substrate_id order by a.request_dtts DESC) r2r_rank,
           c.qty
    from lake_catalog.apc.apc_inquiry_hst_r2r_{fab} a
    left join lake_catalog.apc.apc_inquiry_ext_hst_r2r_{fab} b
           on a.rawid = b.inquiry_hst_rawid
    left join lake_catalog.apc.apc_lot_hst_r2r_{fab} c
           on a.lot_hst_rawid = c.rawid
    where a.dt between '{dt_s}' and '{dt_e}'
      and b.dt between '{dt_s}' and '{dt_e}'
      and c.mt between '{mt_s}' and '{mt_e}'
      and c.operation_id = '{cond['oper_id']}'
      and ( a.model_name like '%CMP%'
         or a.model_name like '%KCC88%'
         or a.model_name like '%KCC01%' )
      and a.input_value is not null
      and b.item_name in ('FORMULA', 'PROCESS_OFFSET_MES_IDLE_FLAG_IDLE',
                          'IDLE_TIME', 'PROCESS_OFFSET_WAFER_SEQ')
      and c.lot_status = 'JobEnd'
) d
where d.r2r_rank = 1
{_recipe_cond(cond['recipe_list'])}
"""
        df = run_query(lake, query)
        if df is not None and not df.empty:
            dfs.append(df)

        done += 1
        n = sum(len(d) for d in dfs)
        if on_progress:
            on_progress(done, total, f'APC {dt_s[:8]} · {n:,}행')
        if VERBOSE:
            print(f'  [APC] {done}/{total} {dt_s}~{dt_e} 누적 {n:,}행')

    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True).drop_duplicates()


# ══════════════════════════════════════════════════════════
# 3. SRC 조회
#    측정값(long) + 사전공정 장비/챔버
#    ※ lot 조건은 right(lot_cd, 3) — 3자리 (5E2). 2자리로 하면 0행!
# ══════════════════════════════════════════════════════════
def fetch_src(lake, cond, days=30, date_from=None, date_to=None,
              on_progress=None):
    fab      = cond['fab']
    oper_id  = cond['oper_id']

    # ★ 조건은 LOT_CD(device)마다 다르다. 여기서는 기본값만 잡고,
    #   실제 값은 아래 루프에서 device 별로 다시 정한다.
    #   예전엔 여기서 한 번 정해 전 device 에 같은 조건을 썼다 —
    #   5E2 를 조회하면서 5E9 의 레시피·파라미터가 섞였고,
    #   사전공정은 첫 device 것만 쓰였다.
    by_lot = cond.get('by_lot') or {}

    # ★ 사전공정이 없으면 wafer-history 조인을 통째로 건너뛴다.
    #   이 조인은 m10/m11/m14/m15 4개 테이블을 UNION 하는 가장 무거운 부분인데,
    #   pre_oper 가 비면 operation_id like '%' 가 되어 전체를 훑는다.
    #   (use_pre 는 device 별로 루프 안에서 정한다)
    if not any(v.get('pre_oper_id') for v in by_lot.values()) and VERBOSE:
        print('  [SRC] 사전공정 미지정 — wafer-history 조인 생략 (조회가 빨라집니다)')

    # ★ 기간을 나누지 않고 한 번에 던진다
    chunks = [_full_range(days, date_from=date_from, date_to=date_to)]
    total  = max(1, len(cond['lot_cd_list']) * len(chunks))
    done   = 0

    dfs = []
    for lot_code in cond['lot_cd_list']:
        # ── 이 device 의 조건 ────────────────────────────
        lc = by_lot.get(str(lot_code), {})
        param_in = _param_tuple(lc.get('param_list') or cond['param_list'])
        recipe_list = [r for r in (lc.get('recipe_list')
                                   or cond['recipe_list']) if r]
        recipe_cond = _recipe_like_cond(recipe_list, 'c.eqp_recipe_id')

        pre_oper = str(lc.get('pre_oper_id') or cond.get('pre_oper_id') or '')
        pre_oper_r1 = pre_oper[:-1] if pre_oper else ''
        use_pre = bool(pre_oper)

        if VERBOSE:
            print(f'  [SRC] {lot_code} — param {len(param_in.split(chr(44)))}개 · '
                  f'recipe {len(recipe_list)}개 · '
                  f'사전공정 {pre_oper or "(없음)"}')

        for dt_s, dt_e, mt_s, mt_e in chunks:
            # ★ end_tm 은 시각까지 있는 값이다.
            #   '2026-08-27' 로 비교하면 '2026-08-27 00:00:00' 이 되어
            #   그날 데이터가 통째로 잘린다 — 마지막 하루가 늘 비던 원인.
            #   끝은 23:59:59 까지 명시한다.
            dt_start = pd.to_datetime(dt_s).strftime("%Y-%m-%d 00:00:00")
            dt_end   = pd.to_datetime(dt_e).strftime("%Y-%m-%d 23:59:59")

            # SUBSTRING 길이는 lot_code 자릿수에 맞춘다.
            #   길이를 고정하면(2 또는 3) 다른 자릿수를 쓰는 공정에서
            #   절대 일치하지 않아 DCP 조인이 통째로 실패하고,
            #   recipe 필터가 걸린 공정만 조용히 0행이 된다.
            lot_len = len(str(lot_code))
            # ── 사전공정 조인(b) — 필요할 때만 만든다 ──────
            #   m10/m11/m14/m15 UNION 이 이 쿼리에서 가장 무겁다.
            if use_pre:
                pre_cols = """b.eqp_id as pre_eqp_id, b.module_id as pre_eqp_ch,
           b.last_update_dtts as pre_oper_time,"""
                units = ('m10', 'm11', 'm14', 'm15')
                pre_join = "    left join (\n" + "\n        union\n".join(
                    f"""        select lot_id, slot_id, wf_id, eqp_id, module_id,
               MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_{u}
        where dt between '{dt_s}' and '{dt_e}'
          and operation_id like '{pre_oper_r1}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id"""
                    for u in units) + "\n    ) b on a.lot_id = b.lot_id and a.wf_id = b.wf_id\n"
            else:
                # 조인을 안 하므로 컬럼은 NULL 로 채운다 (뒤 단계 구조 유지)
                pre_cols = """CAST(NULL as VARCHAR) as pre_eqp_id,
           CAST(NULL as VARCHAR) as pre_eqp_ch,
           CAST(NULL as TIMESTAMP) as pre_oper_time,"""
                pre_join = ""

            # rework 전(ASC) / 후(DESC) — SRC_PICK 으로 정한다
            src_order = 'ASC' if str(SRC_PICK).lower() == 'first' else 'DESC'

            query = f"""
WITH src AS (
    select a.lot_id, a.wf_id,
           concat(CAST(a.alias_lot_id as VARCHAR), '.', CAST(a.wf_id as VARCHAR)) as substrate_id,
           a.main_eqp_id, a.param_nm, a.oper_id, a.oper_det_desc,
           a.meas_val as thk_value, a.end_tm,
           {pre_cols}
           RANK() over(partition by a.lot_id, a.wf_id, a.param_nm order by a.end_tm {src_order}) r2r_rank,
           COUNT(*) over(partition by a.lot_id, a.wf_id, a.param_nm) meas_n
    from lake_catalog.tas.tas_src_wf_metr_inf a
{pre_join}    left join (
        select distinct d.lot_id, d.eqp_recipe_id, d.recipe_rank
        from (
            select lot_id, crt_tm, eqp_recipe_id,
                   rank() over (partition by lot_id order by crt_tm asc) recipe_rank
            from lake_catalog.dcp.dcp_dcp_dcoldata_inf_{fab}
            where ( SUBSTRING(lot_id, 2, {lot_len}) = '{lot_code}'
                 or SUBSTRING(lot_id, 2, 2) = 'XC'
                 or SUBSTRING(lot_id, 1, 1) = 'S' )
              and oper_id = '{oper_id}'
              and dt between '{dt_s}' and '{dt_e}'
        ) d
        where d.recipe_rank = 1
    ) c on a.lot_id = c.lot_id
    where a.mt between '{mt_s}' and '{mt_e}'
      and a.end_tm >= '{dt_start}'
      and a.end_tm <= '{dt_end}'
      and a.oper_id = '{oper_id}'
      and right(a.lot_cd, 3) = '{lot_code}'
      {recipe_cond}
      and a.param_nm in {param_in}
"""
            # 일부 사전공정만 module_id 제한 (하드코딩 · 필요시 추가)
            #   조인을 생략했으면 b 가 없으므로 조건도 붙이지 않는다
            if not use_pre:
                query += "\n)"
            elif pre_oper in ('V5071000B', 'X106100B', 'T5515000C'):
                query += "      and ( b.module_id = '2' or b.module_id = '3' )\n)"
            elif pre_oper in ('T5515000M', 'T5515000A'):
                query += "      and ( b.module_id = '2' or b.module_id = '3' or b.module_id = '5' )\n)"
            else:
                query += "\n)"

            query += """
select *
from (
    select src.*,
           row_number() over (partition by src.substrate_id, src.param_nm
                              order by src.pre_oper_time desc) as rn
    from src
    where src.r2r_rank = 1
) t
where rn = 1
"""
            # ★ 한 LOT_CD 가 실패해도 나머지는 계속 조회한다.
            #   예전엔 여기서 예외가 나면 공정 전체가 멈춰
            #   앞서 받은 것까지 버려졌다.
            try:
                df = run_query(lake, query)
                if df is not None and not df.empty:
                    dfs.append(df)
                    # ★ Lake 원본이 어디까지 왔는지 — 여기가 시작점이다.
                    #   이 값이 이미 밀려 있으면 Lake 쪽 문제이고,
                    #   여기는 최신인데 뒤에서 밀리면 전처리 문제다.
                    if VERBOSE:
                        c = next((x for x in df.columns
                                  if str(x).upper() == 'END_TM'), None)
                        if c is not None:
                            v = pd.to_datetime(df[c], errors='coerce').max()
                            print(f'    [SRC] {lot_code} 원본 최신 '
                                  f'{str(v)[:19]} · {len(df):,}행')
            except Exception as e:
                note_fail('SRC', lot_code, e)

            done += 1
            n = sum(len(d) for d in dfs)
            if on_progress:
                on_progress(done, total, f'SRC {lot_code} {dt_s[:8]} · {n:,}행')
            if VERBOSE:
                print(f'  [SRC] {done}/{total} {lot_code} {dt_s}~{dt_e} '
                      f'누적 {n:,}행')

    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True).drop_duplicates()


# ══════════════════════════════════════════════════════════
# 4. MES(LC) 조회 — layer change
#    장비별 recipe 이력을 시간순으로 놓고 직전 recipe 에서 이전 layer 유도
# ══════════════════════════════════════════════════════════
# 모델별 챔버 규칙
#   챔버 표기는 recipe_id 중간(_L_)에도, 끝(_L)에도 올 수 있음
#   → r'_L(_|$)' 형태여야 함. r'_L$' 로 하면 전부 걸러져 0행이 됨
# ★ 모델 추가 시 여기만 수정
_KCT_CH = {
    'L': {'include': r'_L(_|$)', 'exclude': r'_R(_|$)'},
    'R': {'include': r'_R(_|$)', 'exclude': r'_L(_|$)'},
}

MODEL_CH_CONFIG = {
    'KCT_NTA': _KCT_CH,               # 구 ELASTIC
    'KCT_NTH': _KCT_CH,
    'EBARA': {
        'AB': {'include': r'_AB(_|$)', 'exclude': r'_CD(_|$)'},
        'CD': {'include': r'_CD(_|$)', 'exclude': r'_AB(_|$)'},
    },
    'OPTA': {
        None: {'include': None, 'exclude': None},   # recipe 에 챔버 표기 없음(.CAS)
    },
}
DEFAULT_CH_CONFIG = MODEL_CH_CONFIG['OPTA']


def _derive_before_info(s):
    """직전 recipe_id → 이전 layer 표기(LC_xxx)"""
    parts = s.str.split('_')
    p0, p1, p2 = parts.str[0], parts.str[1], parts.str[2]
    three = 'LC_' + p0 + '_' + p1 + '_' + p2
    two   = 'LC_' + p0 + '_' + p1
    return np.where(s.str.contains('ADD_|T_|TB_', na=False), three, two)


def _lc_by_chamber(lc_df, eqp_id, ch, rule, recipe_infos):
    """장비 1대 × 챔버 1개의 layer change 추출"""
    d = lc_df[lc_df['eqp_id'] == eqp_id]

    if rule['include']:
        d = d[d['recipe_id'].str.contains(rule['include'], na=False, regex=True)]
    if rule['exclude']:
        d = d[~d['recipe_id'].str.contains(rule['exclude'], na=False, regex=True)]
    if d.empty:
        return None

    d = d.sort_values('event_tm').copy()
    d['before_recipe_id'] = d['recipe_id'].shift()
    d = d.dropna(subset=['before_recipe_id'])
    if d.empty:
        return None

    d['before_info']    = _derive_before_info(d['before_recipe_id'])
    d['recipe_id_info'] = (d['recipe_id'].str.split('_').str[0] + '_' +
                           d['recipe_id'].str.split('_').str[1])
    d = d[d['recipe_id_info'].isin(recipe_infos)]
    if d.empty:
        return None

    d['eqp_ch'] = ch if ch else ''
    d['rank']   = 1
    return d


def fetch_mes(lake, cond, df_src, days=30, date_from=None, date_to=None,
              on_progress=None):
    """
    MES(LC) 조회.
      df_src : SRC 결과 — 여기서 main_eqp_id 로 장비 목록을 뽑는다
               (APC 의 eqp_id 는 lot 테이블 c 쪽이라 안정적이지 않음)
    """
    if df_src is None or df_src.empty:
        return pd.DataFrame()

    fab = cond['fab']

    # 구닥스 RECIPE_ID 는 전체 recipe 명(E2_M1CU_R12_TSV.CAS).
    # recipe_id_info 는 앞 두 파트(E2_M1CU) 이므로 prefix 로 변환해 비교해야 함
    recipe_infos = []
    for r in cond['recipe_list']:
        if not r:
            continue
        prefix = '_'.join(str(r).split('_')[:2])
        if prefix not in recipe_infos:
            recipe_infos.append(prefix)

    src = df_src.copy()
    src.columns = src.columns.str.lower()
    eqp_ids = src['main_eqp_id'].dropna().unique()
    if len(eqp_ids) == 0:
        return pd.DataFrame()
    eqp_in = "'" + "','".join(map(str, eqp_ids)) + "'"

    dfs = []
    # ★ 기간을 나누지 않고 한 번에 던진다
    _mes_chunks = [_full_range(days, date_from=date_from, date_to=date_to)]
    _mes_total, _mes_done = max(1, len(_mes_chunks)), 0

    for dt_s, dt_e, _, _ in _mes_chunks:
        query = f"""
select eqp_id, event_tm, last_recipe_id as recipe_id,
       resv_field_val_3 as lot_id
from lake_catalog.mes.mes_mes_eqpmasext_his_{fab}
where dt between '{dt_s}' and '{dt_e}'
  and eqp_id in ({eqp_in})
  and event_cd = 'JobStart'
"""
        try:
            d = run_query(lake, query)
            if d is not None and not d.empty:
                dfs.append(d)
        except Exception as e:
            note_fail('MES', f'{dt_s}~{dt_e}', e)

        _mes_done += 1
        if on_progress:
            on_progress(_mes_done, _mes_total, f'MES {dt_s[:8]}')

    if not dfs:
        return pd.DataFrame()

    lc_df = pd.concat(dfs, ignore_index=True).drop_duplicates()
    lc_df.columns = lc_df.columns.str.lower()

    model    = str(cond.get('eq_model') or '').upper()
    ch_rules = MODEL_CH_CONFIG.get(model, DEFAULT_CH_CONFIG)

    out = []
    for eqp_id in lc_df['eqp_id'].unique():
        for ch, rule in ch_rules.items():
            part = _lc_by_chamber(lc_df, eqp_id, ch, rule, recipe_infos)
            if part is not None:
                out.append(part)

    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)


# ── SRC 측정값 선택 기준 ──────────────────────────────────
#   같은 lot_id·wf_id·param_nm 에 측정이 여러 번 쌓이는 경우가 있다.
#   rework(재연마 후 재측정)를 해도 lot_id·wf_id 는 바뀌지 않으므로
#   같은 키에 2건 이상이면 rework 이 있었다는 뜻이다.
#
#     'first'  가장 먼저 측정한 값 = rework 전  ← 기본
#     'last'   가장 나중 측정한 값 = rework 후
#
#   ★ 분석에는 rework 전 값이 맞다. rework 후 값만 보면
#     '문제가 있어서 재작업한 사실' 이 지워져 공정이 늘 정상으로 보인다.
#   ★ APC 는 반대다. 한 번 공정에 R2R 계산 요청이 2~3번 있으므로
#     마지막 요청이 실제 적용 조건이다 (그대로 DESC 유지).
SRC_PICK = 'first'


# ══════════════════════════════════════════════════════════
# 4-B. Inline 계측 조회 — Response / Defect
#
#   Inline 에서 실제로 관리하는 항목이다. 기준정보에 등록돼 있을 때만
#   조회하며, 없으면 조용히 건너뛴다 (APC·SRC·MES 병합은 그대로 진행).
#
#   ★ 두 테이블은 컬럼 이름이 서로 다르다 —
#       Response : oper_id  / param_nm         / meas_val
#       Defect   : step_id  / defect_class_nm  / meas_defect_cnt
#       필터도 fab vs fab_id 로 다르다.
#     조회 후 같은 이름으로 정규화해 뒤 단계를 하나로 만든다.
#
#   ★ 병합 키는 substrate_id (= alias_lot_id + '.' + wf_id).
#     SRC 와 만드는 규칙이 같아야 붙는다. alias_lot_id 는 이미 7자리로
#     정리된 값이므로 여기서 자르지 않는다.
# ══════════════════════════════════════════════════════════
# ── 조회 SQL ──────────────────────────────────────────────
#   ★ 사용자가 준 쿼리를 그대로 둔다.
#     select 절에서 컬럼 이름을 바꾸지 않는다 — 이름 정리는 조회 후
#     파이썬에서 한다. SQL 을 손대면 Lake 에서 실패했을 때
#     원본 쿼리와 대조하기가 어려워진다.
#   ★ mt 는 언제나 between 이다. 같은 달이어도 between 을 쓴다 —
#     조건 형태가 상황에 따라 달라지면 실패했을 때 대조하기 어렵다.
#   ★ fab 조건은 넣지 않는다. lot_cd 만으로 대상이 정해지고,
#     fab 값 대소문자 때문에 조회가 실패한 적이 있다.
SQL_REP = """
select alias_lot_id, wf_id, end_tm as rep_end_tm, oper_id, oper_det_desc,
       param_nm, meas_val
from lake_catalog.tas.tas_rep_wf_metr_inf
where mt between '{mt_s}' and '{mt_e}'
  and lot_cd = '{lot_cd}'
  and oper_id = '{step}'
  and param_nm in ({params})
"""

SQL_DEF = """
select alias_lot_id, wf_id, step_id, defect_class_nm, meas_defect_cnt,
       end_tm as def_end_tm
from lake_catalog.tas.tas_dft_wf_inf
where mt between '{mt_s}' and '{mt_e}'
  and lot_cd = '{lot_cd}'
  and step_id = '{step}'
  and defect_class_nm in ({params})
"""

# ── 실행한 쿼리 보관 ──────────────────────────────────────
#   ★ Lake 조회가 실패하면 '무슨 쿼리를 던졌는지' 를 봐야 원인을 안다.
#     최근 것만 들고 있다가 로그·터미널에서 꺼내 볼 수 있게 한다.
from collections import deque
QUERY_LOG = deque(maxlen=40)


def _log_query(label, query, rows=None, error=None):
    QUERY_LOG.append({
        'at': datetime.now().strftime('%H:%M:%S'),
        'label': label, 'query': query.strip(),
        'rows': rows, 'error': str(error) if error else None,
    })


def last_queries(n=5, label=None):
    """최근 실행한 쿼리 (터미널·shell 에서 확인용)"""
    items = [q for q in QUERY_LOG if not label or q['label'] == label]
    return list(items)[-n:]


def print_queries(n=5, label=None):
    for q in last_queries(n, label):
        head = f"[{q['at']}] {q['label']}"
        tail = (f"실패: {q['error']}" if q['error']
                else f"{q['rows']:,}행" if q['rows'] is not None else '')
        print(f'\n─── {head} · {tail} ' + '─' * 30)
        print(q['query'])


# 조회 후 컬럼 이름 통일 — 뒤 단계(pivot·merge)를 하나로 쓰기 위한 것
STEP_SQL = {'resp': SQL_REP, 'def': SQL_DEF}
STEP_RENAME = {
    'resp': {'oper_id': 'step_id', 'oper_det_desc': 'lake_step_desc',
             'param_nm': 'param', 'meas_val': 'value',
             'rep_end_tm': 'end_tm'},
    'def':  {'defect_class_nm': 'param', 'meas_defect_cnt': 'value',
             'def_end_tm': 'end_tm'},
}
STEP_LABEL = {'resp': 'REP', 'def': 'DEF'}


def fetch_steps(lake, cond, kind, days=30, date_from=None, date_to=None,
                on_progress=None):
    """
    Response 또는 Defect 계측값 조회 (long).

    반환 컬럼: substrate_id, alias_lot_id, wf_id, step_id, step_desc,
              param, value, end_tm
    기준정보에 등록된 스텝이 없으면 빈 DataFrame.
    """
    label = STEP_LABEL[kind]
    steps = cond.get('resp_steps' if kind == 'resp' else 'def_steps') or []
    if not steps:
        if VERBOSE:
            print(f'  [{label}] 등록된 계측 스텝 없음 — 조회 생략')
        return pd.DataFrame()

    sql  = STEP_SQL[kind]
    lots_all = [str(v).upper() for v in (cond.get('lot_cd_list') or []) if v]

    # ★ rep/def 는 dt 필터가 없고 mt 로만 기간을 좁힌다.
    #   그래서 청크로 쪼갤 이유가 없다 — 오히려 청크마다 mt 범위가
    #   달라져 첫 청크가 202606~202607 이면 8월 데이터가 빠진다.
    #   전체 기간의 시작·끝 월을 한 번에 준다.
    mt_s, mt_e = _month_range(days, date_from=date_from, date_to=date_to)

    total = sum(len(st.get('lot_cds') or lots_all) for st in steps) or 1
    done, dfs = 0, []

    for st in steps:
        params = [p for p in st.get('params') or [] if p]
        # 스텝에 LOT_CD 가 지정돼 있으면 그것만, 없으면 공정의 전체 device
        lots = [l for l in (st.get('lot_cds') or lots_all) if l]
        if not params or not lots:
            continue
        p_in = ", ".join("'" + str(p).replace("'", "''") + "'" for p in params)

        for lot_cd in lots:
            query = sql.format(mt_s=mt_s, mt_e=mt_e, lot_cd=lot_cd,
                               step=st['step_id'], params=p_in)

            # 첫 쿼리는 전문을 남긴다 — 조건이 의도대로 들어갔는지 확인용
            if done == 0 and VERBOSE:
                print(f'\n─── [{label}] 실행 쿼리 ' + '─' * 40)
                print(query.strip())
                print('─' * 58)

            try:
                d = run_query(lake, query)
            except Exception as e:
                _log_query(label, query, error=e)
                # ★ 여기서 raise 하면 공정 전체가 멈춰 앞서 받은 것까지
                #   버려진다. 이 LOT_CD 만 건너뛰고 계속한다.
                print(f'\n[{label}] {lot_cd} 조회 실패 — 아래 쿼리를 확인하세요')
                print(query.strip())
                note_fail(label, f"{st['step_id']}/{lot_cd}", e)
                done += 1
                continue

            _log_query(label, query, rows=(0 if d is None else len(d)))
            if d is not None and not d.empty:
                dfs.append(d)

            done += 1
            n = sum(len(x) for x in dfs)
            if on_progress:
                on_progress(done, total,
                            f"{label} {st['step_id']} {lot_cd} · {n:,}행")
            if VERBOSE:
                print(f"  [{label}] {done}/{total} {st['step_id']} "
                      f"{lot_cd} mt {mt_s}~{mt_e} 누적 {n:,}행")

    if not dfs:
        return pd.DataFrame()

    out = pd.concat(dfs, ignore_index=True).drop_duplicates()
    out.columns = out.columns.str.lower()
    out = out.rename(columns=STEP_RENAME[kind])

    # SRC 와 같은 규칙으로 병합 키를 만든다 (alias_lot_id 는 이미 7자리)
    out['substrate_id'] = (out['alias_lot_id'].astype(str) + '.'
                           + out['wf_id'].astype(str))

    # ★ 컬럼 이름은 '기준정보만' 으로 정한다.
    #   Lake 의 oper_det_desc 를 섞어 쓰면 Defect(그 컬럼이 없음)과
    #   규칙이 갈려 셋업 화면의 '생성 컬럼' 미리보기와 어긋난다.
    desc_map = {str(s['step_id']).upper(): (s.get('step_desc') or '').strip()
                for s in steps}
    out['step_desc'] = out['step_id'].astype(str).str.upper().map(
        lambda k: desc_map.get(k, ''))
    return out



def preview_step_sql(df_info, oper_id, kind, days=30,
                     date_from=None, date_to=None, limit=6):
    """
    실제로 던질 쿼리를 만들어서 돌려준다 (실행은 하지 않음).

    ★ Lake 에 붙여넣어 그대로 확인할 수 있게 하기 위한 것.
      조회가 실패했을 때 '어떤 조건이 들어갔나' 를 눈으로 보는 게
      가장 빠르다.
    """
    cond  = get_oper_cond(df_info, oper_id)
    label = STEP_LABEL[kind]
    steps = cond.get('resp_steps' if kind == 'resp' else 'def_steps') or []
    if not steps:
        return []

    sql = STEP_SQL[kind]
    lots_all = [str(v).upper() for v in (cond.get('lot_cd_list') or []) if v]
    mt_s, mt_e = _month_range(days, date_from=date_from, date_to=date_to)

    out = []
    for st in steps:
        params = [p for p in st.get('params') or [] if p]
        lots = [l for l in (st.get('lot_cds') or lots_all) if l]
        if not params or not lots:
            continue
        p_in = ", ".join("'" + str(p).replace("'", "''") + "'" for p in params)
        for lot_cd in lots:
            out.append({
                'label': label, 'step_id': st['step_id'],
                'lot_cd': lot_cd, 'mt': f"between '{mt_s}' and '{mt_e}'",
                'query': sql.format(mt_s=mt_s, mt_e=mt_e, lot_cd=lot_cd,
                                    step=st['step_id'],
                                    params=p_in).strip(),
            })
            if len(out) >= limit:
                return out
    return out


def pivot_steps(df, kind):
    """
    계측 long → wide.
      값 컬럼 : RESP_<STEP>_<PARAM> / DEF_<STEP>_<PARAM>
      시각    : RESP_DATE / DEF_DATE (그 웨이퍼의 마지막 계측 시각)

    ★ 컬럼 이름은 config_service.step_column 이 정한다 —
      화면 미리보기·점검과 규칙이 갈리면 안 되므로 반드시 그 함수를 쓴다.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        from . import config_service as cfg
        colname = cfg.step_column
    except Exception:
        def colname(k, step, param):
            pre = 'DEF' if k == 'def' else 'RESP'
            slug = lambda v: re.sub(r'_+', '_', re.sub(
                r'[^0-9A-Za-z_]+', '_', str(v or '').upper())).strip('_')
            st, pm = slug(step), slug(param)
            return f'{pre}_{st}_{pm}' if st else f'{pre}_{pm}'

    d = df.copy()
    d['__col'] = [colname(kind, (sd or si), p)
                  for sd, si, p in zip(d.get('step_desc', ''),
                                       d['step_id'], d['param'])]
    d = d[d['__col'] != '']
    if d.empty:
        return pd.DataFrame()

    d['value'] = pd.to_numeric(d['value'], errors='coerce')
    wide = d.pivot_table(index='substrate_id', columns='__col',
                         values='value', aggfunc='last').reset_index()
    wide.columns.name = None

    tcol = 'RESP_DATE' if kind == 'resp' else 'DEF_DATE'
    tm = (d.groupby('substrate_id', as_index=False)['end_tm'].max()
            .rename(columns={'end_tm': tcol}))
    return wide.merge(tm, on='substrate_id', how='left')


def merge_steps(base, df_step, kind):
    """
    병합 테이블에 계측 결과를 붙인다.

    ★ 항상 left join 이다 — 계측은 표본이라 측정 안 된 웨이퍼가 정상이다.
      inner 로 붙이면 계측 없는 웨이퍼가 통째로 사라진다.
    ★ 비어 있으면 base 를 그대로 돌려준다 (등록이 없거나 조회 결과가 없을 때).
    """
    if base is None or base.empty:
        return base
    wide = pivot_steps(df_step, kind)
    if wide is None or wide.empty:
        return base

    # ★ finalize_df 가 컬럼을 전부 대문자로 바꾸므로 SUBSTRATE_ID 로 온다.
    #   반면 pivot_steps 는 소문자 substrate_id 로 만든다.
    #   어느 쪽이든 찾아서 맞춰 붙인다 — 대소문자 하나 때문에
    #   조회는 다 해놓고 못 붙이는 일이 없게.
    key = next((c for c in base.columns if c.lower() == 'substrate_id'), None)
    if key is None:
        print(f'[{kind}] SUBSTRATE_ID 컬럼이 없어 계측을 붙이지 못했습니다 — '
              f'base 컬럼: {list(base.columns)[:12]}')
        return base

    if key != 'substrate_id':
        wide = wide.rename(columns={'substrate_id': key})

    # 값 컬럼도 base 의 표기(대문자)에 맞춘다
    if key.isupper():
        wide.columns = [c if c == key else str(c).upper() for c in wide.columns]

    # ★ 이미 있는 컬럼과 겹치면 pandas 가 _x/_y 를 붙인다.
    #   본공정과 계측 스텝이 같은 파라미터 이름을 쓰면 그렇게 된다.
    #   덮어쓰지 말고 건너뛰면서 이유를 알려 준다 — 어느 쪽 값인지
    #   모르는 컬럼이 생기는 것보다 낫다.
    clash = [c for c in wide.columns if c != key and c in base.columns]
    if clash:
        print(f'  [{kind}] ★ 컬럼 이름이 겹쳐 {len(clash)}개를 건너뜁니다: '
              f'{", ".join(map(str, clash[:6]))}'
              f'{" ..." if len(clash) > 6 else ""}')
        print(f'     본공정에 이미 같은 이름이 있습니다 — 기준정보에서 '
              f'스텝 이름을 다르게 지으면 구분됩니다')
        wide = wide.drop(columns=clash)
        if len(wide.columns) <= 1:
            return base

    before = set(base.columns)
    out = base.merge(wide, on=key, how='left')
    added = [c for c in out.columns if c not in before]
    if VERBOSE:
        n_hit = int(out[added[0]].notna().sum()) if added else 0
        print(f'  [{kind}] 컬럼 {len(added)}개 추가 · 값 있는 웨이퍼 {n_hit:,}장')
    return out


# ══════════════════════════════════════════════════════════
# 5-A. SRC 정리 (pivot)
# ══════════════════════════════════════════════════════════
# 웨이퍼 1장당 하나인 값 (pivot 후에도 유지)
SRC_META_COLS = [
    'lot_id', 'wf_id', 'substrate_id', 'main_eqp_id',
    'oper_id', 'oper_det_desc', 'end_tm',
    'pre_eqp_id', 'pre_eqp_ch', 'pre_oper_time',
]


def pivot_src(df_src):
    """
    SRC long → wide.
      substrate_id 기준으로 param_nm 을 컬럼화.
      end_tm 이 param 별로 미세하게 다를 수 있어 index 에 넣지 않고,
      메타는 별도 추출 후 병합한다.
    """
    if df_src is None or df_src.empty:
        return pd.DataFrame()

    df = df_src.copy()
    df.columns = df.columns.str.lower()

    wide = df.pivot_table(
        index='substrate_id', columns='param_nm',
        values='thk_value', aggfunc='first',
    ).reset_index()
    wide.columns.name = None

    meta_cols = [c for c in SRC_META_COLS if c in df.columns]
    meta = (df.sort_values('end_tm')
              .groupby('substrate_id', as_index=False)[meta_cols]
              .first())

    # ── rework 지표 ──────────────────────────────────────
    #   같은 lot_id·wf_id·param_nm 에 측정이 2건 이상이면 재작업이 있었다.
    #   ★ 값은 rework 전(SRC_PICK='first')을 쓰되, '재작업이 있었다' 는
    #     사실은 남긴다. 그걸 버리면 재작업이 늘어나는 신호를 못 본다.
    if 'meas_n' in df.columns:
        rw = (df.groupby('substrate_id', as_index=False)['meas_n'].max()
                .rename(columns={'meas_n': 'rework_n'}))
        meta = meta.merge(rw, on='substrate_id', how='left')

    return meta.merge(wide, on='substrate_id', how='left')


# ══════════════════════════════════════════════════════════
# 5-B. APC 정리
#    APC 는 웨이퍼 × input_name × item_name 으로 행이 갈라져 있다.
#    이를 웨이퍼당 1행으로 펼친다.
#
#      {input_name}          APC 파라미터 값 (input_value)
#      {input_name}_OFFSET   보정량 = seq_offset + idle_offset
#      {input_name}_FORMULA  적용된 수식 (FORMULA 의 item_value)
#      IDLE                  idle_1~4 / layer_1~4 / ''
# ══════════════════════════════════════════════════════════

# idle 순번을 몇 번째 웨이퍼까지 표기할지
IDLE_RANK_MAX = 4

# lot 완전성 검사 — 조회된 웨이퍼 수가 qty 와 다르면 그 lot 을 버린다.
#   일부만 조회된 lot 은 순번(rank)이 어긋나므로 제외하는 편이 안전하다.
CHECK_LOT_COMPLETE = True


def _wf_id_from_substrate(sr):
    """substrate_id (= alias_lot_id.wf_id) 에서 wf_id 추출"""
    return sr.astype(str).str.rsplit('.', n=1).str[-1].str.strip()


def _apc_idle(df):
    """
    IDLE 라벨 계산.

      IDLE_TIME 의 item_value 에 Idle / Layer 가 들어온다.
      순번은 (lot, 장비, 레시피) 안에서 wf_id 순으로 매긴다.
      → 챔버가 나뉘어 병렬로 도는 경우에도 각 흐름의 첫 장이 1번이 된다.
        (lot 전체를 시간순으로 줄 세우면 늦게 시작한 챔버의 첫 장이 밀린다)

    ★ 수정
      · drop_duplicates 전에 request_dtts 로 정렬한다.
        정렬 없이 keep='first' 를 쓰면 한 웨이퍼에 IDLE_TIME 행이 여러 개일 때
        어느 값이 남을지가 실행마다 달라져 라벨이 들쭉날쭉해진다.
      · rank 1 인데 라벨을 못 받은 웨이퍼 수를 진단 로그로 남긴다
        (idle_1 만 비어 보이는 현상의 원인을 좁히기 위함)
    """
    idle_rows = df[df['item_name'] == 'IDLE_TIME'].copy()
    if idle_rows.empty:
        if VERBOSE:
            print('  [IDLE] IDLE_TIME 행이 없음 → 라벨 없음')
        return pd.DataFrame(columns=['substrate_id', 'idle'])

    key = [c for c in ['lot_id', 'eqp_id', 'recipe_id'] if c in idle_rows.columns]

    # 웨이퍼당 1행 — 가장 이른 조회 기록을 남긴다 (결정적으로)
    if 'request_dtts' in idle_rows.columns:
        idle_rows = idle_rows.sort_values('request_dtts')
    idle_rows = idle_rows.drop_duplicates(subset=['substrate_id'], keep='first')

    idle_rows['wf_no'] = pd.to_numeric(idle_rows['wf_id'], errors='coerce')
    n_bad_wf = int(idle_rows['wf_no'].isna().sum())
    idle_rows = idle_rows[idle_rows['wf_no'].notna()]
    if VERBOSE and n_bad_wf:
        print(f'  [IDLE] wf_id 를 숫자로 못 읽어 제외 {n_bad_wf}장')
    if idle_rows.empty:
        return pd.DataFrame(columns=['substrate_id', 'idle'])

    # key 가 비어 있으면 groupby([]) 가 예외 → 전체 기준으로 순번
    if key:
        idle_rows['rank'] = idle_rows.groupby(key)['wf_no'].rank(method='first')
    else:
        idle_rows['rank'] = idle_rows['wf_no'].rank(method='first')

    val  = idle_rows['item_value'].astype(str).str.strip().str.lower()
    rank = idle_rows['rank'].astype(int)

    is_idle  = val.str.startswith('idle')
    is_layer = val.str.startswith('layer')
    base     = pd.Series('', index=idle_rows.index)
    base[is_idle]  = 'idle'
    base[is_layer] = 'layer'

    hit   = (is_idle | is_layer) & (rank <= IDLE_RANK_MAX)
    label = pd.Series('', index=idle_rows.index)
    label[hit] = base[hit] + '_' + rank[hit].astype(str)

    idle_rows['idle'] = label

    # ── 진단 ─────────────────────────────────────────────
    # rank 1 인데 라벨이 안 붙었다면, 그 웨이퍼의 item_value 가
    # Idle/Layer 로 시작하지 않는다는 뜻이다 (= APC 원본이 그렇게 옴).
    if VERBOSE:
        r1     = rank == 1
        r1_tot = int(r1.sum())
        r1_lab = int((r1 & hit).sum())
        print(f'  [IDLE] APC 라벨 분포: {_idle_dist(idle_rows["idle"])}')
        print(f'  [IDLE] rank1 웨이퍼 {r1_tot}장 중 라벨 {r1_lab}장')
        if r1_tot and r1_lab < r1_tot:
            miss_val = val[r1 & ~hit].value_counts().head(5)
            print(f'  [IDLE] ★ rank1 인데 라벨 없음 {r1_tot - r1_lab}장 — '
                  f'그 웨이퍼들의 IDLE_TIME item_value 상위: {dict(miss_val)}')

    return idle_rows[['substrate_id', 'idle']]


def _apc_offset(df):
    """
    보정량 = PROCESS_OFFSET_WAFER_SEQ + PROCESS_OFFSET_MES_IDLE_FLAG_IDLE
    (input_name 별로 따로 존재하므로 substrate_id + input_name 기준)
    """
    def pick(item, name):
        d = df[df['item_name'] == item][['substrate_id', 'input_name', 'item_value']]
        return d.rename(columns={'item_value': name}).drop_duplicates(
            subset=['substrate_id', 'input_name'], keep='first')

    seq  = pick('PROCESS_OFFSET_WAFER_SEQ', 'seq_offset')
    idle = pick('PROCESS_OFFSET_MES_IDLE_FLAG_IDLE', 'idle_offset')

    if seq.empty and idle.empty:
        return pd.DataFrame(columns=['substrate_id', 'input_name', 'OFFSET'])

    off = seq.merge(idle, on=['substrate_id', 'input_name'], how='outer')
    for c in ('seq_offset', 'idle_offset'):
        off[c] = pd.to_numeric(off.get(c), errors='coerce').fillna(0)
    off['OFFSET'] = off['seq_offset'] + off['idle_offset']
    return off[['substrate_id', 'input_name', 'OFFSET']]


def _pivot_by_input(df, values, suffix=''):
    """input_name 을 컬럼으로 펼친다 (웨이퍼당 1행)"""
    d = df[df['input_name'].notna() & df[values].notna()]
    if d.empty:
        return pd.DataFrame(columns=['substrate_id'])

    wide = pd.pivot_table(d, index='substrate_id', columns='input_name',
                          values=values, aggfunc='first').reset_index()
    wide.columns.name = None
    if suffix:
        wide = wide.rename(columns={c: f'{c}{suffix}'
                                    for c in wide.columns if c != 'substrate_id'})
    return wide


def prepare_apc(df_apc):
    """APC → 웨이퍼당 1행 (파라미터 · 보정량 · 수식 · IDLE)"""
    if df_apc is None or df_apc.empty:
        return pd.DataFrame()

    df = df_apc.copy()
    df.columns = df.columns.str.lower()

    # ── wf_id 추출 + 비정상 행 제거 ───────────────────────
    n0 = len(df)
    df['wf_id'] = _wf_id_from_substrate(df['substrate_id'])
    df = df[(df['wf_id'] != '-') & (df['lot_id'].astype(str) != '-')]
    if VERBOSE and len(df) < n0:
        print(f'  [APC] 비정상 행 제거 {n0 - len(df):,}행 ({n0:,} → {len(df):,})')
    if df.empty:
        return pd.DataFrame()

    # ── lot 완전성 (조회 웨이퍼 수 == qty) ────────────────
    #   전 lot 이 제외되면 검사 기준 자체가 틀린 것이므로 경고 후 통과시킨다.
    if CHECK_LOT_COMPLETE and 'qty' in df.columns:
        cnt  = df.groupby('lot_id')['substrate_id'].transform('nunique')
        qty  = pd.to_numeric(df['qty'], errors='coerce')
        keep = cnt == qty

        n_lots = df['lot_id'].nunique()
        n_drop = df.loc[~keep, 'lot_id'].nunique()
        if n_drop and VERBOSE:
            print(f'  [APC] 완전성 검사: lot {n_drop}/{n_lots}개 제외 '
                  f'(웨이퍼 수 ≠ qty)')

        if n_drop >= n_lots:
            print(f'  [APC] ★ 완전성 검사에서 전 lot({n_lots}개) 제외 — '
                  f'검사 없이 진행. qty 값/형식을 확인할 것 '
                  f'(예시 qty: {df["qty"].head(3).tolist()}, '
                  f'예시 웨이퍼수: {cnt.head(3).tolist()})')
        else:
            df = df[keep]
            if df.empty:
                return pd.DataFrame()

    # ── 웨이퍼 메타 (첫 행) ───────────────────────────────
    meta_cols = [c for c in ['substrate_id', 'lot_id', 'wf_id', 'eqp_id',
                             'process_id', 'recipe_id', 'operation_id',
                             'qty', 'request_dtts']
                 if c in df.columns]
    out = (df.sort_values('request_dtts')
             .groupby('substrate_id', as_index=False)[meta_cols]
             .first())

    # ── IDLE ─────────────────────────────────────────────
    out = out.merge(_apc_idle(df), on='substrate_id', how='left')
    out['idle'] = out['idle'].fillna('')

    # ── 파라미터 / 보정량 / 수식 ──────────────────────────
    df['input_value'] = pd.to_numeric(df['input_value'], errors='coerce')
    out = out.merge(_pivot_by_input(df, 'input_value'),
                    on='substrate_id', how='left')

    off = _apc_offset(df)
    if not off.empty:
        wide = pd.pivot_table(off, index='substrate_id', columns='input_name',
                              values='OFFSET', aggfunc='first').reset_index()
        wide.columns.name = None
        wide = wide.rename(columns={c: f'{c}_OFFSET'
                                    for c in wide.columns if c != 'substrate_id'})
        out = out.merge(wide, on='substrate_id', how='left')

    formula = df[df['item_name'] == 'FORMULA']
    if not formula.empty:
        out = out.merge(_pivot_by_input(formula, 'item_value', '_FORMULA'),
                        on='substrate_id', how='left')

    return out


# ══════════════════════════════════════════════════════════
# 6. 머지
#    SRC ↔ APC : substrate_id (SRC 는 alias_lot_id 기반 — 매칭률 더 높음)
#    ↔ MES     : lot_id (lot 당 1행으로 줄인 뒤 붙임)
# ══════════════════════════════════════════════════════════
def merge_sources(df_src_wide, df_apc_prep, df_mes=None):
    if df_src_wide is None or df_src_wide.empty:
        return pd.DataFrame()

    df = df_src_wide.copy()

    if df_apc_prep is not None and not df_apc_prep.empty:
        # SRC 가 이미 가진 식별 컬럼은 APC 쪽을 버린다.
        # (남겨두면 merge 가 wf_id_x / wf_id_y 로 갈라 저장에서 실패한다)
        apc = df_apc_prep.drop(
            columns=[c for c in ['lot_id', 'wf_id'] if c in df_apc_prep.columns])

        # ★ 이름이 겹치는 APC 컬럼은 머지 전에 버린다.
        #   APC 의 input_name 을 펼친 컬럼과 SRC 의 param_nm 컬럼은 이름이 겹치고
        #   값도 사실상 같다. 그대로 두면 suffixes 규칙에 따라 X_APC 가 만들어져
        #   같은 값을 가진 컬럼이 두 벌 저장되고 PARAMETER 목록도 두 배로 늘어난다.
        dup = [c for c in apc.columns
               if c != 'substrate_id' and c in df.columns]
        if dup:
            apc = apc.drop(columns=dup)
            if VERBOSE:
                print(f'  [merge] SRC 와 중복된 APC 컬럼 {len(dup)}개 제외: '
                      f'{", ".join(dup[:8])}{" ..." if len(dup) > 8 else ""}')

        # inner join 사전 점검 — 키 포맷이 어긋나면 에러 없이 0행이 된다
        if VERBOSE:
            ws  = set(df['substrate_id'].astype(str).str.strip())
            as_ = set(apc['substrate_id'].astype(str).str.strip())
            inter = len(ws & as_)
            print(f'  [merge] substrate_id 교집합 {inter:,} '
                  f'(SRC {len(ws):,} / APC {len(as_):,})')
            if inter == 0:
                print(f'  [merge] ★ 교집합 0 — 키 포맷 불일치 의심')
                print(f'          SRC 예시: {sorted(ws)[:3]}')
                print(f'          APC 예시: {sorted(as_)[:3]}')

        # 위에서 중복을 걸렀으므로 _APC 는 원칙적으로 생기지 않는다.
        # suffixes 는 예상 못 한 충돌에 대비한 안전장치로만 남겨둔다.
        # ★ inner 는 rework 를 걸러 주지만, APC 에 아직 안 들어온
        #   최신 웨이퍼도 함께 버린다. APC(R2R 이력)는 측정보다 늦게
        #   쌓이는 일이 있어 그 구간이 통째로 잘린다.
        before_n = len(df)
        before_max = None
        try:
            if 'end_tm' in df.columns:
                before_max = pd.to_datetime(df['end_tm'],
                                            errors='coerce').max()
        except Exception:
            pass

        df = df.merge(apc, on='substrate_id', how=SRC_APC_JOIN,
                      suffixes=('', '_APC'))

        if VERBOSE:
            if SRC_APC_JOIN == 'inner':
                print(f'  [merge] SRC∩APC(inner) → {len(df):,}행 '
                      f'(SRC {before_n:,}행에서 {before_n - len(df):,}행 제외)')
            else:
                miss = 0
                try:
                    kc = next((c for c in apc.columns
                               if c != 'substrate_id'), None)
                    if kc:
                        miss = int(df[kc].isna().sum())
                except Exception:
                    pass
                print(f'  [merge] SRC+APC(left) → {len(df):,}행 '
                      f'(APC 값 없는 웨이퍼 {miss:,}행 — 값만 비고 행은 남음)')

            # 잘린 구간이 최신 쪽이면 알려 준다 — rework 제외와 구분해야 한다
            try:
                after_max = (pd.to_datetime(df['end_tm'], errors='coerce').max()
                             if 'end_tm' in df.columns else None)
                if before_max is not None and after_max is not None \
                        and pd.notna(before_max) and pd.notna(after_max) \
                        and after_max < before_max:
                    gap_h = (before_max - after_max).total_seconds() / 3600
                    print(f'  [merge] ★ 최신 시각이 {gap_h:.1f}시간 뒤로 밀렸습니다: '
                          f'{str(before_max)[:19]} → {str(after_max)[:19]}')
                    print(f'          APC(R2R)에 아직 안 들어온 웨이퍼가 '
                          f'inner join 에서 빠진 것입니다.')
            except Exception:
                pass
            if 'idle' in df.columns:
                # ★ APC 단계 분포와 비교하면 idle_1 이 어디서 사라졌는지 알 수 있다.
                #   APC 에는 있는데 여기서 줄었다면 → 그 웨이퍼가 SRC(측정)에 없는 것
                print(f'  [merge] 머지 후 IDLE 분포: {_idle_dist(df["idle"])}')

    if df_mes is not None and not df_mes.empty:
        mes = df_mes.copy()
        mes.columns = mes.columns.str.lower()
        keep = [c for c in ['before_info', 'eqp_ch'] if c in mes.columns]
        mes = (mes.sort_values('event_tm')
                  .groupby('lot_id', as_index=False)[keep]
                  .first())                                   # lot 당 1행 (행 뻥튀기 방지)
        df = df.merge(mes, on='lot_id', how='left')
        if VERBOSE:
            n_hit = df['before_info'].notna().sum() if 'before_info' in df.columns else 0
            print(f'  [merge] +MES(left) → {len(df):,}행 (LC 매칭 {n_hit:,}행)')

    return df


# ══════════════════════════════════════════════════════════
# 7. 파생 — OPTA 챔버
# ══════════════════════════════════════════════════════════
# OPTA 챔버 — param_nm 표기(_P1/_P2) → 실제 챔버명
OPTA_CH_MAP = {'P1': 'P3', 'P2': 'P4'}

# param 으로 판정 불가한 공정 (챔버 고정)
# ★ 실제 oper_id 입력 필요. 추후 구닥스 컬럼으로 옮기는 것 권장
OPTA_FIXED_CH = {
    # 'OPER_ID값': 'P4',
}


def derive_opta_chamber(df_src, oper_id):
    """substrate_id 별 OPTA 챔버 판정"""
    d = df_src.copy()
    d.columns = d.columns.str.lower()

    fixed = OPTA_FIXED_CH.get(str(oper_id))
    if fixed:
        out = d[['substrate_id']].drop_duplicates().copy()
        out['eqp_ch_opta'] = fixed
        return out

    # thk 등 P 표기 없는 param 은 NaN 으로 빠져 자동 제외됨
    d['ch'] = d['param_nm'].str.extract(r'_(P\d)(?:_|$)')
    d['ch'] = d['ch'].map(OPTA_CH_MAP).fillna(d['ch'])
    return (d.dropna(subset=['ch'])
              .groupby('substrate_id', as_index=False)['ch']
              .first()
              .rename(columns={'ch': 'eqp_ch_opta'}))


# ══════════════════════════════════════════════════════════
# 8. 저장 직전 정리
# ══════════════════════════════════════════════════════════
RENAME_MAP = {
    'end_tm':      'DATE',
    'main_eqp_id': 'EQP_ID',
    'eqp_ch':      'EQP_CH_ID',
    'before_info': 'PRE_LAYER',
    'oper_id':     'OPERATION_ID',
    'idle':        'IDLE',
}

DROP_COLS = [
    'r2r_rank', 'rn', 'rank', 'recipe_id_info', 'before_recipe_id',
    'pre_oper_time', 'request_dtts', 'event_tm', 'oper_det_desc',
    'operation_id', 'eqp_id',          # APC 쪽 중복 (SRC 값을 사용)
]


def _join_eqp_ch(eqp_sr, ch_sr):
    """
    '장비ID_챔버' 결합.
      챔버가 비면 빈 문자열, 장비가 비면 챔버만 남긴다.
      (챔버 값만 덩그러니 남으면 어느 장비의 챔버인지 구분이 안 된다)
    """
    eqps = eqp_sr.fillna('').astype(str).str.strip()
    chs  = ch_sr.fillna('').astype(str).str.strip()
    out = []
    for e, c in zip(eqps, chs):
        if c and e:
            out.append(f'{e}_{c}')
        elif c:
            out.append(c)
        else:
            out.append('')
    return pd.Series(out, index=ch_sr.index)


def finalize_df(df, cond, df_src=None):
    """
    머지+파생 결과를 저장 형태로 정리.
      - OPTA 면 param 기반 챔버 채우기 (df_src 필요)
      - EQP_CH_ID  = 장비ID_챔버
      - PRE_EQP_CH = 사전공정장비ID_챔버   ★ 이번 수정
      - 컬럼명 통일 / 기준정보 추가 / 불필요 컬럼 제거 / 대문자화
    """
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = df.columns.str.lower()

    # OPTA 챔버 보강
    if str(cond.get('eq_model', '')).upper() == 'OPTA' and df_src is not None:
        ch = derive_opta_chamber(df_src, cond['oper_id'])
        df = df.merge(ch, on='substrate_id', how='left')
        if 'eqp_ch' not in df.columns:
            df['eqp_ch'] = pd.NA
        if 'eqp_ch_opta' in df.columns:
            df['eqp_ch'] = df['eqp_ch'].replace('', pd.NA).fillna(df['eqp_ch_opta'])
            df = df.drop(columns=['eqp_ch_opta'])

    # EQP_CH_ID = 장비ID_챔버
    if 'eqp_ch' in df.columns and 'main_eqp_id' in df.columns:
        df['eqp_ch'] = _join_eqp_ch(df['main_eqp_id'], df['eqp_ch'])

    # ★ PRE_EQP_CH = 사전공정장비ID_챔버
    #   SRC 의 b.module_id 는 '2', '3' 처럼 번호만 온다.
    #   그대로 두면 어느 장비의 챔버인지 알 수 없으므로 장비ID 를 앞에 붙인다.
    #   ※ 화면은 PostgreSQL 을 읽으므로, 이 결합은 '다음 적재분'부터 반영된다.
    #     이미 저장된 옛 행은 재적재해야 형식이 바뀐다.
    if 'pre_eqp_ch' in df.columns:
        if 'pre_eqp_id' in df.columns:
            before = df['pre_eqp_ch'].fillna('').astype(str).str.strip()
            ids    = df['pre_eqp_id'].fillna('').astype(str).str.strip()
            df['pre_eqp_ch'] = _join_eqp_ch(df['pre_eqp_id'], df['pre_eqp_ch'])

            if VERBOSE:
                n_both   = int(((ids != '') & (before != '')).sum())
                n_chonly = int(((ids == '') & (before != '')).sum())
                print(f'  [finalize] PRE_EQP_CH 결합: 장비+챔버 {n_both:,}행 / '
                      f'챔버만(사전공정 장비 미매칭) {n_chonly:,}행')
                if n_chonly and not n_both:
                    # 코드는 돌았는데 붙일 장비 ID 가 없는 상태.
                    # SRC 쿼리의 APC wafer-history 조인(b) 이 안 붙은 것이므로
                    # pre_oper_id / module_id 조건을 확인해야 한다.
                    print('  [finalize] ★ PRE_EQP_ID 가 전부 비어 있어 '
                          '챔버만 남았습니다 — SRC 의 wafer-history 조인(b) 확인 필요 '
                          '(pre_oper_id 접두 조건, module_id 필터)')
                sample = [v for v in df['pre_eqp_ch'].unique()[:3] if v]
                print(f'  [finalize] PRE_EQP_CH 예시: {sample}')
        else:
            print('  [finalize] ★ pre_eqp_id 컬럼이 없어 PRE_EQP_CH 를 결합하지 못했습니다 '
                  '— SRC_META_COLS 와 SRC 쿼리의 b.eqp_id 확인 필요')

    df = df.drop(columns=[c for c in DROP_COLS if c in df.columns])

    # ★ 혹시 남아 있는 _APC 중복 컬럼 제거 (방어)
    #   짝이 되는 원본 컬럼이 있을 때만 지운다 —
    #   이름이 우연히 _APC 로 끝나는 실제 파라미터를 지우지 않기 위함.
    apc_dup = [c for c in df.columns
               if c.endswith('_apc') and c[:-4] in df.columns]
    if apc_dup:
        df = df.drop(columns=apc_dup)
        if VERBOSE:
            print(f'  [finalize] _APC 중복 컬럼 {len(apc_dup)}개 제거: '
                  f'{", ".join(apc_dup[:8])}{" ..." if len(apc_dup) > 8 else ""}')

    df = df.rename(columns={k: v for k, v in RENAME_MAP.items() if k in df.columns})

    if 'lot_id' in df.columns:
        df['LOT_CD'] = df['lot_id'].astype(str).str[:3]
    df['EQP_MODEL'] = cond.get('eq_model', '')

    df.columns = [c.upper() for c in df.columns]

    if 'DATE' in df.columns:
        df['DATE'] = pd.to_datetime(df['DATE'], errors='coerce')

    # 중복 컬럼 방어 (앞의 것만 유지)
    df = df.loc[:, ~df.columns.duplicated()]

    return df


# ══════════════════════════════════════════════════════════
# 9. PostgreSQL 저장
#    pandas to_sql 은 버전 조합에 따라 실패 → psycopg2 execute_values 사용
# ══════════════════════════════════════════════════════════
# 값이 숫자처럼 보여도 반드시 텍스트로 저장할 컬럼 (식별자류)
#   LOT_CD 5E2 / 5E9 가 과학적표기법 숫자(500.0 / 5e9)로 뭉개지는 것을 막는다.
#   계산 대상이 아니므로 전부 텍스트가 맞다.
TEXT_COLS = {
    'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID', 'EQP_MODEL',
    'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID', 'WF_ID',
    'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH',
    'FORMULA', 'IDLE_FLAG',            # APC item_value 계열 (범례·요인용)
}
TIME_COLS = {'DATE'}

# information_schema 의 숫자 타입명 (넓히기 판정용)
_PG_NUMERIC_TYPES = {'double precision', 'integer', 'bigint', 'numeric',
                     'real', 'smallint'}


def _pg_type_from_series(s):
    """
    실제 데이터로 PG 타입 판정.
    하드코딩 목록은 컬럼이 늘 때마다 누락되므로 값을 보고 결정한다.

    ★ 값이 하나도 없는 컬럼은 DOUBLE PRECISION 으로 본다.
      여기 오는 컬럼은 식별자류(TEXT_COLS)가 이미 걸러진 '측정값' 뿐이다.
      이번 적재에서만 비어 있다고 VARCHAR 로 만들면,
        · 아래 타입 넓히기가 멀쩡한 숫자 컬럼을 VARCHAR 로 바꾸고
        · 화면에서 그 컬럼에 AVG/CORR 를 걸 때 500 이 난다
    """
    if pd.api.types.is_datetime64_any_dtype(s):
        return 'TIMESTAMP'
    if pd.api.types.is_numeric_dtype(s):
        return 'DOUBLE PRECISION'

    nonnull = s.dropna()
    if len(nonnull) == 0:
        return 'DOUBLE PRECISION'          # ★ 전부 NULL → 숫자로 유지
    if pd.to_numeric(nonnull, errors='coerce').notna().all():
        return 'DOUBLE PRECISION'
    return 'VARCHAR(200)'


def _table_name(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def drop_analysis_table(oper_id):
    """컬럼/타입이 바뀌었을 때 테이블을 지운다 (재적재 전 1회)"""
    table = _table_name(oper_id)
    with connections['analysis_db'].cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS {table}')
    print(f"[{oper_id}] {table} DROP 완료")


def repair_numeric_columns(oper_id):
    """
    과거 적재에서 VARCHAR 로 굳어버린 측정값 컬럼을 숫자로 되돌린다.

    값이 전부 비어 있던 컬럼이 VARCHAR 로 만들어지면 화면에서
    AVG/CORR 를 걸 때 500 이 나므로, 숫자로 바꿀 수 있는 것만 되돌린다.
    (한 번만 돌리면 되고, 실패하는 컬럼은 실제로 텍스트가 들어 있는 것)
    """
    table = _table_name(oper_id)
    fixed, skipped = [], []
    with connections['analysis_db'].cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = %s
        """, [table])
        cols = cur.fetchall()

        for name, dtype in cols:
            up = name.upper()
            if up in TEXT_COLS or up in TIME_COLS or up == 'ID':
                continue
            if dtype.lower() not in ('character varying', 'text'):
                continue
            try:
                cur.execute(
                    f'ALTER TABLE {table} ALTER COLUMN "{name}" '
                    f'TYPE DOUBLE PRECISION '
                    f'USING NULLIF("{name}", \'\')::double precision')
                fixed.append(name)
            except Exception as e:
                skipped.append(f'{name}({e.__class__.__name__})')

    print(f'[{oper_id}] 숫자 복구 {len(fixed)}개: {", ".join(fixed) or "-"}')
    if skipped:
        print(f'[{oper_id}] 건너뜀(실제 텍스트) {len(skipped)}개: '
              f'{", ".join(skipped[:8])}')
    return fixed


def repair_pre_eqp_ch(oper_id=None):
    """
    이미 저장된 PRE_EQP_CH 를 '사전공정장비ID_챔버' 형태로 일괄 보정한다.

    재적재를 기다리지 않고 기존 행을 바로 고친다.
    여러 번 실행해도 안전하다 — 이미 '_' 가 들어간(결합된) 값은 건드리지 않는다.

      oper_id 를 주면 그 공정만, 생략하면 cmp_analysis_* 전체.

    실행 예)
      from equipment import analysis_service as svc
      svc.repair_pre_eqp_ch()            # 전 공정
      svc.repair_pre_eqp_ch('공정ID')     # 한 공정만

    로그에 공정별로 아래를 찍는다.
      · 보정        : 이번에 장비ID 를 붙인 행 수
      · 이미결합    : 앞서 결합돼 있던 행 수
      · 장비ID없음  : 챔버는 있는데 PRE_EQP_ID 가 비어 결합 불가한 행 수
                     → 이 값이 크면 코드 문제가 아니라 SRC 의
                       wafer-history 조인(b) 이 안 붙은 것이다
    """
    conn = connections['analysis_db']

    with conn.cursor() as cur:
        if oper_id:
            tables = [_table_name(oper_id)]
        else:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE %s "
                "ORDER BY tablename", ['cmp_analysis_%'])
            tables = [r[0] for r in cur.fetchall()]

        if not tables:
            print('[repair] 대상 테이블 없음')
            return

        total_upd = 0
        for t in tables:
            cur.execute("""
                SELECT upper(column_name) FROM information_schema.columns
                WHERE table_name = %s
            """, [t])
            cols = {r[0] for r in cur.fetchall()}
            if 'PRE_EQP_CH' not in cols or 'PRE_EQP_ID' not in cols:
                print(f'  [{t}] PRE_EQP_ID / PRE_EQP_CH 컬럼 없음 — 건너뜀')
                continue

            # 현황 파악 (챔버 값이 있는 행 기준)
            cur.execute(f'''
                SELECT
                  COUNT(*) FILTER (WHERE COALESCE("PRE_EQP_CH", '') <> ''),
                  COUNT(*) FILTER (WHERE COALESCE("PRE_EQP_CH", '') <> ''
                                     AND strpos("PRE_EQP_CH", '_') > 0),
                  COUNT(*) FILTER (WHERE COALESCE("PRE_EQP_CH", '') <> ''
                                     AND COALESCE("PRE_EQP_ID", '') = '')
                FROM {t}
            ''')
            n_ch, n_done, n_noid = cur.fetchone()

            # 결합 — 장비ID 가 있고 아직 결합 안 된 행만
            cur.execute(f'''
                UPDATE {t}
                SET "PRE_EQP_CH" = "PRE_EQP_ID" || '_' || "PRE_EQP_CH"
                WHERE COALESCE("PRE_EQP_ID", '') <> ''
                  AND COALESCE("PRE_EQP_CH", '') <> ''
                  AND strpos("PRE_EQP_CH", '_') = 0
            ''')
            n_upd = cur.rowcount
            total_upd += n_upd

            print(f'  [{t}] 챔버보유 {n_ch:,}행 | 보정 {n_upd:,} | '
                  f'이미결합 {n_done:,} | 장비ID없음 {n_noid:,}')

            if n_ch and n_noid == n_ch:
                print(f'  [{t}] ★ PRE_EQP_ID 가 전부 비어 있음 — '
                      f'코드가 아니라 SRC 의 wafer-history 조인(b) 문제. '
                      f'pre_oper_id 접두 조건과 module_id 필터를 확인할 것')

            # 결과 예시
            cur.execute(f'''
                SELECT DISTINCT "PRE_EQP_CH" FROM {t}
                WHERE COALESCE("PRE_EQP_CH", '') <> '' LIMIT 3
            ''')
            sample = [r[0] for r in cur.fetchall()]
            if sample:
                print(f'  [{t}] 예시: {sample}')

    print(f'[repair] PRE_EQP_CH 보정 완료 — 총 {total_upd:,}행')
    return total_upd


def drop_apc_columns(oper_id=None, dry_run=False):
    """
    이미 저장된 테이블에서 _APC 접미사 중복 컬럼을 삭제한다.

    APC 의 input_name 을 펼친 컬럼은 SRC 의 param_nm 컬럼과 이름이 겹쳐
    머지 과정에서 X_APC 로 갈라졌고, 값은 사실상 X 와 같다.
    저장 로직은 컬럼 추가만 하고 삭제는 하지 않으므로 기존 테이블에는
    그대로 남아 PARAMETER 목록을 두 배로 부풀린다.

      oper_id  : 주면 그 공정만, 생략하면 cmp_analysis_* 전체
      dry_run  : True 면 삭제하지 않고 대상만 출력

    ★ 짝이 되는 원본 컬럼(X)이 있을 때만 X_APC 를 지운다.
      이름이 우연히 _APC 로 끝나는 실제 파라미터는 건드리지 않는다.

    실행 예)
      from equipment import analysis_service as svc
      svc.drop_apc_columns(dry_run=True)   # 먼저 대상 확인
      svc.drop_apc_columns()               # 실제 삭제
    """
    conn = connections['analysis_db']

    with conn.cursor() as cur:
        if oper_id:
            tables = [_table_name(oper_id)]
        else:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE %s "
                "ORDER BY tablename", ['cmp_analysis_%'])
            tables = [r[0] for r in cur.fetchall()]

        if not tables:
            print('[apc-col] 대상 테이블 없음')
            return 0

        total = 0
        for t in tables:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s
            """, [t])
            cols = [r[0] for r in cur.fetchall()]
            upper = {c.upper() for c in cols}

            targets = [c for c in cols
                       if c.upper().endswith('_APC') and c.upper()[:-4] in upper]
            if not targets:
                continue

            if dry_run:
                print(f'  [{t}] 삭제 대상 {len(targets)}개: '
                      f'{", ".join(targets[:8])}'
                      f'{" ..." if len(targets) > 8 else ""}')
            else:
                for c in targets:
                    cur.execute(f'ALTER TABLE {t} DROP COLUMN "{c}"')
                print(f'  [{t}] _APC 컬럼 {len(targets)}개 삭제: '
                      f'{", ".join(targets[:8])}'
                      f'{" ..." if len(targets) > 8 else ""}')
            total += len(targets)

    verb = '삭제 대상' if dry_run else '삭제 완료'
    print(f'[apc-col] {verb} — 총 {total}개')
    return total


def save_config_snapshot(df_info):
    """
    구닥스 기준정보를 PostgreSQL 에 스냅샷으로 저장한다.

    ★ 웹 프로세스는 사내 모듈(Lake/구닥스 클라이언트)을 쓸 수 없다.
      그래서 Inline Monitoring 이 INLINE_YN / PARAM_TYPE 을 읽으려면
      배치가 적재할 때 기준정보를 함께 복사해 둬야 한다.

    배치(run_analysis_load.py)에서 get_config() 직후에 한 번 호출:

        df_info = svc.get_config()
        svc.save_config_snapshot(df_info)

    구닥스에 아직 INLINE_YN / PARAM_TYPE 컬럼이 없으면 그 컬럼만 빠진
    채로 저장되고, 모니터링은 이름 규칙 폴백으로 동작한다.
    """
    if df_info is None or len(df_info) == 0:
        print('[config] 기준정보가 비어 있어 스냅샷을 건너뜁니다')
        return 0

    df = df_info.copy()
    df.columns = [str(c).upper() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]

    table = 'cmp_gooddocs_config'
    conn  = connections['analysis_db']

    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS {table}')
        col_defs = ", ".join(f'"{c}" VARCHAR(300)' for c in df.columns)
        cur.execute(f'CREATE TABLE {table} '
                    f'(id BIGSERIAL PRIMARY KEY, {col_defs})')

        # 전부 텍스트로 저장한다 — 기준정보는 계산 대상이 아니고,
        # 숫자로 캐스팅하면 LOT_CD 5E2 같은 값이 뭉개진다.
        data = [tuple('' if pd.isna(v) else str(v) for v in row)
                for row in df.itertuples(index=False, name=None)]
        cols = ", ".join(f'"{c}"' for c in df.columns)
        execute_values(cur.cursor,
                       f'INSERT INTO {table} ({cols}) VALUES %s',
                       data, page_size=1000)

    missing = [c for c in ('INLINE_YN', 'PARAM_TYPE') if c not in df.columns]
    print(f'[config] 기준정보 스냅샷 저장 {len(data):,}행 / 컬럼 {len(df.columns)}개')
    if missing:
        print(f'[config] ※ 구닥스에 {", ".join(missing)} 컬럼이 없습니다 — '
              f'Inline Monitoring 은 이름 규칙으로 대상을 고릅니다')
    return len(data)


def save_analysis_df(df, oper_id, date_from=None):
    """
    최종 wide df -> PostgreSQL 저장.

    ★ date_from 을 주면 그 시각 이후 행만 지우고 넣는다 (증분 적재).
      주지 않으면 예전처럼 LOT_CD 전체를 지우고 다시 넣는다.
      증분 조회를 하면서 LOT_CD 전체를 지우면 조회하지 않은 이전 기간이
      통째로 사라진다 — 두 가지가 반드시 짝을 이뤄야 한다.

    스키마 자가 치유:
      - 새 컬럼            → ALTER TABLE ADD COLUMN
      - 숫자 → 텍스트 변경 → ALTER COLUMN TYPE VARCHAR (넓히기만, 좁히기는 안 함)
    그 외의 구조 변경은 drop_analysis_table() 을 먼저 호출해야 한다.
    """
    if df is None or df.empty:
        print(f"[{oper_id}] 저장 스킵 (빈 df) — 위 단계별 행수 로그에서 "
              f"어디서 0이 됐는지 확인할 것")
        return

    # ★ 조회 결과가 기존 데이터보다 크게 줄었으면 멈춘다.
    #   전체 교체는 지우고 다시 넣는 것이라, 조회가 일부만 성공한 상태로
    #   저장하면 멀쩡하던 데이터가 그만큼 사라진다.
    #   '적재가 실패했다' 는 알아챌 수 있지만 '조용히 줄었다' 는 놓친다.
    if not date_from:
        try:
            with connections['analysis_db'].cursor() as _cur:
                _cur.execute("SELECT to_regclass(%s) IS NOT NULL",
                             [_table_name(oper_id)])
                if _cur.fetchone()[0]:
                    _cur.execute(f'SELECT COUNT(*) FROM {_table_name(oper_id)}')
                    old_n = _cur.fetchone()[0] or 0
                    if old_n and len(df) < old_n * SHRINK_GUARD:
                        raise RuntimeError(
                            f'조회 결과가 기존의 {len(df)/old_n:.0%} 밖에 '
                            f'안 됩니다 ({old_n:,}행 → {len(df):,}행). '
                            f'일부 조회가 실패했을 수 있어 저장을 멈춥니다 — '
                            f'로그에서 실패한 LOT_CD 를 확인하세요. '
                            f'의도한 축소라면 drop_analysis_table() 로 '
                            f'테이블을 지우고 다시 적재하세요.')
        except RuntimeError:
            raise
        except Exception as e:
            print(f'[{oper_id}] 기존 행수 확인 생략: '
                  f'{e.__class__.__name__}: {e}')

    df = df.copy()
    df.columns = df.columns.str.upper()

    # ★ pandas 는 병합할 때 양쪽에 같은 이름이 있으면 _X / _Y 를 붙인다.
    #   그대로 저장하면 화면에 THK_X 같은 컬럼이 그대로 보인다.
    #   원본 이름이 남아 있으면 접미사가 붙은 쪽은 버리고,
    #   없으면 접미사만 떼어 원래 이름으로 되돌린다.
    dup_fixed, dropped = {}, []
    for c in list(df.columns):
        if not (c.endswith('_X') or c.endswith('_Y')):
            continue
        base = c[:-2]
        if not base:
            continue
        if base in df.columns:
            # 원본이 이미 있다 — 접미사 쪽은 같은 값의 사본이므로 버린다
            dropped.append(c)
        elif base not in dup_fixed:
            dup_fixed[c] = base

    if dropped:
        df = df.drop(columns=dropped)
        print(f'  [{oper_id}] 병합 중복 컬럼 {len(dropped)}개 제거: '
              f'{", ".join(dropped[:6])}{" ..." if len(dropped) > 6 else ""}')
    if dup_fixed:
        df = df.rename(columns=dup_fixed)
        print(f'  [{oper_id}] 접미사 정리 {len(dup_fixed)}개: '
              + ", ".join(f'{k}→{v}' for k, v in list(dup_fixed.items())[:6]))

    df = df.loc[:, ~df.columns.duplicated()]      # 중복 컬럼 방어

    table = _table_name(oper_id)
    conn  = connections['analysis_db']

    # 타입 판정 + 값 캐스팅 (Lake 에서 숫자가 문자열로 오는 경우 대비)
    #   TEXT_COLS 는 값 변환 없이 타입만 VARCHAR 로 고정한다.
    #   (Lake 가 문자열로 주므로 그대로 두면 5E2 가 보존된다)
    col_types = {}
    for c in df.columns:
        if c in TEXT_COLS:
            t = 'VARCHAR(200)'
        elif c in TIME_COLS:
            t = 'TIMESTAMP'
        else:
            t = _pg_type_from_series(df[c])
        col_types[c] = t

        if t == 'DOUBLE PRECISION':
            df[c] = pd.to_numeric(df[c], errors='coerce')
        elif t == 'TIMESTAMP':
            df[c] = pd.to_datetime(df[c], errors='coerce')
        # VARCHAR 는 원본 그대로 둔다 (변환하면 오히려 값이 깨진다)

    col_defs = ["id BIGSERIAL PRIMARY KEY"] + \
               [f'"{c}" {col_types[c]}' for c in df.columns]
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} (\n  "
                    + ",\n  ".join(col_defs) + "\n)")

        # 기존 테이블 구조 확인 (CREATE IF NOT EXISTS 는 구조를 안 바꾼다)
        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = %s
        """, [table])
        exists = {r[0]: r[1] for r in cur.fetchall()}

        # ★ 예전에 만들어진 _X / _Y 컬럼을 지운다.
        #   병합 접미사는 이제 안 생기지만, 이미 테이블에 만들어진 것은
        #   ALTER ADD 만으로는 사라지지 않아 값이 빈 채로 계속 보인다.
        #   이번 조회 결과에 없는 접미사 컬럼만 지운다.
        stale = [c for c in exists
                 if (c.upper().endswith('_X') or c.upper().endswith('_Y'))
                 and c not in df.columns]
        for c in stale:
            try:
                cur.execute(f'ALTER TABLE {table} DROP COLUMN "{c}"')
            except Exception as e:
                print(f'  [{oper_id}] {c} 삭제 실패: {e.__class__.__name__}')
        if stale:
            print(f'  [{oper_id}] 옛 병합 접미사 컬럼 {len(stale)}개 제거: '
                  f'{", ".join(stale[:8])}{" ..." if len(stale) > 8 else ""}')
            for c in stale:
                exists.pop(c, None)

        # 새 컬럼 추가
        added = [c for c in df.columns if c not in exists]
        for c in added:
            cur.execute(f'ALTER TABLE {table} ADD COLUMN "{c}" {col_types[c]}')
        if added:
            print(f'  [{oper_id}] 컬럼 {len(added)}개 추가: '
                  f'{", ".join(added[:8])}{" ..." if len(added) > 8 else ""}')

        # 타입 넓히기 — df 는 텍스트인데 테이블이 숫자면 VARCHAR 로.
        #   이게 없으면 값 형식이 바뀐 컬럼에서
        #   'invalid input syntax for type double precision' 으로 INSERT 실패.
        #   (전부 NULL 인 컬럼은 위에서 숫자로 판정되므로 여기 걸리지 않는다)
        widened = []
        for c in df.columns:
            if c in exists and col_types[c].startswith('VARCHAR') \
                    and exists[c] in _PG_NUMERIC_TYPES:
                cur.execute(
                    f'ALTER TABLE {table} ALTER COLUMN "{c}" TYPE VARCHAR(200) '
                    f'USING "{c}"::VARCHAR(200)')
                widened.append(c)
        if widened:
            print(f'  [{oper_id}] 타입 넓힘(→VARCHAR) {len(widened)}개: '
                  f'{", ".join(widened[:8])}{" ..." if len(widened) > 8 else ""}')

        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_lot  ON {table} ("LOT_CD")')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table} ("DATE")')

    df = df.astype(object).where(pd.notnull(df), None)

    cols    = list(df.columns)
    col_str = ", ".join(f'"{c}"' for c in cols)
    data    = [tuple(r) for r in df.itertuples(index=False, name=None)]
    lot_cds = df['LOT_CD'].dropna().unique().tolist() if 'LOT_CD' in df.columns else []

    # ★ DELETE 와 INSERT 를 한 덩어리로 묶는다.
    #   전체 교체는 LOT_CD 의 45일치를 통째로 지우고 다시 넣는 것이라,
    #   그 사이에 연결이 끊기거나 오류가 나면 그 device 데이터가
    #   통째로 사라진다. 트랜잭션으로 감싸면 실패해도 옛 데이터가
    #   그대로 살아남는다 — 없느니만 못한 상태를 막는다.
    from django.db import transaction

    with transaction.atomic(using='analysis_db'):
        with conn.cursor() as cur:
            for lc in lot_cds:
                if date_from:
                    # 다시 조회한 구간만 교체한다
                    cur.execute(f'DELETE FROM {table} '
                                f'WHERE "LOT_CD" = %s AND "DATE" >= %s',
                                [lc, date_from])
                else:
                    cur.execute(f'DELETE FROM {table} WHERE "LOT_CD" = %s',
                                [lc])
            execute_values(cur.cursor,
                           f'INSERT INTO {table} ({col_str}) VALUES %s',
                           data, page_size=1000)

    mode = f'{date_from} 이후 교체' if date_from else '전체 교체'
    print(f"[{oper_id}] 저장 완료 {len(df):,}행 ({mode} · lot_cd: {lot_cds})")

    # ★ 저장한 뒤 DB 의 실제 최신값을 확인해 찍는다.
    #   조회는 최신인데 DB 가 밀려 있으면 저장 단계가 문제라는 뜻이고,
    #   둘이 같으면 조회 자체가 거기까지만 받은 것이다 — 구분이 된다.
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT MAX("DATE"), COUNT(*) FROM {table}')
            mx, n = cur.fetchone()
        gap = ''
        if mx:
            h = (datetime.now() - mx).total_seconds() / 3600
            gap = f' · 지금으로부터 {h:.1f}시간 전'
        print(f"[{oper_id}] DB 최신 {str(mx)[:19] if mx else '(없음)'}"
              f"{gap} · 전체 {n:,}행")
    except Exception as e:
        print(f"[{oper_id}] DB 최신값 확인 실패: {e.__class__.__name__}: {e}")


# ══════════════════════════════════════════════════════════
# 10. 한 공정 전체 파이프라인
# ══════════════════════════════════════════════════════════
def _latest(d):
    """
    데이터프레임의 최신 시각 — 시각처럼 보이는 컬럼에서 찾는다.

    ★ 단계마다 컬럼 이름이 다르다 (END_TM · DATE · REQUEST_DTTS …).
      우선순위대로 찾아 첫 번째로 값이 있는 것을 쓴다.
    """
    try:
        if d is None or not hasattr(d, 'columns') or d.empty:
            return ''
        up = {str(c).upper(): c for c in d.columns}
        for name in ('DATE', 'END_TM', 'REQUEST_DTTS', 'LAST_UPDATE_DTTS',
                     'PRE_OPER_TIME'):
            c = up.get(name)
            if c is None:
                continue
            v = pd.to_datetime(d[c], errors='coerce').max()
            if pd.notna(v):
                return f'~ {str(v)[:19]}  ({name})'
        return ''
    except Exception:
        return ''


def build_analysis_df(lake, df_info, oper_id, days=30,
                      date_from=None, date_to=None):
    """
    조회 → 정리 → 머지 → 저장형태 정리까지.
    단계별 행수를 로그로 남긴다 — 저장 시 '빈 df' 가 나오면
    이 로그에서 어느 단계가 0인지 바로 보인다.

    date_from/date_to 를 주면 그 기간을, 없으면 오늘부터 days 일 전까지.
    (분석 화면에서 기간을 직접 고르는 경우에 쓴다)
    """
    # ★ 이번 조회의 실패 기록을 새로 시작한다
    clear_fails()

    rng = {'date_from': date_from, 'date_to': date_to}
    def _n(tag, d):
        """
        단계별 행수 + 최신 시각 + 병합 접미사 확인.

        ★ 어느 단계에서 최신 데이터가 잘리는지, 어느 단계에서
          _X/_Y 접미사가 생겼는지 여기서 바로 보인다.
        """
        if VERBOSE:
            print(f"[{oper_id}] {tag:<12} {_rows(d):>8,}행  {_latest(d)}")
            try:
                if d is not None and hasattr(d, 'columns'):
                    bad = [c for c in d.columns
                           if str(c).upper().endswith(('_X', '_Y'))]
                    if bad:
                        print(f'  [{oper_id}] ★ {tag} 에서 병합 접미사 발생: '
                              f'{", ".join(map(str, bad[:8]))}'
                              f'{" ..." if len(bad) > 8 else ""}')
            except Exception:
                pass
        return d

    cond = get_oper_cond(df_info, oper_id)

    df_src = _n('fetch_src',   fetch_src(lake, cond, days, **rng))
    df_apc = _n('fetch_apc',   fetch_apc(lake, cond, days, **rng))
    df_mes = _n('fetch_mes',   fetch_mes(lake, cond, df_src, days, **rng))

    w = _n('pivot_src',   pivot_src(df_src))
    a = _n('prepare_apc', prepare_apc(df_apc))    # IDLE 라벨까지 여기서 계산

    m = _n('merge',       merge_sources(w, a, df_mes))
    m = _n('finalize',    finalize_df(m, cond, df_src))

    # ── Inline 계측 (선택) ───────────────────────────────
    #   등록이 없으면 fetch_steps 가 빈 DF 를 주고 merge_steps 가
    #   그대로 통과시킨다 — 계측 없이도 병합 테이블은 완성된다.
    df_rep = _n('fetch_rep', fetch_steps(lake, cond, 'resp', days, **rng))
    m = _n('merge_rep',      merge_steps(m, df_rep, 'resp'))

    df_def = _n('fetch_def', fetch_steps(lake, cond, 'def', days, **rng))
    m = _n('merge_def',      merge_steps(m, df_def, 'def'))

    # ── 조회 실패 정리 ───────────────────────────────────
    #   ★ 일부 LOT_CD 가 실패해도 나머지는 적재된다.
    #     다만 '일부만 적재됐다' 는 사실을 모르고 지나치면 안 되므로
    #     로그에 분명히 남긴다. 호출부는 get_fails() 로 읽어
    #     적재 결과에 함께 표시한다.
    # ── 등록했는데 안 들어온 파라미터 ────────────────────
    #   ★ 기준정보에 있는 이름이 Lake 에 없으면 컬럼 자체가 안 생긴다.
    #     조용히 넘어가면 '등록했는데 화면에 없다' 가 되고,
    #     원인을 찾기까지 한참 걸린다.
    try:
        want = set()
        for lc in (cond.get('by_lot') or {}).values():
            want |= {str(p).upper() for p in (lc.get('param_list') or []) if p}
        if not want:
            want = {str(p).upper() for p in (cond.get('param_list') or []) if p}

        have = {str(c).upper() for c in (m.columns if m is not None else [])}
        missing = sorted(want - have)
        if missing:
            print(f'\n[{oper_id}] ★ 등록했지만 데이터가 없는 파라미터 '
                  f'{len(missing)}개')
            print(f'    {", ".join(missing[:15])}'
                  f'{" ..." if len(missing) > 15 else ""}')
            print(f'    Lake 에 그 이름이 없거나, 조회 기간에 측정이 '
                  f'없었습니다.')
            print(f'    기준정보의 철자를 확인하세요 — '
                  f'대소문자·언더바까지 정확해야 합니다.\n')
    except Exception as e:
        print(f'[{oper_id}] 파라미터 대조 생략: {e.__class__.__name__}: {e}')

    fails = get_fails()
    if fails:
        print(f'\n{"!" * 58}')
        print(f'[{oper_id}] 조회 실패 {len(fails)}건 — 그 부분은 빠진 채 '
              f'적재됩니다')
        for f in fails[:10]:
            print(f'  · {f["stage"]} {f["key"]}: {f["error"]}')
        if len(fails) > 10:
            print(f'  · 외 {len(fails) - 10}건')
        print('!' * 58 + '\n')

    return m
