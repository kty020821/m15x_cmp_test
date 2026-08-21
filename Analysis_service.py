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
  2. IDLE 라벨 진단 로그 — idle_1 이 어디서 사라지는지 추적
  3. PRE_EQP_CH 를 '사전공정장비_챔버' 형태로 결합 (EQP_CH_ID 와 동일 규칙)
  4. Config2 기준 영구 DB 물리 적재 기능 지원 (is_cfg2 플래그 연동)
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

VERBOSE = True

def _rows(d):
    if d is None: return 0
    try: return len(d)
    except TypeError: return 0

def _idle_dist(sr):
    if sr is None or len(sr) == 0: return '(없음)'
    v = sr.fillna('').astype(str).str.strip()
    v = v[v != '']
    if v.empty: return '(없음)'
    c = v.value_counts().sort_index()
    return ', '.join(f'{k}:{n}' for k, n in c.items())

def get_lake():
    lake = lakes.LakeHouse(real_user_id='')
    lake.ensure_running(cluster_type='starrocks')
    return lake

def run_query(lake, query):
    lake.auto_run_sync_paragraph(code=query)
    return lake.get_rst().toPandas()

def _date_chunks(days=30, freq='30D', date_from=None, date_to=None):
    if date_from or date_to:
        date_start = pd.to_datetime(date_from).date() if date_from else None
        date_end   = pd.to_datetime(date_to).date() if date_to else date.today()
        if date_start is None: date_start = date_end - timedelta(days=days)
        if date_start > date_end: date_start, date_end = date_end, date_start
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
    if date_from or date_to:
        d2 = pd.to_datetime(date_to).date() if date_to else date.today()
        d1 = (pd.to_datetime(date_from).date() if date_from else d2 - timedelta(days=days))
        if d1 > d2: d1, d2 = d2, d1
    else:
        d2 = date.today()
        d1 = d2 - timedelta(days=days)
    return d1.strftime('%Y%m'), d2.strftime('%Y%m')

def _recipe_cond(recipe_list, col='recipe_id'):
    rl = [r for r in recipe_list if r]
    if not rl: return ""
    if len(rl) == 1: return f"and {col} = '{rl[0]}'"
    return f"and {col} in (" + ",".join(f"'{r}'" for r in rl) + ")"

def _recipe_like_cond(recipe_list, col='eqp_recipe_id'):
    bases = []
    for r in recipe_list:
        if not r: continue
        b = str(r).split('.')[0]
        if b not in bases: bases.append(b)
    if not bases: return ""
    ors = " or ".join(f"{col} like '{b}%'" for b in bases)
    return f"and ({col} is null or {ors})"

def _param_tuple(param_list):
    params = [str(p) for p in param_list if p]
    if not params: return "('')"
    return "(" + ",".join(f"'{p}'" for p in params) + ")"

# ══════════════════════════════════════════════════════════
def get_config(is_cfg2=False):
    """
    is_cfg2 옵션에 따라 config1 또는 config2 서비스의 DataFrame을 로드합니다.
    """
    try:
        if is_cfg2:
            from equipment import config2_service as cfg
            print('[config] Config2 기준정보 DB 사용')
        else:
            from equipment import config_service as cfg
            print('[config] Config1 기준정보 DB 사용')
            
        df = cfg.build_config_df()
        if df is not None and len(df):
            return df
        print('[config] cmp_cfg_* 가 비어 있어 구닥스로 폴백합니다')
    except Exception as e:
        print(f'[config] 기준정보 DB 조회 실패 — 구닥스로 폴백: {e}')

    try:
        return goodDocsGetData().dropna(axis=0)
    except NameError:
        raise RuntimeError('기준정보 DB와 구닥스 모듈을 모두 찾을 수 없습니다.')

def get_oper_list(df_info):
    return df_info.drop_duplicates(subset=['OPER_ID'])

CHAMBER_TWINS = {
    'EBARA':   [('PA', 'PC'), ('PB', 'PD')],
    'KCT_NTA': [('PL', 'PR')],
    'KCT_NTH': [('PL', 'PR')],
}

def _expand_chamber_params(param_list, model):
    twins = CHAMBER_TWINS.get(str(model).upper())
    if not twins: return param_list
    out, have = list(param_list), set(str(p) for p in param_list)
    for p in param_list:
        s = str(p)
        for left, right in twins:
            m = re.match(rf'^{left}(\d*)_(.+)$', s, re.IGNORECASE)
            if m:
                twin = f"{right}{m.group(1)}_{m.group(2)}"
                if twin not in have:
                    out.append(twin)
                    have.add(twin)
                break
    return out

def get_oper_cond(df_info, oper_id):
    sub   = df_info[df_info['OPER_ID'] == oper_id]
    first = sub.iloc[0]
    param_list = _expand_chamber_params(sub['PARAM'].unique().tolist(), first['EQ_MODEL'])
    return {
        'fab':            str(first['FAB']).lower(),
        'lot_cd_list':    sub['LOT_CD'].unique().tolist(),
        'oper_id':        oper_id,
        'oper_desc':      first['OPER_DESC'],
        'eq_model':       first['EQ_MODEL'],
        'recipe_list':    sub['RECIPE_ID'].unique().tolist(),
        'param_list':     param_list,
        'pre_oper_id':    first['PRE_OPER_ID'],
        'pre_oper_desc':  first['PRE_OPER_DESC'],
        'pre_oper_param': first['PRE_OPER_PARAM'],
        'resp_steps':     _step_cond(oper_id, 'resp'),
        'def_steps':      _step_cond(oper_id, 'def'),
    }

def _step_cond(oper_id, kind):
    try:
        from equipment import config_service as cfg
        df = (cfg.build_response_config_df() if kind == 'resp' else cfg.build_defect_config_df())
    except Exception as e:
        return []
    if df is None or df.empty: return []
    df = df[df['OPER_ID'] == str(oper_id).upper()]
    if df.empty: return []

    out = {}
    for _, r in df.iterrows():
        prm = str(r['PARAM'] or '').strip()
        if not prm: continue
        k = str(r['STEP_ID'] or '').strip().upper()
        if not k: continue
        o = out.setdefault(k, {'step_id': k, 'step_desc': str(r['STEP_DESC'] or '').strip(), 'lot_cds': [], 'params': []})
        if prm not in o['params']: o['params'].append(prm)
        lc = str(r['LOT_CD'] or '').strip().upper()
        if lc and lc not in o['lot_cds']: o['lot_cds'].append(lc)
    return list(out.values())

# ══════════════════════════════════════════════════════════
def fetch_apc(lake, cond, days=30, date_from=None, date_to=None, on_progress=None):
    fab = cond['fab']
    dfs = []
    chunks = _date_chunks(days, date_from=date_from, date_to=date_to)
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
      and ( a.model_name like '%CMP%' or a.model_name like '%KCC88%' or a.model_name like '%KCC01%' )
      and a.input_value is not null
      and b.item_name in ('FORMULA', 'PROCESS_OFFSET_MES_IDLE_FLAG_IDLE', 'IDLE_TIME', 'PROCESS_OFFSET_WAFER_SEQ')
      and c.lot_status = 'JobEnd'
) d
where d.r2r_rank = 1
{_recipe_cond(cond['recipe_list'])}
"""
        df = run_query(lake, query)
        if df is not None and not df.empty: dfs.append(df)
        done += 1
        n = sum(len(d) for d in dfs)
        if on_progress: on_progress(done, total, f'APC {dt_s[:8]} · {n:,}행')
    
    if not dfs: return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True).drop_duplicates()

# ══════════════════════════════════════════════════════════
SRC_PICK = 'first'

def fetch_src(lake, cond, days=30, date_from=None, date_to=None, on_progress=None):
    fab      = cond['fab']
    oper_id  = cond['oper_id']
    pre_oper = str(cond.get('pre_oper_id') or '')
    param_in = _param_tuple(cond['param_list'])

    recipe_list = [r for r in cond['recipe_list'] if r]
    recipe_cond = _recipe_like_cond(recipe_list, 'c.eqp_recipe_id')
    pre_oper_r1 = pre_oper[:-1] if pre_oper else ''

    use_pre = bool(pre_oper)
    chunks = _date_chunks(days, date_from=date_from, date_to=date_to)
    total  = max(1, len(cond['lot_cd_list']) * len(chunks))
    done, dfs = 0, []

    for lot_code in cond['lot_cd_list']:
        for dt_s, dt_e, mt_s, mt_e in chunks:
            dt_start = pd.to_datetime(dt_s).strftime("%Y-%m-%d")
            dt_end   = pd.to_datetime(dt_e).strftime("%Y-%m-%d")
            lot_len = len(str(lot_code))

            if use_pre:
                pre_cols = """b.eqp_id as pre_eqp_id, b.module_id as pre_eqp_ch, b.last_update_dtts as pre_oper_time,"""
                units = ('m10', 'm11', 'm14', 'm15')
                pre_join = "    left join (\n" + "\n        union\n".join(
                    f"""        select lot_id, slot_id, wf_id, eqp_id, module_id, MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_{u}
        where dt between '{dt_s}' and '{dt_e}' and operation_id like '{pre_oper_r1}%' and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id"""
                    for u in units) + "\n    ) b on a.lot_id = b.lot_id and a.wf_id = b.wf_id\n"
            else:
                pre_cols = """CAST(NULL as VARCHAR) as pre_eqp_id, CAST(NULL as VARCHAR) as pre_eqp_ch, CAST(NULL as TIMESTAMP) as pre_oper_time,"""
                pre_join = ""

            src_order = 'ASC' if str(SRC_PICK).lower() == 'first' else 'DESC'

            query = f"""
WITH src AS (
    select a.lot_id, a.wf_id, concat(CAST(a.alias_lot_id as VARCHAR), '.', CAST(a.wf_id as VARCHAR)) as substrate_id,
           a.main_eqp_id, a.param_nm, a.oper_id, a.oper_det_desc, a.meas_val as thk_value, a.end_tm,
           {pre_cols}
           RANK() over(partition by a.lot_id, a.wf_id, a.param_nm order by a.end_tm {src_order}) r2r_rank,
           COUNT(*) over(partition by a.lot_id, a.wf_id, a.param_nm) meas_n
    from lake_catalog.tas.tas_src_wf_metr_inf a
{pre_join}    left join (
        select distinct d.lot_id, d.eqp_recipe_id, d.recipe_rank
        from (
            select lot_id, crt_tm, eqp_recipe_id, rank() over (partition by lot_id order by crt_tm asc) recipe_rank
            from lake_catalog.dcp.dcp_dcp_dcoldata_inf_{fab}
            where ( SUBSTRING(lot_id, 2, {lot_len}) = '{lot_code}' or SUBSTRING(lot_id, 2, 2) = 'XC' or SUBSTRING(lot_id, 1, 1) = 'S' )
              and oper_id = '{oper_id}' and dt between '{dt_s}' and '{dt_e}'
        ) d where d.recipe_rank = 1
    ) c on a.lot_id = c.lot_id
    where a.mt between '{mt_s}' and '{mt_e}' and a.end_tm >= '{dt_start}' and a.end_tm <= '{dt_end}'
      and a.oper_id = '{oper_id}' and right(a.lot_cd, 3) = '{lot_code}'
      {recipe_cond} and a.param_nm in {param_in}
"""
            if not use_pre: query += "\n)"
            elif pre_oper in ('V5071000B', 'X106100B', 'T5515000C'): query += "      and ( b.module_id = '2' or b.module_id = '3' )\n)"
            elif pre_oper in ('T5515000M', 'T5515000A'): query += "      and ( b.module_id = '2' or b.module_id = '3' or b.module_id = '5' )\n)"
            else: query += "\n)"

            query += """
select * from (
    select src.*, row_number() over (partition by src.substrate_id, src.param_nm order by src.pre_oper_time desc) as rn
    from src where src.r2r_rank = 1
) t where rn = 1
"""
            df = run_query(lake, query)
            if df is not None and not df.empty: dfs.append(df)
            done += 1
            n = sum(len(d) for d in dfs)
            if on_progress: on_progress(done, total, f'SRC {lot_code} {dt_s[:8]} · {n:,}행')

    if not dfs: return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True).drop_duplicates()

# ══════════════════════════════════════════════════════════
_KCT_CH = {'L': {'include': r'_L(_|$)', 'exclude': r'_R(_|$)'}, 'R': {'include': r'_R(_|$)', 'exclude': r'_L(_|$)'}}
MODEL_CH_CONFIG = {
    'KCT_NTA': _KCT_CH, 'KCT_NTH': _KCT_CH,
    'EBARA': {'AB': {'include': r'_AB(_|$)', 'exclude': r'_CD(_|$)'}, 'CD': {'include': r'_CD(_|$)', 'exclude': r'_AB(_|$)'}},
    'OPTA': {None: {'include': None, 'exclude': None}},
}
DEFAULT_CH_CONFIG = MODEL_CH_CONFIG['OPTA']

def _derive_before_info(s):
    parts = s.str.split('_')
    p0, p1, p2 = parts.str[0], parts.str[1], parts.str[2]
    three = 'LC_' + p0 + '_' + p1 + '_' + p2
    two   = 'LC_' + p0 + '_' + p1
    return np.where(s.str.contains('ADD_|T_|TB_', na=False), three, two)

def _lc_by_chamber(lc_df, eqp_id, ch, rule, recipe_infos):
    d = lc_df[lc_df['eqp_id'] == eqp_id]
    if rule['include']: d = d[d['recipe_id'].str.contains(rule['include'], na=False, regex=True)]
    if rule['exclude']: d = d[~d['recipe_id'].str.contains(rule['exclude'], na=False, regex=True)]
    if d.empty: return None

    d = d.sort_values('event_tm').copy()
    d['before_recipe_id'] = d['recipe_id'].shift()
    d = d.dropna(subset=['before_recipe_id'])
    if d.empty: return None

    d['before_info']    = _derive_before_info(d['before_recipe_id'])
    d['recipe_id_info'] = (d['recipe_id'].str.split('_').str[0] + '_' + d['recipe_id'].str.split('_').str[1])
    d = d[d['recipe_id_info'].isin(recipe_infos)]
    if d.empty: return None

    d['eqp_ch'] = ch if ch else ''
    d['rank']   = 1
    return d

def fetch_mes(lake, cond, df_src, days=30, date_from=None, date_to=None, on_progress=None):
    if df_src is None or df_src.empty: return pd.DataFrame()
    fab = cond['fab']
    recipe_infos = []
    for r in cond['recipe_list']:
        if not r: continue
        prefix = '_'.join(str(r).split('_')[:2])
        if prefix not in recipe_infos: recipe_infos.append(prefix)

    src = df_src.copy()
    src.columns = src.columns.str.lower()
    eqp_ids = src['main_eqp_id'].dropna().unique()
    if len(eqp_ids) == 0: return pd.DataFrame()
    eqp_in = "'" + "','".join(map(str, eqp_ids)) + "'"

    dfs = []
    _mes_chunks = _date_chunks(days, date_from=date_from, date_to=date_to)
    _mes_total, _mes_done = max(1, len(_mes_chunks)), 0

    for dt_s, dt_e, _, _ in _mes_chunks:
        query = f"""
select eqp_id, event_tm, last_recipe_id as recipe_id, resv_field_val_3 as lot_id
from lake_catalog.mes.mes_mes_eqpmasext_his_{fab}
where dt between '{dt_s}' and '{dt_e}' and eqp_id in ({eqp_in}) and event_cd = 'JobStart'
"""
        d = run_query(lake, query)
        if d is not None and not d.empty: dfs.append(d)
        _mes_done += 1
        if on_progress: on_progress(_mes_done, _mes_total, f'MES {dt_s[:8]}')

    if not dfs: return pd.DataFrame()
    lc_df = pd.concat(dfs, ignore_index=True).drop_duplicates()
    lc_df.columns = lc_df.columns.str.lower()

    model    = str(cond.get('eq_model') or '').upper()
    ch_rules = MODEL_CH_CONFIG.get(model, DEFAULT_CH_CONFIG)
    out = []
    for eqp_id in lc_df['eqp_id'].unique():
        for ch, rule in ch_rules.items():
            part = _lc_by_chamber(lc_df, eqp_id, ch, rule, recipe_infos)
            if part is not None: out.append(part)

    if not out: return pd.DataFrame()
    return pd.concat(out, ignore_index=True)

# ══════════════════════════════════════════════════════════
SQL_REP = """select alias_lot_id, wf_id, end_tm as rep_end_tm, oper_id, oper_det_desc, param_nm, meas_val
from lake_catalog.tas.tas_rep_wf_metr_inf
where mt between '{mt_s}' and '{mt_e}' and lot_cd = '{lot_cd}' and oper_id = '{step}' and param_nm in ({params})"""

SQL_DEF = """select alias_lot_id, wf_id, step_id, defect_class_nm, meas_defect_cnt, end_tm as def_end_tm
from lake_catalog.tas.tas_dft_wf_inf
where mt between '{mt_s}' and '{mt_e}' and lot_cd = '{lot_cd}' and step_id = '{step}' and defect_class_nm in ({params})"""

from collections import deque
QUERY_LOG = deque(maxlen=40)

def _log_query(label, query, rows=None, error=None):
    QUERY_LOG.append({
        'at': datetime.now().strftime('%H:%M:%S'), 'label': label, 'query': query.strip(),
        'rows': rows, 'error': str(error) if error else None,
    })

def last_queries(n=5, label=None):
    items = [q for q in QUERY_LOG if not label or q['label'] == label]
    return list(items)[-n:]

def print_queries(n=5, label=None):
    for q in last_queries(n, label):
        head = f"[{q['at']}] {q['label']}"
        tail = (f"실패: {q['error']}" if q['error'] else f"{q['rows']:,}행" if q['rows'] is not None else '')
        print(f'\n─── {head} · {tail} ' + '─' * 30)
        print(q['query'])

STEP_SQL = {'resp': SQL_REP, 'def': SQL_DEF}
STEP_RENAME = {
    'resp': {'oper_id': 'step_id', 'oper_det_desc': 'lake_step_desc', 'param_nm': 'param', 'meas_val': 'value', 'rep_end_tm': 'end_tm'},
    'def':  {'defect_class_nm': 'param', 'meas_defect_cnt': 'value', 'def_end_tm': 'end_tm'},
}
STEP_LABEL = {'resp': 'REP', 'def': 'DEF'}

def fetch_steps(lake, cond, kind, days=30, date_from=None, date_to=None, on_progress=None):
    label = STEP_LABEL[kind]
    steps = cond.get('resp_steps' if kind == 'resp' else 'def_steps') or []
    if not steps: return pd.DataFrame()

    sql  = STEP_SQL[kind]
    lots_all = [str(v).upper() for v in (cond.get('lot_cd_list') or []) if v]
    mt_s, mt_e = _month_range(days, date_from=date_from, date_to=date_to)

    total = sum(len(st.get('lot_cds') or lots_all) for st in steps) or 1
    done, dfs = 0, []

    for st in steps:
        params = [p for p in st.get('params') or [] if p]
        lots = [l for l in (st.get('lot_cds') or lots_all) if l]
        if not params or not lots: continue
        p_in = ", ".join("'" + str(p).replace("'", "''") + "'" for p in params)

        for lot_cd in lots:
            query = sql.format(mt_s=mt_s, mt_e=mt_e, lot_cd=lot_cd, step=st['step_id'], params=p_in)
            try: d = run_query(lake, query)
            except Exception as e:
                _log_query(label, query, error=e)
                raise

            _log_query(label, query, rows=(0 if d is None else len(d)))
            if d is not None and not d.empty: dfs.append(d)

            done += 1
            n = sum(len(x) for x in dfs)
            if on_progress: on_progress(done, total, f"{label} {st['step_id']} {lot_cd} · {n:,}행")

    if not dfs: return pd.DataFrame()
    out = pd.concat(dfs, ignore_index=True).drop_duplicates()
    out.columns = out.columns.str.lower()
    out = out.rename(columns=STEP_RENAME[kind])
    out['substrate_id'] = (out['alias_lot_id'].astype(str) + '.' + out['wf_id'].astype(str))
    desc_map = {str(s['step_id']).upper(): (s.get('step_desc') or '').strip() for s in steps}
    out['step_desc'] = out['step_id'].astype(str).str.upper().map(lambda k: desc_map.get(k, ''))
    return out

def pivot_steps(df, kind):
    if df is None or df.empty: return pd.DataFrame()
    try:
        from equipment import config_service as cfg
        colname = cfg.step_column
    except Exception:
        def colname(k, step, param):
            pre = 'DEF' if k == 'def' else 'RESP'
            slug = lambda v: re.sub(r'_+', '_', re.sub(r'[^0-9A-Za-z_]+', '_', str(v or '').upper())).strip('_')
            st, pm = slug(step), slug(param)
            return f'{pre}_{st}_{pm}' if st else f'{pre}_{pm}'

    d = df.copy()
    d['__col'] = [colname(kind, (sd or si), p) for sd, si, p in zip(d.get('step_desc', ''), d['step_id'], d['param'])]
    d = d[d['__col'] != '']
    if d.empty: return pd.DataFrame()

    d['value'] = pd.to_numeric(d['value'], errors='coerce')
    wide = d.pivot_table(index='substrate_id', columns='__col', values='value', aggfunc='last').reset_index()
    wide.columns.name = None

    tcol = 'RESP_DATE' if kind == 'resp' else 'DEF_DATE'
    tm = (d.groupby('substrate_id', as_index=False)['end_tm'].max().rename(columns={'end_tm': tcol}))
    return wide.merge(tm, on='substrate_id', how='left')

def merge_steps(base, df_step, kind):
    if base is None or base.empty: return base
    wide = pivot_steps(df_step, kind)
    if wide is None or wide.empty: return base

    key = next((c for c in base.columns if c.lower() == 'substrate_id'), None)
    if key is None: return base
    if key != 'substrate_id': wide = wide.rename(columns={'substrate_id': key})
    if key.isupper(): wide.columns = [c if c == key else str(c).upper() for c in wide.columns]

    return base.merge(wide, on=key, how='left')

# ══════════════════════════════════════════════════════════
SRC_META_COLS = [
    'lot_id', 'wf_id', 'substrate_id', 'main_eqp_id',
    'oper_id', 'oper_det_desc', 'end_tm',
    'pre_eqp_id', 'pre_eqp_ch', 'pre_oper_time',
]

def pivot_src(df_src):
    if df_src is None or df_src.empty: return pd.DataFrame()
    df = df_src.copy()
    df.columns = df.columns.str.lower()
    wide = df.pivot_table(index='substrate_id', columns='param_nm', values='thk_value', aggfunc='first').reset_index()
    wide.columns.name = None

    meta_cols = [c for c in SRC_META_COLS if c in df.columns]
    meta = (df.sort_values('end_tm').groupby('substrate_id', as_index=False)[meta_cols].first())

    if 'meas_n' in df.columns:
        rw = (df.groupby('substrate_id', as_index=False)['meas_n'].max().rename(columns={'meas_n': 'rework_n'}))
        meta = meta.merge(rw, on='substrate_id', how='left')

    return meta.merge(wide, on='substrate_id', how='left')

IDLE_RANK_MAX = 4
CHECK_LOT_COMPLETE = True

def _wf_id_from_substrate(sr):
    return sr.astype(str).str.rsplit('.', n=1).str[-1].str.strip()

def _apc_idle(df):
    idle_rows = df[df['item_name'] == 'IDLE_TIME'].copy()
    if idle_rows.empty: return pd.DataFrame(columns=['substrate_id', 'idle'])
    key = [c for c in ['lot_id', 'eqp_id', 'recipe_id'] if c in idle_rows.columns]
    if 'request_dtts' in idle_rows.columns: idle_rows = idle_rows.sort_values('request_dtts')
    idle_rows = idle_rows.drop_duplicates(subset=['substrate_id'], keep='first')
    idle_rows['wf_no'] = pd.to_numeric(idle_rows['wf_id'], errors='coerce')
    idle_rows = idle_rows[idle_rows['wf_no'].notna()]
    if idle_rows.empty: return pd.DataFrame(columns=['substrate_id', 'idle'])

    if key: idle_rows['rank'] = idle_rows.groupby(key)['wf_no'].rank(method='first')
    else: idle_rows['rank'] = idle_rows['wf_no'].rank(method='first')

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

    return idle_rows[['substrate_id', 'idle']]

def _apc_offset(df):
    def pick(item, name):
        d = df[df['item_name'] == item][['substrate_id', 'input_name', 'item_value']]
        return d.rename(columns={'item_value': name}).drop_duplicates(subset=['substrate_id', 'input_name'], keep='first')
    seq  = pick('PROCESS_OFFSET_WAFER_SEQ', 'seq_offset')
    idle = pick('PROCESS_OFFSET_MES_IDLE_FLAG_IDLE', 'idle_offset')
    if seq.empty and idle.empty: return pd.DataFrame(columns=['substrate_id', 'input_name', 'OFFSET'])
    off = seq.merge(idle, on=['substrate_id', 'input_name'], how='outer')
    for c in ('seq_offset', 'idle_offset'): off[c] = pd.to_numeric(off.get(c), errors='coerce').fillna(0)
    off['OFFSET'] = off['seq_offset'] + off['idle_offset']
    return off[['substrate_id', 'input_name', 'OFFSET']]

def _pivot_by_input(df, values, suffix=''):
    d = df[df['input_name'].notna() & df[values].notna()]
    if d.empty: return pd.DataFrame(columns=['substrate_id'])
    wide = pd.pivot_table(d, index='substrate_id', columns='input_name', values=values, aggfunc='first').reset_index()
    wide.columns.name = None
    if suffix: wide = wide.rename(columns={c: f'{c}{suffix}' for c in wide.columns if c != 'substrate_id'})
    return wide

def prepare_apc(df_apc):
    if df_apc is None or df_apc.empty: return pd.DataFrame()
    df = df_apc.copy()
    df.columns = df.columns.str.lower()
    df['wf_id'] = _wf_id_from_substrate(df['substrate_id'])
    df = df[(df['wf_id'] != '-') & (df['lot_id'].astype(str) != '-')]
    if df.empty: return pd.DataFrame()

    if CHECK_LOT_COMPLETE and 'qty' in df.columns:
        cnt  = df.groupby('lot_id')['substrate_id'].transform('nunique')
        qty  = pd.to_numeric(df['qty'], errors='coerce')
        keep = cnt == qty
        n_lots = df['lot_id'].nunique()
        n_drop = df.loc[~keep, 'lot_id'].nunique()
        if n_drop < n_lots: df = df[keep]
        if df.empty: return pd.DataFrame()

    meta_cols = [c for c in ['substrate_id', 'lot_id', 'wf_id', 'eqp_id', 'process_id', 'recipe_id', 'operation_id', 'qty', 'request_dtts'] if c in df.columns]
    out = (df.sort_values('request_dtts').groupby('substrate_id', as_index=False)[meta_cols].first())
    out = out.merge(_apc_idle(df), on='substrate_id', how='left')
    out['idle'] = out['idle'].fillna('')

    df['input_value'] = pd.to_numeric(df['input_value'], errors='coerce')
    out = out.merge(_pivot_by_input(df, 'input_value'), on='substrate_id', how='left')

    off = _apc_offset(df)
    if not off.empty:
        wide = pd.pivot_table(off, index='substrate_id', columns='input_name', values='OFFSET', aggfunc='first').reset_index()
        wide.columns.name = None
        wide = wide.rename(columns={c: f'{c}_OFFSET' for c in wide.columns if c != 'substrate_id'})
        out = out.merge(wide, on='substrate_id', how='left')

    formula = df[df['item_name'] == 'FORMULA']
    if not formula.empty:
        out = out.merge(_pivot_by_input(formula, 'item_value', '_FORMULA'), on='substrate_id', how='left')

    return out

def merge_sources(df_src_wide, df_apc_prep, df_mes=None):
    if df_src_wide is None or df_src_wide.empty: return pd.DataFrame()
    df = df_src_wide.copy()

    if df_apc_prep is not None and not df_apc_prep.empty:
        apc = df_apc_prep.drop(columns=[c for c in ['lot_id', 'wf_id'] if c in df_apc_prep.columns])
        dup = [c for c in apc.columns if c != 'substrate_id' and c in df.columns]
        if dup: apc = apc.drop(columns=dup)
        df = df.merge(apc, on='substrate_id', how='inner', suffixes=('', '_APC'))

    if df_mes is not None and not df_mes.empty:
        mes = df_mes.copy()
        mes.columns = mes.columns.str.lower()
        keep = [c for c in ['before_info', 'eqp_ch'] if c in mes.columns]
        mes = (mes.sort_values('event_tm').groupby('lot_id', as_index=False)[keep].first())
        df = df.merge(mes, on='lot_id', how='left')

    return df

# ══════════════════════════════════════════════════════════
OPTA_CH_MAP = {'P1': 'P3', 'P2': 'P4'}
OPTA_FIXED_CH = {}

def derive_opta_chamber(df_src, oper_id):
    d = df_src.copy()
    d.columns = d.columns.str.lower()
    fixed = OPTA_FIXED_CH.get(str(oper_id))
    if fixed:
        out = d[['substrate_id']].drop_duplicates().copy()
        out['eqp_ch_opta'] = fixed
        return out
    d['ch'] = d['param_nm'].str.extract(r'_(P\d)(?:_|$)')
    d['ch'] = d['ch'].map(OPTA_CH_MAP).fillna(d['ch'])
    return (d.dropna(subset=['ch']).groupby('substrate_id', as_index=False)['ch'].first().rename(columns={'ch': 'eqp_ch_opta'}))

RENAME_MAP = {
    'end_tm': 'DATE', 'main_eqp_id': 'EQP_ID', 'eqp_ch': 'EQP_CH_ID',
    'before_info': 'PRE_LAYER', 'oper_id': 'OPERATION_ID', 'idle': 'IDLE',
}
DROP_COLS = ['r2r_rank', 'rn', 'rank', 'recipe_id_info', 'before_recipe_id', 'pre_oper_time', 'request_dtts', 'event_tm', 'oper_det_desc', 'operation_id', 'eqp_id']

def _join_eqp_ch(eqp_sr, ch_sr):
    eqps = eqp_sr.fillna('').astype(str).str.strip()
    chs  = ch_sr.fillna('').astype(str).str.strip()
    out = []
    for e, c in zip(eqps, chs):
        if c and e: out.append(f'{e}_{c}')
        elif c: out.append(c)
        else: out.append('')
    return pd.Series(out, index=ch_sr.index)

def finalize_df(df, cond, df_src=None):
    if df is None or df.empty: return pd.DataFrame()
    df = df.copy()
    df.columns = df.columns.str.lower()

    if str(cond.get('eq_model', '')).upper() == 'OPTA' and df_src is not None:
        ch = derive_opta_chamber(df_src, cond['oper_id'])
        df = df.merge(ch, on='substrate_id', how='left')
        if 'eqp_ch' not in df.columns: df['eqp_ch'] = pd.NA
        if 'eqp_ch_opta' in df.columns:
            df['eqp_ch'] = df['eqp_ch'].replace('', pd.NA).fillna(df['eqp_ch_opta'])
            df = df.drop(columns=['eqp_ch_opta'])

    if 'eqp_ch' in df.columns and 'main_eqp_id' in df.columns:
        df['eqp_ch'] = _join_eqp_ch(df['main_eqp_id'], df['eqp_ch'])

    if 'pre_eqp_ch' in df.columns and 'pre_eqp_id' in df.columns:
        df['pre_eqp_ch'] = _join_eqp_ch(df['pre_eqp_id'], df['pre_eqp_ch'])

    df = df.drop(columns=[c for c in DROP_COLS if c in df.columns])

    apc_dup = [c for c in df.columns if c.endswith('_apc') and c[:-4] in df.columns]
    if apc_dup: df = df.drop(columns=apc_dup)

    df = df.rename(columns={k: v for k, v in RENAME_MAP.items() if k in df.columns})
    if 'lot_id' in df.columns: df['LOT_CD'] = df['lot_id'].astype(str).str[:3]
    df['EQP_MODEL'] = cond.get('eq_model', '')
    df.columns = [c.upper() for c in df.columns]
    if 'DATE' in df.columns: df['DATE'] = pd.to_datetime(df['DATE'], errors='coerce')
    df = df.loc[:, ~df.columns.duplicated()]
    return df

# ══════════════════════════════════════════════════════════
TEXT_COLS = {
    'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID', 'EQP_MODEL',
    'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID', 'WF_ID',
    'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'FORMULA', 'IDLE_FLAG'
}
TIME_COLS = {'DATE'}
_PG_NUMERIC_TYPES = {'double precision', 'integer', 'bigint', 'numeric', 'real', 'smallint'}

def _pg_type_from_series(s):
    if pd.api.types.is_datetime64_any_dtype(s): return 'TIMESTAMP'
    if pd.api.types.is_numeric_dtype(s): return 'DOUBLE PRECISION'
    nonnull = s.dropna()
    if len(nonnull) == 0: return 'DOUBLE PRECISION'
    if pd.to_numeric(nonnull, errors='coerce').notna().all(): return 'DOUBLE PRECISION'
    return 'VARCHAR(200)'

def _table_name(oper_id, is_cfg2=False):
    base = f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"
    return f"{base}_cfg2" if is_cfg2 else base

def drop_analysis_table(oper_id, is_cfg2=False):
    table = _table_name(oper_id, is_cfg2=is_cfg2)
    with connections['analysis_db'].cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS {table}')
    print(f"[{oper_id}] {table} DROP 완료")

def repair_numeric_columns(oper_id, is_cfg2=False):
    table = _table_name(oper_id, is_cfg2=is_cfg2)
    fixed, skipped = [], []
    with connections['analysis_db'].cursor() as cur:
        cur.execute("""SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s""", [table])
        for name, dtype in cur.fetchall():
            up = name.upper()
            if up in TEXT_COLS or up in TIME_COLS or up == 'ID': continue
            if dtype.lower() not in ('character varying', 'text'): continue
            try:
                cur.execute(f'ALTER TABLE {table} ALTER COLUMN "{name}" TYPE DOUBLE PRECISION USING NULLIF("{name}", \'\')::double precision')
                fixed.append(name)
            except Exception as e:
                skipped.append(f'{name}({e.__class__.__name__})')
    return fixed

def repair_pre_eqp_ch(oper_id=None, is_cfg2=False):
    conn = connections['analysis_db']
    with conn.cursor() as cur:
        if oper_id: tables = [_table_name(oper_id, is_cfg2=is_cfg2)]
        else:
            cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s ORDER BY tablename", ['cmp_analysis_%'])
            tables = [r[0] for r in cur.fetchall()]

        total_upd = 0
        for t in tables:
            cur.execute("""SELECT upper(column_name) FROM information_schema.columns WHERE table_name = %s""", [t])
            cols = {r[0] for r in cur.fetchall()}
            if 'PRE_EQP_CH' not in cols or 'PRE_EQP_ID' not in cols: continue

            cur.execute(f'''
                UPDATE {t} SET "PRE_EQP_CH" = "PRE_EQP_ID" || '_' || "PRE_EQP_CH"
                WHERE COALESCE("PRE_EQP_ID", '') <> '' AND COALESCE("PRE_EQP_CH", '') <> '' AND strpos("PRE_EQP_CH", '_') = 0
            ''')
            total_upd += cur.rowcount
    return total_upd

def drop_apc_columns(oper_id=None, dry_run=False, is_cfg2=False):
    conn = connections['analysis_db']
    with conn.cursor() as cur:
        if oper_id: tables = [_table_name(oper_id, is_cfg2=is_cfg2)]
        else:
            cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s ORDER BY tablename", ['cmp_analysis_%'])
            tables = [r[0] for r in cur.fetchall()]

        total = 0
        for t in tables:
            cur.execute("""SELECT column_name FROM information_schema.columns WHERE table_name = %s""", [t])
            cols = [r[0] for r in cur.fetchall()]
            upper = {c.upper() for c in cols}
            targets = [c for c in cols if c.upper().endswith('_APC') and c.upper()[:-4] in upper]
            if not targets: continue

            if not dry_run:
                for c in targets: cur.execute(f'ALTER TABLE {t} DROP COLUMN "{c}"')
            total += len(targets)
    return total

def save_config_snapshot(df_info):
    if df_info is None or len(df_info) == 0: return 0
    df = df_info.copy()
    df.columns = [str(c).upper() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]

    table = 'cmp_gooddocs_config'
    conn  = connections['analysis_db']
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS {table}')
        col_defs = ", ".join(f'"{c}" VARCHAR(300)' for c in df.columns)
        cur.execute(f'CREATE TABLE {table} (id BIGSERIAL PRIMARY KEY, {col_defs})')
        data = [tuple('' if pd.isna(v) else str(v) for v in row) for row in df.itertuples(index=False, name=None)]
        cols = ", ".join(f'"{c}"' for c in df.columns)
        execute_values(cur.cursor, f'INSERT INTO {table} ({cols}) VALUES %s', data, page_size=1000)
    return len(data)

def save_analysis_df(df, oper_id, is_cfg2=False):
    if df is None or df.empty: return
    df = df.copy()
    df.columns = df.columns.str.upper()
    df = df.loc[:, ~df.columns.duplicated()]

    table = _table_name(oper_id, is_cfg2=is_cfg2)
    conn  = connections['analysis_db']

    col_types = {}
    for c in df.columns:
        if c in TEXT_COLS: t = 'VARCHAR(200)'
        elif c in TIME_COLS: t = 'TIMESTAMP'
        else: t = _pg_type_from_series(df[c])
        col_types[c] = t

        if t == 'DOUBLE PRECISION': df[c] = pd.to_numeric(df[c], errors='coerce')
        elif t == 'TIMESTAMP': df[c] = pd.to_datetime(df[c], errors='coerce')

    col_defs = ["id BIGSERIAL PRIMARY KEY"] + [f'"{c}" {col_types[c]}' for c in df.columns]
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} (\n  " + ",\n  ".join(col_defs) + "\n)")
        cur.execute("""SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s""", [table])
        exists = {r[0]: r[1] for r in cur.fetchall()}

        added = [c for c in df.columns if c not in exists]
        for c in added: cur.execute(f'ALTER TABLE {table} ADD COLUMN "{c}" {col_types[c]}')

        widened = []
        for c in df.columns:
            if c in exists and col_types[c].startswith('VARCHAR') and exists[c] in _PG_NUMERIC_TYPES:
                cur.execute(f'ALTER TABLE {table} ALTER COLUMN "{c}" TYPE VARCHAR(200) USING "{c}"::VARCHAR(200)')
                widened.append(c)

        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_lot  ON {table} ("LOT_CD")')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table} ("DATE")')

    df = df.astype(object).where(pd.notnull(df), None)
    cols    = list(df.columns)
    col_str = ", ".join(f'"{c}"' for c in cols)
    data    = [tuple(r) for r in df.itertuples(index=False, name=None)]
    lot_cds = df['LOT_CD'].dropna().unique().tolist() if 'LOT_CD' in df.columns else []

    with conn.cursor() as cur:
        for lc in lot_cds: cur.execute(f'DELETE FROM {table} WHERE "LOT_CD" = %s', [lc])
        execute_values(cur.cursor, f'INSERT INTO {table} ({col_str}) VALUES %s', data, page_size=1000)
    print(f"[{oper_id}] 저장 완료 {len(df):,}행 (lot_cd: {lot_cds})")

# ══════════════════════════════════════════════════════════
def build_analysis_df(lake, df_info, oper_id, days=30, date_from=None, date_to=None, on_progress=None, is_cfg2=False):
    rng = {'date_from': date_from, 'date_to': date_to}
    
    cond = get_oper_cond(df_info, oper_id)
    df_src = fetch_src(lake, cond, days, on_progress=on_progress, **rng)
    df_apc = fetch_apc(lake, cond, days, on_progress=on_progress, **rng)
    df_mes = fetch_mes(lake, cond, df_src, days, on_progress=on_progress, **rng)

    w = pivot_src(df_src)
    a = prepare_apc(df_apc)
    m = merge_sources(w, a, df_mes)
    m = finalize_df(m, cond, df_src)

    df_rep = fetch_steps(lake, cond, 'resp', days, on_progress=on_progress, **rng)
    m = merge_steps(m, df_rep, 'resp')

    df_def = fetch_steps(lake, cond, 'def', days, on_progress=on_progress, **rng)
    m = merge_steps(m, df_def, 'def')

    return m
