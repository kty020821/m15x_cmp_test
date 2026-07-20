def LCGetData(
  Fab,
  apc_df_merge,
  recipe_info,
  days=None
):
  if Days is None :
    Days = 30

  lake = lakes.LakeHouse(real_user_id = '')

  date_end = date.today()
  diff_days = timedelta(days=Days)
  date_start =. date_end - diff_days
  dt_start = date_start.strftime("%Y%m%d")
  dt_end = date_end.strftime("%Y%m%d")
  eqp_id_list = apc_df_merge.eqp_id.unique()
  eqp_id_list = "'" + "','".join(eqp_id_list) + "'"

  Fab = Fab.lower()

  query = f""" select eqp_id, event_tm, last_recipe_id as recipe_id, resv_field_val_3, as lot_id
              from lake_catalog.mes.mes_mes_eqpmasext_his_{Fab}
              where dt between '{dt_start}' and '{dt_end}'
              and eqp_id in ({eqp_id_list})
              and event_cd = 'JobStart'
  """
  starrocks =. lake.ensure_running(cluster_type='starrocks')
  lake.auto_run_sync_paragraph(code=query)
  lc_df =. lake.get_rst().toPandas()

  lc_df_info = pd.DataFrame()

  #####여기부턴 장비 모델마다 다름
