##################

CH = ['L', 'R']
        lc_df_info = pd.DataFrame()

        for i in lc_df['eqp_id'].unique():
            for x in CH:
                if x == 'L':
                    ch_1 = '_' + x #_L
                    temp_data = lc_df[(lc_df['eqp_id'] == i) 
                                        & (lc_df['recipe_id'].str.contains(ch_1))
                                        & (~lc_df['recipe_id'].str.contains(r'_R$|_R_', na=False))]
                    temp_data = temp_data.sort_values('event_tm')
                    temp_data['before_recipe_id'] = temp_data['recipe_id'].shift()
                    temp_data.dropna(axis=0, how='any', inplace=True)
                    temp_data['before_info'] = np.where(temp_data['before_recipe_id'].str.contains('ADD_|T_|TB_'),
                                    'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1] + '_' + temp_data['before_recipe_id'].str.split('_').str[2],
                                        'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1])
                    temp_data['recipe_id_info'] = temp_data['recipe_id'].str.split('_').str[0] + '_' + temp_data['recipe_id'].str.split('_').str[1]
                    temp_data = temp_data[temp_data['recipe_id_info'] == Recipe_info]
                    temp_data['rank'] = 1
                    if lc_df_info.empty:
                        lc_df_info = temp_data
                    else:
                        lc_df_info = pd.concat([lc_df_info, temp_data], axis=0)
                else :
                    ch_1 = '_' + x # _R
                    temp_data = lc_df[(lc_df['eqp_id'] == i) 
                                        & (lc_df['recipe_id'].str.contains(ch_1))
                                        & (~lc_df['recipe_id'].str.contains(r'_L$|_L_', na=False))]
                    temp_data = temp_data.sort_values('event_tm')
                    temp_data['before_recipe_id'] = temp_data['recipe_id'].shift()
                    temp_data.dropna(axis=0, how='any', inplace=True)
                    temp_data['before_info'] = np.where(temp_data['before_recipe_id'].str.contains('ADD_|T_|TB_'),
                                    'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1] + '_' + temp_data['before_recipe_id'].str.split('_').str[2],
                                        'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1])
                    temp_data['recipe_id_info'] = temp_data['recipe_id'].str.split('_').str[0] + '_' + temp_data['recipe_id'].str.split('_').str[1]
                    temp_data = temp_data[temp_data['recipe_id_info'] == Recipe_info]
                    temp_data['rank'] = 1
                    if lc_df_info.empty:
                        lc_df_info = temp_data
                    else:
                        lc_df_info = pd.concat([lc_df_info, temp_data], axis=0)
#################

CH = ['AB', 'CD']
        lc_df_info = pd.DataFrame()

        for i in lc_df['eqp_id'].unique():
            for x in CH:
                if x == 'AB':
                    ch_1 = '_' + x 
                    ch_2 = '_' + x[1] 
                    temp_data = lc_df[(lc_df['eqp_id'] == i) 
                                        & (lc_df['recipe_id'].str.contains(ch_1) | lc_df['recipe_id'].str.contains(ch_2))
                                        & (~lc_df['recipe_id'].str.contains('_CD|_PD'))]
                    temp_data = temp_data.sort_values('event_tm')
                    temp_data['before_recipe_id'] = temp_data['recipe_id'].shift()
                    temp_data.dropna(axis=0, how='any', inplace=True)
                    temp_data['before_info'] = np.where(temp_data['before_recipe_id'].str.contains('ADD_|T_|TB_'),
                                    'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1] + '_' + temp_data['before_recipe_id'].str.split('_').str[2],
                                        'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1])
                    temp_data['recipe_id_info'] = temp_data['recipe_id'].str.split('_').str[0] + '_' + temp_data['recipe_id'].str.split('_').str[1]
                    temp_data = temp_data[temp_data['recipe_id_info'] == Recipe_info]
                    temp_data['rank'] = 1
                    if lc_df_info.empty:
                        lc_df_info = temp_data
                    else:
                        lc_df_info = pd.concat([lc_df_info, temp_data], axis=0)
                else :
                    ch_1 = '_' + x # _CD
                    ch_2 = '_' + x[1] # _D
                    temp_data = lc_df[(lc_df['eqp_id'] == i) 
                                        & (lc_df['recipe_id'].str.contains(ch_1) | lc_df['recipe_id'].str.contains(ch_2))
                                        & (~lc_df['recipe_id'].str.contains('_AB|_PB'))]
                    temp_data = temp_data.sort_values('event_tm')
                    temp_data['before_recipe_id'] = temp_data['recipe_id'].shift()
                    temp_data.dropna(axis=0, how='any', inplace=True)
                    temp_data['before_info'] = np.where(temp_data['before_recipe_id'].str.contains('ADD_|T_|TB_'),
                                    'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1] + '_' + temp_data['before_recipe_id'].str.split('_').str[2],
                                        'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1])
                    temp_data['recipe_id_info'] = temp_data['recipe_id'].str.split('_').str[0] + '_' + temp_data['recipe_id'].str.split('_').str[1]
                    temp_data = temp_data[temp_data['recipe_id_info'] == Recipe_info]
                    temp_data['rank'] = 1
                    if lc_df_info.empty:
                        lc_df_info = temp_data
                    else:
                        lc_df_info = pd.concat([lc_df_info, temp_data], axis=0)0)
#############

lc_df_info = pd.DataFrame()

        for i in lc_df['eqp_id'].unique():

            temp_data = lc_df[(lc_df['eqp_id'] == i)]
            temp_data = temp_data.sort_values('event_tm')
            temp_data['before_recipe_id'] = temp_data['recipe_id'].shift()
            temp_data.dropna(axis=0, how='any', inplace=True)
            temp_data['before_info'] = np.where(temp_data['before_recipe_id'].str.contains('ADD_|T_|TB_'),
                               'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1] + '_' + temp_data['before_recipe_id'].str.split('_').str[2],
                                'LC_' + temp_data['before_recipe_id'].str.split('_').str[0] + '_' + temp_data['before_recipe_id'].str.split('_').str[1])
            temp_data['recipe_id_info'] = temp_data['recipe_id'].str.split('_').str[0] + '_' + temp_data['recipe_id'].str.split('_').str[1]
            temp_data = temp_data[temp_data['recipe_id_info'] == Recipe_info]
            temp_data['rank'] = 1
            if lc_df_info.empty:
                lc_df_info = temp_data
            else:
                lc_df_info = pd.concat([lc_df_info, temp_data], axis=0)
