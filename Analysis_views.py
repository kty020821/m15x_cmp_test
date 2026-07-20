def prepare_apc(df_apc):
    if df_apc is None or df_apc.empty:
        return pd.DataFrame()

    df = df_apc.copy()
    df.columns = df.columns.str.lower()

    # item_value 가 'idle' / 'layer_change' 인 행만 추출
    iv = df['item_value'].astype(str).str.strip().str.lower()
    flag = df[iv.isin(['idle', 'layer_change'])]

    if not flag.empty:
        idle = (flag.sort_values('request_dtts')
                    .groupby('substrate_id', as_index=False)['item_value']
                    .first()
                    .rename(columns={'item_value': 'idle'}))
    else:
        idle = pd.DataFrame(columns=['substrate_id', 'idle'])

    meta_cols = [c for c in ['substrate_id', 'lot_id', 'process_id', 'recipe_id',
                             'operation_id', 'qty', 'request_dtts']
                 if c in df.columns]
    meta = (df.sort_values('request_dtts')
              .groupby('substrate_id', as_index=False)[meta_cols]
              .first())

    return meta.merge(idle, on='substrate_id', how='left')
