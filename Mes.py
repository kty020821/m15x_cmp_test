"""
analysis_service.py 에 추가 — MES(LC, Layer Change) 조회
────────────────────────────────────────────────────────
원본 LCGetData 의 모델별 3중 분기를 설정(dict) + 공통 함수로 통합.
모델이 늘어나면 MODEL_CH_CONFIG 에 한 줄만 추가하면 됨.
"""

import numpy as np


# ══════════════════════════════════════════════════════════
# 모델별 챔버 설정
#   chs      : 챔버 구분 목록 (None 이면 챔버 구분 없음)
#   include  : 해당 챔버로 인정할 recipe_id 패턴 (정규식, OR)
#   exclude  : 제외할 recipe_id 패턴 (정규식) — 없으면 None
# ★ 모델 추가 시 여기만 수정
# ══════════════════════════════════════════════════════════
MODEL_CH_CONFIG = {
    'ELASTIC': {
        'L': {'include': r'_L',        'exclude': r'_R$|_R_'},
        'R': {'include': r'_R',        'exclude': r'_L$|_L_'},
    },
    'EBARA': {
        'AB': {'include': r'_AB|_B',   'exclude': r'_CD|_PD'},
        'CD': {'include': r'_CD|_D',   'exclude': r'_AB|_PB'},
    },
    'OPTA': {
        None: {'include': None, 'exclude': None},    # 챔버 구분 없음
    },
    # KCT_NTA / KCT_NTH 는 확인 후 추가 (미정이면 OPTA 규칙 적용됨)
}

DEFAULT_CH_CONFIG = MODEL_CH_CONFIG['OPTA']


def _derive_before_info(s):
    """직전 recipe_id → 이전 layer 표기(LC_xxx) 생성"""
    parts = s.str.split('_')
    p0, p1, p2 = parts.str[0], parts.str[1], parts.str[2]
    three = 'LC_' + p0 + '_' + p1 + '_' + p2
    two   = 'LC_' + p0 + '_' + p1
    return np.where(s.str.contains('ADD_|T_|TB_', na=False), three, two)


def _lc_by_chamber(lc_df, eqp_id, ch, rule, recipe_info):
    """장비 1대 × 챔버 1개에 대한 layer change 추출"""
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

    d['before_info']     = _derive_before_info(d['before_recipe_id'])
    d['recipe_id_info']  = (d['recipe_id'].str.split('_').str[0] + '_' +
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
      cond   : get_oper_cond() 결과 (fab, recipe_list, eq_model 등)
      df_apc : APC 조회 결과 (여기서 eqp_id 목록을 뽑음)
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

    # ── 조회 ────────────────────────────────────────────
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

    # ── 모델별 챔버 규칙으로 layer change 추출 ──────────
    model = str(cond.get('eq_model') or '').upper()
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
