"""
equipment/analysis_service.py
────────────────────────────────────────────────────────
산포 분석 데이터 적재: APC/SRC/MES 조회 → 머지 → idle 파생 → PG 저장
실행: run_analysis_load.py (사내 스케줄러)
"""

import re
import pandas as pd
from datetime import date, timedelta
from django.db import connections
from psycopg2.extras import execute_values

# ★ 사내 모듈 import (기존 코드에서 쓰던 그대로)
# from ??? import lakes
# from ??? import goodDocsGetData


# ══════════════════════════════════════════════════════════
# 0. Lake 공통
# ══════════════════════════════════════════════════════════
def get_lake():
    lake = lakes.LakeHouse(real_user_id='')
    lake.ensure_running(cluster_type='starrocks')
    return lake


def run_query(lake, query):
    lake.auto_run_sync_paragraph(code=query)
    return lake.get_rst().toPandas()


def _date_chunks(days=30, freq='30D'):
    """조회 기간을 30일 단위 (dt_start, dt_end, mt_start, mt_end) 로 분할"""
    date_end   = date.today()
    date_start = date_end - timedelta(days=days)
    rng = pd.date_range(start=date_start, end=date_end, freq=freq)
    # freq 간격이 기간보다 크면 rng가 1개뿐 → 끝점 보장
    if len(rng) < 2 or rng[-1].date() < date_end:
        rng = rng.append(pd.DatetimeIndex([pd.Timestamp(date_end)]))
    chunks = []
    for i in range(len(rng) - 1):
        chunks.append((
            rng[i].strftime("%Y%m%d"),     # dt_start
            rng[i+1].strftime("%Y%m%d"),   # dt_end
            rng[i].strftime("%Y%m"),       # mt_start
            rng[i+1].strftime("%Y%m"),     # mt_end
        ))
    return chunks


def _recipe_cond(recipe_list, col='recipe_id'):
    """recipe 1개면 =, 여러 개면 IN 조건 문자열"""
    recipe_list = [r for r in recipe_list if r]
    if not recipe_list:
        return ""
    if len(recipe_list) == 1:
        return f"and {col} = '{recipe_list[0]}'"
    in_str = ",".join(f"'{r}'" for r in recipe_list)
    return f"and {col} in ({in_str})"


# ══════════════════════════════════════════════════════════
# 1. 기준정보 (구닥스)
#    컬럼: FAB, LOT_CD, OPER_ID, OPER_DESC, EQ_MODEL, RECIPE_ID,
#          PARAM, PRE_OPER_ID, PRE_OPER_DESC, PRE_OPER_PARAM
#    (param 마다 1행, 나머지 값 반복)
# ══════════════════════════════════════════════════════════
def get_config():
    df_info = goodDocsGetData().dropna(axis=0)
    return df_info


def get_oper_list(df_info):
    """공정 단위 목록: (OPER_ID 별 대표 1행)"""
    return df_info.drop_duplicates(subset=['OPER_ID'])


def get_oper_cond(df_info, oper_id):
    """
    한 공정의 조회 조건 묶음:
      fab, oper_id, recipe_list(중복제거), param_list, pre_oper 정보
    """
    sub = df_info[df_info['OPER_ID'] == oper_id]
    first = sub.iloc[0]
    return {
        'fab':            str(first['FAB']).lower(),   # 테이블명에 소문자
        'lot_cd_list':    sub['LOT_CD'].unique().tolist(),
        'oper_id':        oper_id,
        'oper_desc':      first['OPER_DESC'],
        'recipe_list':    sub['RECIPE_ID'].unique().tolist(),
        'param_list':     sub['PARAM'].unique().tolist(),
        'pre_oper_id':    first['PRE_OPER_ID'],
        'pre_oper_desc':  first['PRE_OPER_DESC'],
        'pre_oper_param': first['PRE_OPER_PARAM'],
    }


# ══════════════════════════════════════════════════════════
# 2. APC 조회 (기존 APCGetData 웹 호환 버전)
# ══════════════════════════════════════════════════════════
def fetch_apc(lake, cond, days=30):
    """
    APC 조회. r2r_rank=1(최신)만, JobEnd만.
    cond: get_oper_cond() 결과
    """
    fab = cond['fab']
    dfs = []

    for dt_s, dt_e, mt_s, mt_e in _date_chunks(days):
        query = f"""
select distinct *
from (
    select a.request_dtts, c.process_id, c.recipe_id, c.operation_id,
           c.lot_id, a.substrate_id, a.input_name, a.r2r_status,
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
# 3. SRC 조회 (★ 쿼리 예시 받으면 채움)
# ══════════════════════════════════════════════════════════
def fetch_src(lake, cond, days=30):
    """
    SRC 조회. 측정값(최종). cond['param_list'] 를 IN 조건으로.
    """
    param_in = ",".join(f"'{p}'" for p in cond['param_list'])
    query = f"""
        -- [TODO] SRC 쿼리
        -- param 조건: and param_nm in ({param_in})
    """
    df = run_query(lake, query)
    return df


# ══════════════════════════════════════════════════════════
# 4. MES 조회 (★ 쿼리 예시 받으면 채움)
# ══════════════════════════════════════════════════════════
def fetch_mes(lake, cond, days=30):
    """
    MES 조회. 사전공정 정보.
    cond['pre_oper_param'] == 'Chamber' → 장비 호기만
    그 외 → 해당 param 조회 조건 추가
    """
    if str(cond['pre_oper_param']).strip().lower() == 'chamber':
        query = f"""
            -- [TODO] MES 쿼리 (장비 호기만)
            -- pre_oper: {cond['pre_oper_id']}
        """
    else:
        query = f"""
            -- [TODO] MES 쿼리 (param 포함)
            -- pre_oper: {cond['pre_oper_id']}, param: {cond['pre_oper_param']}
        """
    df = run_query(lake, query)
    return df


# ══════════════════════════════════════════════════════════
# 5. 머지 + idle 파생
# ══════════════════════════════════════════════════════════
def _split_substrate(df):
    if 'SUBSTRATE_ID' in df.columns and 'WF_ID' not in df.columns:
        parts = df['SUBSTRATE_ID'].astype(str).str.split('.', n=1, expand=True)
        df['LOT_ID'] = parts[0]
        df['WF_ID']  = parts[1]
    return df


def derive_idle(df):
    """idle_1~4 파생 (★ 실제 idle 판정 규칙 확인 후 조정)"""
    if 'IDLE' not in df.columns:
        df['IDLE'] = ''
    df['IDLE'] = df['IDLE'].fillna('')
    df = df.sort_values(['EQP_ID', 'DATE']).reset_index(drop=True) \
        if 'EQP_ID' in df.columns and 'DATE' in df.columns else df

    out = []
    for lot_id, g in df.groupby('LOT_ID', sort=False):
        g = g.sort_values('DATE').reset_index(drop=True) if 'DATE' in g.columns else g.reset_index(drop=True)
        first_idle = str(g.loc[0, 'IDLE']).lower().startswith('idle')
        if first_idle:
            for i in range(min(4, len(g))):
                g.loc[i, 'IDLE'] = f'idle_{i+1}'
        out.append(g)
    return pd.concat(out, ignore_index=True)


def build_analysis_df(lake, df_info, oper_id, days=30):
    """한 공정의 최종 wide df"""
    cond = get_oper_cond(df_info, oper_id)

    df_apc = fetch_apc(lake, cond, days)
    df_src = fetch_src(lake, cond, days)
    df_mes = fetch_mes(lake, cond, days)

    # 컬럼 대문자 통일
    for d in (df_apc, df_src, df_mes):
        if d is not None and not d.empty:
            d.columns = d.columns.str.upper()

    df_apc = _split_substrate(df_apc)
    df_src = _split_substrate(df_src)

    # SRC + APC (wafer 단위, inner → rework 자동 제외)
    df = df_src.merge(df_apc, on=['LOT_ID', 'WF_ID'], how='inner',
                      suffixes=('', '_APC'))

    # + MES (lot 단위)
    if df_mes is not None and not df_mes.empty:
        df = df.merge(df_mes, on='LOT_ID', how='left')

    df = derive_idle(df)
    return df


# ══════════════════════════════════════════════════════════
# 6. PG 저장
# ══════════════════════════════════════════════════════════
TEXT_COLS = {
    'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID', 'EQP_MODEL',
    'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID', 'WF_ID',
    'IDLE', 'PRE_LAYER',
}
TIME_COLS = {'DATE'}


def _pg_type(col):
    cu = col.upper()
    if cu in TIME_COLS: return 'TIMESTAMP'
    if cu in TEXT_COLS: return 'VARCHAR(100)'
    return 'DOUBLE PRECISION'


def _table_name(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def save_analysis_df(df, oper_id):
    if df is None or df.empty:
        print(f"[{oper_id}] 저장 스킵 (빈 df)")
        return

    df = df.copy()
    df.columns = df.columns.str.upper()

    table = _table_name(oper_id)
    conn  = connections['analysis_db']

    col_defs = ["id BIGSERIAL PRIMARY KEY"] + [f'"{c}" {_pg_type(c)}' for c in df.columns]
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} (\n  " + ",\n  ".join(col_defs) + "\n)")
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_lot  ON {table} ("LOT_CD")')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table} ("DATE")')

    df = df.where(pd.notnull(df), None)
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
