"""
analysis_service.py 에 추가 — SRC pivot & APC 정리 & 머지
────────────────────────────────────────────────────────
SRC 컬럼(소문자):
  lot_id, wf_id, substrate_id, main_eqp_id, param_nm, oper_id,
  oper_det_desc, thk_value, end_tm, pre_eqp_id, pre_eqp_ch,
  pre_oper_time, r2r_rank, rn
"""

# 웨이퍼 1장당 하나인 값들 (pivot 후에도 유지)
SRC_META_COLS = [
    'lot_id', 'wf_id', 'substrate_id', 'main_eqp_id',
    'oper_id', 'oper_det_desc', 'end_tm',
    'pre_eqp_id', 'pre_eqp_ch', 'pre_oper_time',
]


def pivot_src(df_src):
    """
    SRC long → wide.
      before: substrate_id | param_nm | thk_value  (웨이퍼당 param 수만큼 행)
      after : substrate_id | THK_AVG | POLISH_TIME | ...  (웨이퍼당 1행)

    end_tm 이 param 별로 미세하게 다를 수 있어 index 에 넣지 않고,
    메타는 substrate_id 기준 대표값(가장 이른 end_tm 행)으로 따로 추출 후 병합.
    """
    if df_src is None or df_src.empty:
        return pd.DataFrame()

    df = df_src.copy()
    df.columns = df.columns.str.lower()

    # 1) 측정값 pivot (substrate_id × param_nm)
    wide = df.pivot_table(
        index='substrate_id',
        columns='param_nm',
        values='thk_value',
        aggfunc='first',        # rn=1 로 이미 유일하지만 방어
    ).reset_index()
    wide.columns.name = None

    # 2) 메타 컬럼 (웨이퍼당 1행) — end_tm 가장 이른 행 기준
    meta_cols = [c for c in SRC_META_COLS if c in df.columns]
    meta = (df.sort_values('end_tm')
              .groupby('substrate_id', as_index=False)[meta_cols]
              .first())

    # 3) 병합
    out = meta.merge(wide, on='substrate_id', how='left')
    return out


def prepare_apc(df_apc):
    """
    APC long → 웨이퍼당 1행으로 정리.
      - item_name/item_value 중 idle 정보만 추출 → 'idle' 컬럼
      - input_name(APC param) 은 필요 시 pivot (현재는 값 미사용: input_value 불필요)
      - 메타(process_id, recipe_id, operation_id, qty 등) 유지
    """
    if df_apc is None or df_apc.empty:
        return pd.DataFrame()

    df = df_apc.copy()
    df.columns = df.columns.str.lower()

    # idle 정보: item_value 에 idle / layer_change 등이 들어옴
    #   ★ 실제 item_name/item_value 조합 확인 후 조건 조정 필요
    idle_src = df[df['item_name'].isin(
        ['IDLE_TIME', 'PROCESS_OFFSET_MES_IDLE_FLAG_IDLE']
    )] if 'item_name' in df.columns else pd.DataFrame()

    if not idle_src.empty:
        idle = (idle_src.sort_values('request_dtts')
                        .groupby('substrate_id', as_index=False)['item_value']
                        .first()
                        .rename(columns={'item_value': 'idle'}))
    else:
        idle = pd.DataFrame(columns=['substrate_id', 'idle'])

    # 메타 (웨이퍼당 1행)
    meta_cols = [c for c in ['substrate_id', 'lot_id', 'process_id', 'recipe_id',
                             'operation_id', 'qty', 'request_dtts']
                 if c in df.columns]
    meta = (df.sort_values('request_dtts')
              .groupby('substrate_id', as_index=False)[meta_cols]
              .first())

    out = meta.merge(idle, on='substrate_id', how='left')
    return out


def merge_sources(df_src_wide, df_apc_prep, df_mes=None):
    """
    SRC(wide) + APC + MES 머지.
      SRC ↔ APC : substrate_id (= lot_id.wf_id)
      ↔ MES     : lot_id
    """
    if df_src_wide is None or df_src_wide.empty:
        return pd.DataFrame()

    df = df_src_wide.copy()

    if df_apc_prep is not None and not df_apc_prep.empty:
        # lot_id 중복 방지: APC 쪽 lot_id 는 제거하고 substrate_id 로만 조인
        apc = df_apc_prep.drop(columns=[c for c in ['lot_id'] if c in df_apc_prep.columns])
        df = df.merge(apc, on='substrate_id', how='inner')   # inner → rework 제외

    if df_mes is not None and not df_mes.empty:
        mes = df_mes.copy()
        mes.columns = mes.columns.str.lower()
        df = df.merge(mes, on='lot_id', how='left')

    return df
