"""
equipment/analysis_service.py
════════════════════════════════════════════════════════════
산포 분석 데이터 적재 파이프라인

  APC / SRC / MES(LC) 조회 (Lake·StarRocks)
      → SRC pivot(long→wide) / APC 압축 / MES lot단위 정리
      → 머지
      → idle_1~4 파생, EQP_CH_ID 결정
      → PostgreSQL 저장

실행: 루트의 run_analysis_load.py (사내 스케줄러가 호출)

────────────────────────────────────────────────────────────
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


def _date_chunks(days=30, freq='30D'):
    """조회 기간을 (dt_start, dt_end, mt_start, mt_end) 단위로 분할"""
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


def _param_tuple(param_list):
    """구닥스 PARAM 목록 → SQL IN 용 튜플 문자열"""
    params = [str(p) for p in param_list if p]
    if not params:
        return "('')"
    return "(" + ",".join(f"'{p}'" for p in params) + ")"


# ══════════════════════════════════════════════════════════
# 1. 기준정보 (구닥스)
#    컬럼(대문자, PARAM 마다 1행 · 나머지 값 반복):
#      FAB, LOT_CD, OPER_ID, OPER_DESC, EQ_MODEL, RECIPE_ID,
#      PARAM, PRE_OPER_ID, PRE_OPER_DESC, PRE_OPER_PARAM
# ══════════════════════════════════════════════════════════
def get_config():
    return goodDocsGetData().dropna(axis=0)


def get_oper_list(df_info):
    """공정 단위 목록 (OPER_ID 별 대표 1행)"""
    return df_info.drop_duplicates(subset=['OPER_ID'])


import re as _re_chamber

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
            m = _re_chamber.match(rf'^{left}(\d*)_(.+)$', s, _re_chamber.IGNORECASE)
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
def fetch_apc(lake, cond, days=30):
    fab = cond['fab']
    dfs = []

    for dt_s, dt_e, mt_s, mt_e in _date_chunks(days):
        query = f"""
select distinct *
from (
    select a.request_dtts, c.process_id, c.recipe_id, c.operation_id,
           c.lot_id, c.eqp_id, a.substrate_id, a.input_name, a.r2r_status,
           a.input_value, b.item_value,
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
def fetch_src(lake, cond, days=30):
    fab      = cond['fab']
    oper_id  = cond['oper_id']
    pre_oper = str(cond.get('pre_oper_id') or '')
    param_in = _param_tuple(cond['param_list'])

    recipe_list = [r for r in cond['recipe_list'] if r]
    recipe_info = recipe_list[0] if recipe_list else ''
    pre_oper_r1 = pre_oper[:-1] if pre_oper else ''

    dfs = []
    for lot_code in cond['lot_cd_list']:
        for dt_s, dt_e, mt_s, mt_e in _date_chunks(days):
            dt_start = pd.to_datetime(dt_s).strftime("%Y-%m-%d")
            dt_end   = pd.to_datetime(dt_e).strftime("%Y-%m-%d")

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
            where ( SUBSTRING(lot_id, 2, 2) = '{lot_code}'
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
      and c.eqp_recipe_id like '{recipe_info}%'
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


def fetch_mes(lake, cond, df_src, days=30):
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
    for dt_s, dt_e, _, _ in _date_chunks(days):
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
# 5. 소스별 정리 (pivot / 압축)
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


def prepare_apc(df_apc):
    """
    APC → 웨이퍼당 1행.
      item_value 에 idle / layer_change 가 들어오므로 값 자체로 판별
      (item_name 은 SELECT 에 없고, 필요하지도 않음)
    """
    if df_apc is None or df_apc.empty:
        return pd.DataFrame()

    df = df_apc.copy()
    df.columns = df.columns.str.lower()

    iv   = df['item_value'].astype(str).str.strip().str.lower()
    flag = df[iv.isin(['idle', 'layer_change'])]

    if not flag.empty:
        idle = (flag.sort_values('request_dtts')
                    .groupby('substrate_id', as_index=False)['item_value']
                    .first()
                    .rename(columns={'item_value': 'idle'}))
    else:
        idle = pd.DataFrame(columns=['substrate_id', 'idle'])

    meta_cols = [c for c in ['substrate_id', 'lot_id', 'eqp_id', 'process_id',
                             'recipe_id', 'operation_id', 'qty', 'request_dtts']
                 if c in df.columns]
    meta = (df.sort_values('request_dtts')
              .groupby('substrate_id', as_index=False)[meta_cols]
              .first())

    return meta.merge(idle, on='substrate_id', how='left')


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
        # lot_id 중복 방지 — substrate_id 로만 조인
        apc = df_apc_prep.drop(columns=[c for c in ['lot_id'] if c in df_apc_prep.columns])
        df = df.merge(apc, on='substrate_id', how='inner')   # inner → rework 자동 제외

    if df_mes is not None and not df_mes.empty:
        mes = df_mes.copy()
        mes.columns = mes.columns.str.lower()
        keep = [c for c in ['before_info', 'eqp_ch'] if c in mes.columns]
        mes = (mes.sort_values('event_tm')
                  .groupby('lot_id', as_index=False)[keep]
                  .first())                                   # lot 당 1행 (행 뻥튀기 방지)
        df = df.merge(mes, on='lot_id', how='left')

    return df


# ══════════════════════════════════════════════════════════
# 7. 파생 — idle_1~4 / OPTA 챔버
# ══════════════════════════════════════════════════════════
def derive_idle(df):
    """
    APC 의 idle 플래그는 해당 lot 전체 웨이퍼에 동일하게 붙어온다.
      → "이 lot 이 idle 직후인가" 를 뜻하므로
        lot 웨이퍼를 시간순 정렬해 앞 4장에 idle_1~4 부여.
      layer_change 는 lot 첫 웨이퍼에만 표기.
    """
    if df is None or df.empty:
        return df

    df = df.copy()
    if 'idle' not in df.columns:
        df['idle'] = ''
    df['idle'] = df['idle'].fillna('').astype(str).str.strip().str.lower()

    time_col = 'end_tm' if 'end_tm' in df.columns else 'request_dtts'

    out = []
    for lot_id, g in df.groupby('lot_id', sort=False):
        g = g.sort_values(time_col).reset_index(drop=True)
        flag = g['idle'].iloc[0]

        g['idle'] = ''
        if flag.startswith('idle'):
            for i in range(min(4, len(g))):
                g.loc[i, 'idle'] = f'idle_{i+1}'
        elif 'layer' in flag:
            g.loc[0, 'idle'] = 'layer_change'

        out.append(g)

    return pd.concat(out, ignore_index=True)


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


def finalize_df(df, cond, df_src=None):
    """
    머지+파생 결과를 저장 형태로 정리.
      - OPTA 면 param 기반 챔버 채우기 (df_src 필요)
      - EQP_CH_ID = 장비ID_챔버 형태로 결합
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
        df['eqp_ch'] = df['eqp_ch'].replace('', pd.NA).fillna(df['eqp_ch_opta'])
        df = df.drop(columns=['eqp_ch_opta'])

    # EQP_CH_ID = 장비ID_챔버
    if 'eqp_ch' in df.columns and 'main_eqp_id' in df.columns:
        chs = df['eqp_ch'].fillna('').astype(str).str.strip()
        df['eqp_ch'] = pd.Series(
            [f"{e}_{c}" if c else '' for e, c in zip(df['main_eqp_id'].astype(str), chs)],
            index=df.index,
        )

    df = df.drop(columns=[c for c in DROP_COLS if c in df.columns])
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
TEXT_COLS = {
    'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID', 'EQP_MODEL',
    'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID', 'WF_ID',
    'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH',
}
TIME_COLS = {'DATE'}


def _pg_type_from_series(s):
    """
    실제 데이터로 PG 타입 판정.
    하드코딩 목록은 컬럼이 늘 때마다 누락되므로 값을 보고 결정한다.
    """
    if pd.api.types.is_datetime64_any_dtype(s):
        return 'TIMESTAMP'
    if pd.api.types.is_numeric_dtype(s):
        return 'DOUBLE PRECISION'

    nonnull = s.dropna()
    if len(nonnull) == 0:
        return 'VARCHAR(200)'
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


def save_analysis_df(df, oper_id):
    """
    최종 wide df -> PostgreSQL 저장 (LOT_CD 단위 삭제 후 재적재)

    [주의] CREATE TABLE IF NOT EXISTS 는 기존 테이블 구조를 바꾸지 않는다.
           컬럼/타입이 바뀌었으면 drop_analysis_table() 을 먼저 호출할 것.
    """
    if df is None or df.empty:
        print(f"[{oper_id}] 저장 스킵 (빈 df)")
        return

    df = df.copy()
    df.columns = df.columns.str.upper()
    df = df.loc[:, ~df.columns.duplicated()]      # 중복 컬럼 방어

    table = _table_name(oper_id)
    conn  = connections['analysis_db']

    # 타입 판정 + 값 캐스팅 (Lake 에서 숫자가 문자열로 오는 경우 대비)
    col_types = {}
    for c in df.columns:
        t = 'TIMESTAMP' if c in TIME_COLS else _pg_type_from_series(df[c])
        col_types[c] = t
        if t == 'DOUBLE PRECISION':
            df[c] = pd.to_numeric(df[c], errors='coerce')
        elif t == 'TIMESTAMP':
            df[c] = pd.to_datetime(df[c], errors='coerce')

    col_defs = ["id BIGSERIAL PRIMARY KEY"] + \
               [f'"{c}" {col_types[c]}' for c in df.columns]
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} (\n  "
                    + ",\n  ".join(col_defs) + "\n)")
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
    """조회 → 정리 → 머지 → 파생 → 저장형태 정리까지"""
    cond = get_oper_cond(df_info, oper_id)

    df_src = fetch_src(lake, cond, days)
    df_apc = fetch_apc(lake, cond, days)
    df_mes = fetch_mes(lake, cond, df_src, days)      # 장비 목록을 SRC 에서 뽑음

    w = pivot_src(df_src)
    a = prepare_apc(df_apc)
    m = merge_sources(w, a, df_mes)
    m = derive_idle(m)

    return finalize_df(m, cond, df_src)
