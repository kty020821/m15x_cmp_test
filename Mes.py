
"""
analysis_service.py — derive_idle 교체 + finalize_df 추가
────────────────────────────────────────────────────────
머지 결과(m)의 컬럼(소문자):
  lot_id, wf_id, substrate_id, main_eqp_id, oper_id, oper_det_desc,
  end_tm, pre_eqp_id, pre_eqp_ch, pre_oper_time,
  <param 컬럼들...>, process_id, recipe_id, operation_id, qty,
  request_dtts, idle, before_info, eqp_ch
"""


def derive_idle(df):
    """
    idle_1~4 파생.
      APC의 idle 플래그는 그 lot 전체 웨이퍼에 동일하게 붙어옴
      → "이 lot이 idle 직후인가"를 뜻하므로,
        해당 lot의 웨이퍼를 시간순 정렬해 앞 4장에 idle_1~4 부여.
      layer_change 는 lot 첫 웨이퍼에만 표기.
      나머지는 빈 문자열.
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
        flag = g['idle'].iloc[0]          # lot 전체가 동일 값

        g['idle'] = ''                    # 초기화 후 재부여
        if flag.startswith('idle'):
            for i in range(min(4, len(g))):
                g.loc[i, 'idle'] = f'idle_{i+1}'
        elif 'layer' in flag:
            g.loc[0, 'idle'] = 'layer_change'

        out.append(g)

    return pd.concat(out, ignore_index=True)


# ══════════════════════════════════════════════════════════
# 저장 직전: 웹이 기대하는 컬럼명으로 정리
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


def finalize_df(df, cond):
    """
    머지+파생 결과를 저장 형태로 정리.
      - 컬럼명 통일 (웹이 기대하는 이름)
      - LOT_CD, EQ_MODEL 등 기준정보 컬럼 추가
      - 불필요 컬럼 제거
      - 전체 대문자화
    """
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = df.columns.str.lower()

    # 불필요 컬럼 제거
    df = df.drop(columns=[c for c in DROP_COLS if c in df.columns])

    # 이름 통일
    df = df.rename(columns={k: v for k, v in RENAME_MAP.items() if k in df.columns})

    # 기준정보 컬럼 추가
    #   LOT_CD: lot_id 앞 3자리 (예: 5E2XXXX → 5E2)
    if 'lot_id' in df.columns:
        df['LOT_CD'] = df['lot_id'].astype(str).str[:3]
    df['EQP_MODEL'] = cond.get('eq_model', '')

    # 나머지 대문자화
    df.columns = [c.upper() for c in df.columns]

    # DATE 는 timestamp 로
    if 'DATE' in df.columns:
        df['DATE'] = pd.to_datetime(df['DATE'], errors='coerce')

    return df
