def MergeDate(apc_df_merge, src_df_merge, lc_df): 
  """ 원래 쓰고 있던 함수이고, apc_df_merge = df_apc, src_df_merge = df_src, lc_df = df_mes를 의미함.
  아래 코드를 돌렸을 때와 일치하도록 함수를 튜닝해주면 됨. 더 효율적이고 깔끔하게."""

  apc_df_merge['wf_id'] = apc_df_merge['substrate_id'].str[-2]
  drop_idx = apc_df_merge[apc_df_merge['wf_id' == '-'].idx
  apc_df_merge.drop(index=drop_idx, inplace=True)

  apc_df_merge['wf_count'] = apc_df_merge.groupby(['lot_id'])['substrate_id'].transform('nunique')
  apc_df_merge = apc_df_merge[apc_df_merge['wf_count'] == apc_df_merge['qty']].copy()

  if apc_df_merge.empty:
    return pd.DataFrame()

  apc_df_offset_seq = apc_df_merge[apc_df_mege['item_name'] == 'PROCESS_OFFSET_WAFER_SEQ']['substrate_id', 'input_name', 'item_value']].copy()
  apc_df_offset_seq.rename(columns = {'item_value' : 'seq_offset'}, inplace=True}

  apc_df_offset_idle = apc_df_merge[apc_df_merge['item_name'] == 'PROCESS_OFFSET_MES_IDLE_FLAG_IDLE'][['substate_id', 'input_name', 'item_value']].copy()
  apc_df_offset_idle.rename(columns =. {'item_value' : 'idle_offset'}, inplace=True)

  apc_df_offset = pd.merge(apc_df_offset_idle, apc_df_offset_seq, on = ['substrate_id', 'input_name'], how = 'outer')
  apc_df_offset = apc_df_offset.fillna()
  apc_df_offset['idle_offset'] =. pd.to_numeric(apc_df_offset['idle_offset'])
  apc_df_offset['seq_offset'] = pd.to_numeric(apc_df_offset['seq_offset'])
  apc_df_offset['OFFSET'] = apc_df_offset['seq_offset'] + apc_df_offset['idle_offset']
  apc_df_offset.drop_duplicates(inplace=True)

  apc_df_idle = apc_df_merge[apc_df_merge['item_name'] == 'IDLE_TIME'][['substrate_id', 'lot_id', 'wf_id', 'eqp_id','recipe_id', 'item_name', 'item_value']].copy()
  apc_df_idle.drop_duplicates(inplace=True)
  apc_df_idle.reset_index(drop=True, inplace=True)

  apc_df_idle_pivot = pd.pivot(apc_df_idle, index=['substrate_id', 'lot_id', 'wf_id', 'eqp_id', 'recipe_id'], columns = 'item_name', values='item_value').reset_index()
  drop_idx = apc_df_idle_pivot[apc_df_idle_pivot['lot_id'] == '-'].index
  apc_df_idle_pivot.drop(index=drop_idx, inplace=True)

  apc_df_idle_pivot['wf_id'] = pd.to_numeric(apc_df_idle_pivot['wf_id'])
  apc_df_idle_pivot['Rank'] = apc_df_idle_pivot.groupby(['lot_id', 'eqp_id', 'recipe_id'])['wf_id']).rank()

  def idle_rank(idle, rank):
    if((idle=='Idle') | (idle == 'Layer')) & (rank <=4): # 이건 사실 필요한지 모르겠음
      return idle + '_' + str(int(rank))
    else :
      return ""
  idle_rank_vector = np.vertorize(idle_rank)
  apc_df_idle_pivot['IDLE'] = idle_rank_vector(apc_df_idle_pivot['IDLE_TIME'], apc_df_idle_pivot['Rank'])
  apc_df_idle_pivot.drop_duplicates(inplace=True)

  apc_df_formula = apc_df_merge[apc_df_merge['item_name'] ==. 'FORMULA'].copy()
  apc_df_formula.drop_duplicates(inplace=True)
  apc_df_temp = pd.merge(apc_df_formula, apc_df_offset, on = ['substrate_id', 'input_name'], how = 'left')
  apc_df = pd.merge(apc_df_temp, apc_df_idle_pivot[['substrate_id', 'IDLE']], on = 'substrate_id', how = 'left')
  col_list = list(apc_df.columns)
  to_remove_list = ['input_name', 'input_value', 'item_value', 'r2r_status', 'OFFSET']
  filtered_list = [item for item in col_list if item not in to_remove_list]
  apc_df['input_value'] =. pd.to_numeric(apc_df['input_value'], errors = 'coerce')
  apc_df_pivot = pd.pivot_table(data=apc_df, index=filtered_list, columns = 'input_name', values= ['input_value'])
                                                                                                   
  apc_df_pivot.columns = [col[1] if isinstance(col, tuple) else col for col in apc_df_pivot.columns.values]
  apc_df_pivot.reset_index(inpace=True)

  apc_df['input_value'] =.pd.to_numeric(apc_df['input_value'], errors='coerce')
  apc_df_pivot_offset =. pd.pivot_table(data=apc_df, index=['substrate_id'], columns = 'input_name', values = ['OFFSET'])
  apc_df_pivot_offset.columns = [col[1] + '_OFFSET' if isinstance(col, tuple) else col for col in apc_df_pivot_offset.columns.values]
  apc_df_pivot_offset.reset_index(inplace=True)

  apc_df_pivot = pd.merge(apc_df_pivot, apc_df_pivot_offset, on = 'substrate_id', how = 'left')

  apc_df_pivot_formula = pd.pivot_table(data=apc_df, index=['substrate_id'], columns = 'input_name', values='item_value', aggcfunc=lambda x : ','.join(map(str, x)))
  apc_df_pivot_formula.columns = [ col + '_formula' if col != 'substrate_id' else col for col in apc_df_pivot_formula.columns.values]
  apc_df_pivot_formula.reset_index(inplace=True)

  apc_df_pivot = pd.merge(apc_df_pivot, apc_df_pivot_formula, on='substrate_id', how='left')


                                  

