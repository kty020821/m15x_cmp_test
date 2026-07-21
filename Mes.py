"""
analysis_service.py — idle 파생 / OPTA 챔버 판정 / 저장용 정리
────────────────────────────────────────────────────────
머지 결과(m)의 컬럼(소문자):
  lot_id, wf_id, substrate_id, main_eqp_id, oper_id, oper_det_desc,
  end_tm, pre_eqp_id, pre_eqp_ch, pre_oper_time,
  <param 컬럼들...>, process_id, recipe_id, operation_id, qty,
  request_dtts, idle, before_info, eqp_ch
"""


# ══════════════════════════════════════════════════════════
# 1. idle_1~4 파생
# ══════════════════════════════════════════════════════════
def derive_idle(df):
    """
    APC의 idle 플래그는 해당 lot 전체 웨이퍼에 동일하게 붙어온다.
      → "이 lot이 idle 직후인가"를 뜻하므로,
        lot 웨이퍼를 시간순 정렬해 앞 4장에 idle_1~4 부여.
      layer_change 는 lot 첫 웨이퍼에만 표기, 나머지는 빈 문자열.
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
        flag = g['idle'].iloc[0]           # lot 전체가 동일 값

        g['idle'] = ''                     # 초기화 후 재부여
        if flag.startswith('idle'):
            for i in range(min(4, len(g))):
                g.loc[i, 'idle'] = f'idle_{i+1}'
        elif 'layer' in flag:
            g.loc[0, 'idle'] = 'layer_change'

        out.append(g)

    return pd.concat(out, ignore_index=True)


# ══════════════════════════════════════════════════════════
# 2. OPTA 챔버 판정
#    KCT/EBARA 는 recipe_id 로 판정(fetch_mes)되지만,
#    OPTA 는 recipe 에 챔버 표기가 없어 param_nm 에서 _P3/_P4 를 추출한다.
#    단, 아래 공정은 param 으로 판정 불가 → 고정값 사용.
#    ★ 예외 공정이 늘면 여기만 수정 (추후 구닥스 컬럼으로 옮기는 것 권장)
# ══════════════════════════════════════════════════════════
OPTA_FIXED_CH = {
    # 'OPER_ID값': 'P4',
}


def derive_opta_chamber(df_src, oper_id):
    """substrate_id 별 OPTA 챔버(P3/P4) 판정 결과 반환"""
    d = df_src.copy()
    d.columns = d.columns.str.lower()

    fixed = OPTA_FIXED_CH.get(str(oper_id))
    if fixed:
        out = d[['substrate_id']].drop_duplicates().copy()
        out['eqp_ch_opta'] = fixed
        return out

    # thk 등 P 표기가 없는 param 은 NaN 으로 빠져 자동 제외됨
    d['ch'] = d['param_nm'].str.extract(r'_(P\d)(?:_|$)')
    return (d.dropna(subset=['ch'])
              .groupby('substrate_id', as_index=False)['ch']
              .first()
              .rename(columns={'ch': 'eqp_ch_opta'}))


# ══════════════════════════════════════════════════════════
# 3. 저장 직전 정리
# ══════════════════════════════════════════════════════════
RENAME_MAP = {
    'end_tm':      'DATE',
    'main_eqp_id': 'EQP_ID',
    'eqp_ch':      'EQP_CH_ID',
    'before_info': 'PRE_LAYER',
    'oper_id':     'OPERATION_ID',
    'idle':        'IDLE',
}

# 저장에서 제외할 컬럼 (조인 부산물 / 중복)
DROP_COLS = [
    'r2r_rank', 'rn', 'rank', 'recipe_id_info', 'before_recipe_id',
    'pre_oper_time', 'request_dtts', 'event_tm', 'oper_det_desc',
]


def finalize_df(df, cond, df_src=None):
    """
    머지+파생 결과를 저장 형태로 정리.
      - OPTA 면 param 기반 챔버 채우기 (df_src 필요)
      - 컬럼명 통일 / 기준정보 컬럼 추가 / 불필요 컬럼 제거 / 대문자화
    """
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = df.columns.str.lower()

    # ── OPTA 챔버 보강 ────────────────────────────────
    if str(cond.get('eq_model', '')).upper() == 'OPTA' and df_src is not None:
        ch = derive_opta_chamber(df_src, cond['oper_id'])
        df = df.merge(ch, on='substrate_id', how='left')
        if 'eqp_ch' not in df.columns:
            df['eqp_ch'] = pd.NA
        df['eqp_ch'] = (df['eqp_ch'].replace('', pd.NA)
                                    .fillna(df['eqp_ch_opta']))
        df = df.drop(columns=['eqp_ch_opta'])

    # ── 정리 ──────────────────────────────────────────
    df = df.drop(columns=[c for c in DROP_COLS if c in df.columns])
    df = df.rename(columns={k: v for k, v in RENAME_MAP.items() if k in df.columns})

    # 기준정보 컬럼
    if 'lot_id' in df.columns:
        df['LOT_CD'] = df['lot_id'].astype(str).str[:3]
    df['EQP_MODEL'] = cond.get('eq_model', '')

    df.columns = [c.upper() for c in df.columns]

    if 'DATE' in df.columns:
        df['DATE'] = pd.to_datetime(df['DATE'], errors='coerce')

    return df
