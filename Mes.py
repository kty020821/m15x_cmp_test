"""
analysis_service.py 에 넣을 MES(LC, Layer Change) 부분 최종본
────────────────────────────────────────────────────────
※ import numpy as np 는 파일 맨 위 import 영역에 추가할 것
"""

# ══════════════════════════════════════════════════════════
# 모델별 챔버 설정
#   recipe_id 끝자리로 챔버를 구분한다 (예: xxxxx_L, xxxxx_AB)
#   include : 해당 챔버로 인정할 패턴(정규식). None 이면 챔버 구분 없음
#   exclude : 제외 패턴. 끝자리 매칭($)이면 보통 불필요(None)
# ★ 모델이 추가되면 여기에 한 줄만 넣으면 됨
# ══════════════════════════════════════════════════════════
_KCT_CH = {
    'L': {'include': r'_L$',  'exclude': None},
    'R': {'include': r'_R$',  'exclude': None},
}

MODEL_CH_CONFIG = {
    'KCT_NTA': _KCT_CH,          # 구 ELASTIC
    'KCT_NTH': _KCT_CH,
    'EBARA': {
        'AB': {'include': r'_AB$', 'exclude': None},
        'CD': {'include': r'_CD$', 'exclude': None},
    },
    'OPTA': {
        None: {'include': None, 'exclude': None},   # 챔버 구분 없음
    },
}

# 설정에 없는 모델은 챔버 구분 없이 처리
DEFAULT_CH_CONFIG = MODEL_CH_CONFIG['OPTA']


def _derive_before_info(s):
    """직전 recipe_id → 이전 layer 표기(LC_xxx)"""
    parts = s.str.split('_')
    p0, p1, p2 = parts.str[0], parts.str[1], parts.str[2]
    three = 'LC_' + p0 + '_' + p1 + '_' + p2
    two   = 'LC_' + p0 + '_' + p1
    return np.where(s.str.contains('ADD_|T_|TB_', na=False), three, two)


def _lc_by_chamber(lc_df, eqp_id, ch, rule, recipe_info):
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
    d = d[d['recipe_id_info'] == recipe_info]
    if d.empty:
        return None

    d['eqp_ch'] = ch if ch else ''
    d['rank']   = 1
    return d


def fetch_mes(lake, cond, df_apc, days=30):
    """
    MES(LC) 조회 → layer change 정보.
      cond   : get_oper_cond() 결과 (fab, recipe_list, eq_model 필요)
      df_apc : APC 조회 결과 (여기서 eqp_id 목록 추출)
    반환: eqp_id, event_tm, recipe_id, lot_id, before_recipe_id,
          before_info, recipe_id_info, eqp_ch, rank
    """
    if df_apc is None or df_apc.empty:
        return pd.DataFrame()

    fab = cond['fab']
    recipe_list = [r for r in cond['recipe_list'] if r]
    recipe_info = recipe_list[0] if recipe_list else ''

    # 장비 목록 (APC 결과 기준)
    apc = df_apc.copy()
    apc.columns = apc.columns.str.lower()
    eqp_col = 'eqp_id' if 'eqp_id' in apc.columns else 'main_eqp_id'
    eqp_ids = [e for e in apc[eqp_col].dropna().unique()]
    if not eqp_ids:
        return pd.DataFrame()
    eqp_in = "'" + "','".join(map(str, eqp_ids)) + "'"

    # ── 조회 ──────────────────────────────────────────
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

    # ── 모델별 챔버 규칙 적용 ──────────────────────────
    model    = str(cond.get('eq_model') or '').upper()
    ch_rules = MODEL_CH_CONFIG.get(model, DEFAULT_CH_CONFIG)

    out = []
    for eqp_id in lc_df['eqp_id'].unique():
        for ch, rule in ch_rules.items():
            part = _lc_by_chamber(lc_df, eqp_id, ch, rule, recipe_info)
            if part is not None:
                out.append(part)

    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)
