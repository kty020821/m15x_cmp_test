# EQP_CH_ID = 장비ID_챔버
    if 'eqp_ch' in df.columns and 'main_eqp_id' in df.columns:
        chs = df['eqp_ch'].fillna('').astype(str).str.strip()
        df['eqp_ch'] = pd.Series(
            [f"{e}_{c}" if c else '' for e, c in zip(df['main_eqp_id'].astype(str), chs)],
            index=df.index,
        )

    # ↓ 추가: PRE_EQP_CH = 사전공정장비ID_챔버
    if 'pre_eqp_ch' in df.columns and 'pre_eqp_id' in df.columns:
        pchs = df['pre_eqp_ch'].fillna('').astype(str).str.strip()
        peqp = df['pre_eqp_id'].fillna('').astype(str).str.strip()
        df['pre_eqp_ch'] = pd.Series(
            [f"{e}_{c}" if (e and c) else '' for e, c in zip(peqp, pchs)],
            index=df.index,
        )
