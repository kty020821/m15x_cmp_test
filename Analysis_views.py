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
from datetime import date, timedelta
from django.db import connections
from psycopg2.extras import execute_values

# ★ 사내 모듈 — 기존 코드에서 쓰던 import 문을 그대로 넣을 것
# from ??? import lakes
# from ??? import goodDocsGetData


# 단계별 행수 / 진단 로그
VERBOSE = True


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
    try:
        from . import config_service as cfg
        df = cfg.build_config_df()
        if df is not None and len(df):
            print(f'[config] 기준정보 DB 사용 — {len(df):,}행 / '
                  f'공정 {df["OPER_ID"].nunique()}개')
            return df
        print('[config] cmp_cfg_* 가 비어 있어 구닥스로 폴백합니다 '
              '(셋업 페이지에서 등록하세요)')
    except Exception as e:
        print(f'[config] 기준정보 DB 조회 실패 — 구닥스로 폴백: '
              f'{e.__class__.__name__}: {e}')

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
    """한 공정의 조회 조건 묶음"""
    sub   = df_info[df_info['OPER_ID'] == oper_id]
    first = sub.iloc[0]

    # 챔버 파라미터 자동 확장 (EBARA: PA/PB→PC/PD, KCT: PL→PR)
    param_list = _expand_chamber_params(
        sub['PARAM'].unique().tolist(), first['EQ_MODEL'])

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
    }


# ══════════════════════════════════════════════════════════
# 2. APC 조회
#    idle / layer_change 플래그 + APC 파라미터
#    ※ c.eqp_id 필수 (a 에는 없음)
# ══════════════════════════════════════════════════════════
def fetch_apc(lake, cond, days=30, date_from=None, date_to=None):
    fab = cond['fab']
    dfs = []

    for dt_s, dt_e, mt_s, mt_e in _date_chunks(days, date_from=date_from,
                                                date_to=date_to):
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

    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True).drop_duplicates()


# ══════════════════════════════════════════════════════════
# 3. SRC 조회
#    측정값(long) + 사전공정 장비/챔버
#    ※ lot 조건은 right(lot_cd, 3) — 3자리 (5E2). 2자리로 하면 0행!
# ══════════════════════════════════════════════════════════
def fetch_src(lake, cond, days=30, date_from=None, date_to=None):
    fab      = cond['fab']
    oper_id  = cond['oper_id']
    pre_oper = str(cond.get('pre_oper_id') or '')
    param_in = _param_tuple(cond['param_list'])

    recipe_list = [r for r in cond['recipe_list'] if r]
    # 챔버 확장(_AB/_CD) 등 여러 레시피를 모두 조회한다
    recipe_cond = _recipe_like_cond(recipe_list, 'c.eqp_recipe_id')
    pre_oper_r1 = pre_oper[:-1] if pre_oper else ''

    dfs = []
    for lot_code in cond['lot_cd_list']:
        for dt_s, dt_e, mt_s, mt_e in _date_chunks(days, date_from=date_from,
                                                    date_to=date_to):
            dt_start = pd.to_datetime(dt_s).strftime("%Y-%m-%d")
            dt_end   = pd.to_datetime(dt_e).strftime("%Y-%m-%d")

            # SUBSTRING 길이는 lot_code 자릿수에 맞춘다.
            #   길이를 고정하면(2 또는 3) 다른 자릿수를 쓰는 공정에서
            #   절대 일치하지 않아 DCP 조인이 통째로 실패하고,
            #   recipe 필터가 걸린 공정만 조용히 0행이 된다.
            lot_len = len(str(lot_code))
            query = f"""
WITH src AS (
    select a.lot_id, a.wf_id,
           concat(CAST(a.alias_lot_id as VARCHAR), '.', CAST(a.wf_id as VARCHAR)) as substrate_id,
           a.main_eqp_id, a.param_nm, a.oper_id, a.oper_det_desc,
           a.meas_val as thk_value, a.end_tm,
           b.eqp_id as pre_eqp_id, b.module_id as pre_eqp_ch,
           b.last_update_dtts as pre_oper_time,
           RANK() over(partition by a.lot_id, a.wf_id, a.param_nm order by a.end_tm DESC) r2r_rank
    from lake_catalog.tas.tas_src_wf_metr_inf a
    left join (
        select lot_id, slot_id, wf_id, eqp_id, module_id, MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_m10
        where dt between '{dt_s}' and '{dt_e}'
          and operation_id like '{pre_oper_r1}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id
        union
        select lot_id, slot_id, wf_id, eqp_id, module_id, MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_m11
        where dt between '{dt_s}' and '{dt_e}'
          and operation_id like '{pre_oper_r1}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id
        union
        select lot_id, slot_id, wf_id, eqp_id, module_id, MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_m14
        where dt between '{dt_s}' and '{dt_e}'
          and operation_id like '{pre_oper_r1}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id
        union
        select lot_id, slot_id, wf_id, eqp_id, module_id, MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_m15
        where dt between '{dt_s}' and '{dt_e}'
          and operation_id like '{pre_oper_r1}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id
    ) b on a.lot_id = b.lot_id and a.wf_id = b.wf_id
    left join (
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
            if pre_oper in ('V5071000B', 'X106100B', 'T5515000C'):
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
            df = run_query(lake, query)
            if df is not None and not df.empty:
                dfs.append(df)

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


def fetch_mes(lake, cond, df_src, days=30, date_from=None, date_to=None):
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
    for dt_s, dt_e, _, _ in _date_chunks(days, date_from=date_from,
                                         date_to=date_to):
        query = f"""
select eqp_id, event_tm, last_recipe_id as recipe_id,
       resv_field_val_3 as lot_id
from lake_catalog.mes.mes_mes_eqpmasext_his_{fab}
where dt between '{dt_s}' and '{dt_e}'
  and eqp_id in ({eqp_in})
  and event_cd = 'JobStart'
"""
        d = run_query(lake, query)
        if d is not None and not d.empty:
            dfs.append(d)

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
        df = df.merge(apc, on='substrate_id', how='inner',
                      suffixes=('', '_APC'))                 # inner → rework 자동 제외

        if VERBOSE:
            print(f'  [merge] SRC∩APC → {len(df):,}행')
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


def save_analysis_df(df, oper_id):
    """
    최종 wide df -> PostgreSQL 저장 (LOT_CD 단위 삭제 후 재적재)

    스키마 자가 치유:
      - 새 컬럼            → ALTER TABLE ADD COLUMN
      - 숫자 → 텍스트 변경 → ALTER COLUMN TYPE VARCHAR (넓히기만, 좁히기는 안 함)
    그 외의 구조 변경은 drop_analysis_table() 을 먼저 호출해야 한다.
    """
    if df is None or df.empty:
        print(f"[{oper_id}] 저장 스킵 (빈 df) — 위 단계별 행수 로그에서 "
              f"어디서 0이 됐는지 확인할 것")
        return

    df = df.copy()
    df.columns = df.columns.str.upper()
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

    with conn.cursor() as cur:
        for lc in lot_cds:
            cur.execute(f'DELETE FROM {table} WHERE "LOT_CD" = %s', [lc])
        execute_values(cur.cursor, f'INSERT INTO {table} ({col_str}) VALUES %s',
                       data, page_size=1000)

    print(f"[{oper_id}] 저장 완료 {len(df):,}행 (lot_cd: {lot_cds})")


# ══════════════════════════════════════════════════════════
# 10. 한 공정 전체 파이프라인
# ══════════════════════════════════════════════════════════
def build_analysis_df(lake, df_info, oper_id, days=30):
    """
    조회 → 정리 → 머지 → 저장형태 정리까지.
    단계별 행수를 로그로 남긴다 — 저장 시 '빈 df' 가 나오면
    이 로그에서 어느 단계가 0인지 바로 보인다.
    """
    def _n(tag, d):
        if VERBOSE:
            print(f"[{oper_id}] {tag:<12} {_rows(d):>8,}행")
        return d

    cond = get_oper_cond(df_info, oper_id)

    df_src = _n('fetch_src',   fetch_src(lake, cond, days))
    df_apc = _n('fetch_apc',   fetch_apc(lake, cond, days))
    df_mes = _n('fetch_mes',   fetch_mes(lake, cond, df_src, days))

    w = _n('pivot_src',   pivot_src(df_src))
    a = _n('prepare_apc', prepare_apc(df_apc))    # IDLE 라벨까지 여기서 계산

    m = _n('merge',       merge_sources(w, a, df_mes))

    return _n('finalize', finalize_df(m, cond, df_src))
