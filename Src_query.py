"""
analysis_service.py 의 fetch_src 부분.
기존 함수들(_date_chunks, _recipe_cond 등)은 그대로 두고 이 함수만 교체/추가.
────────────────────────────────────────────────────────
"""

def _param_tuple(param_list):
    """구닥스 PARAM 리스트 → SQL IN 용 튜플 문자열: ('A','B','C')"""
    params = [str(p) for p in param_list if p]
    if not params:
        return "('')"                       # 빈 경우 방어
    if len(params) == 1:
        return f"('{params[0]}')"
    return "(" + ",".join(f"'{p}'" for p in params) + ")"


def fetch_src(lake, cond, days=30):
    """
    SRC 조회 (측정값 + 사전공정 장비/챔버).
    cond: get_oper_cond() 결과
      - fab, oper_id, lot_cd_list, recipe_list, param_list
      - pre_oper_id, pre_oper_desc, pre_oper_param
    ※ recipe_info: 구닥스 RECIPE_ID 를 recipe prefix(LIKE)로 사용
    """
    fab       = cond['fab']
    oper_id   = cond['oper_id']
    pre_oper  = str(cond.get('pre_oper_id') or '')
    param_in  = _param_tuple(cond['param_list'])

    # recipe: 원본은 Recipe_info + '%' (prefix LIKE). 구닥스 RECIPE_ID 사용.
    #   여러 개면 각각 LIKE OR 로. 여기선 대표 1개 가정(원본과 동일).
    recipe_list = [r for r in cond['recipe_list'] if r]
    recipe_info = recipe_list[0] if recipe_list else ''

    # pre_oper 뒷자리 한 글자 제거 (원본 로직: Pre_Oper_Code[:-1])
    pre_oper_r1 = pre_oper[:-1] if pre_oper else ''

    dfs = []
    # SRC 는 lot_cd 단위로 조회 (right(lot_cd,2) 조건). 여러 lot_cd면 반복.
    for lot_code in cond['lot_cd_list']:
        for dt_s, dt_e, mt_s, mt_e in _date_chunks(days):
            # 날짜 포맷들
            dt_start = pd.to_datetime(dt_s).strftime("%Y-%m-%d")
            dt_end   = pd.to_datetime(dt_e).strftime("%Y-%m-%d")
            dt_s_r2  = dt_s   # 이미 %Y%m%d
            dt_e_r2  = dt_e
            mt_start = mt_s
            mt_end   = mt_e

            query = f"""
WITH src AS (
    select a.lot_id, a.wf_id,
           concat(CAST(a.alias_lot_id as VARCHAR), '.', CAST(a.wf_id as VARCHAR)) as substrate_id,
           a.main_eqp_id, a.param_nm, a.oper_id, a.oper_det_desc,
           a.meas_val as thk_value, a.end_tm,
           b.eqp_id as pre_eqp_id, b.module_id as pre_eqp_ch,
           b.last_update_dtts as pre_oper_time,
           RANK() over(partition by a.lot_id, a.wf_id, a.param_nm order by a.end_tm DESC) r2r_rank
    from lake_catalog.tas.tas_src_wf_metr_inf a
    left join (
        select lot_id, slot_id, wf_id, eqp_id, module_id, MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_m10
        where dt between '{dt_s_r2}' and '{dt_e_r2}'
          and operation_id like '{pre_oper_r1}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id
        union
        select lot_id, slot_id, wf_id, eqp_id, module_id, MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_m11
        where dt between '{dt_s_r2}' and '{dt_e_r2}'
          and operation_id like '{pre_oper_r1}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id
        union
        select lot_id, slot_id, wf_id, eqp_id, module_id, MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_m14
        where dt between '{dt_s_r2}' and '{dt_e_r2}'
          and operation_id like '{pre_oper_r1}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id
        union
        select lot_id, slot_id, wf_id, eqp_id, module_id, MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_m15
        where dt between '{dt_s_r2}' and '{dt_e_r2}'
          and operation_id like '{pre_oper_r1}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, slot_id, wf_id, eqp_id, module_id
    ) b on a.lot_id = b.lot_id and a.wf_id = b.wf_id
    left join (
        select distinct d.lot_id, d.eqp_recipe_id, d.recipe_rank
        from (
            select lot_id, crt_tm, eqp_recipe_id,
                   rank() over (partition by lot_id order by crt_tm asc) recipe_rank
            from lake_catalog.dcp.dcp_dcp_dcoldata_inf_{fab}
            where ( SUBSTRING(lot_id, 2, 2) = '{lot_code}'
                 or SUBSTRING(lot_id, 2, 2) = 'XC'
                 or SUBSTRING(lot_id, 1, 1) = 'S' )
              and oper_id = '{oper_id}'
              and dt between '{dt_s_r2}' and '{dt_e_r2}'
        ) d
        where d.recipe_rank = 1
    ) c on a.lot_id = c.lot_id
    where a.mt between '{mt_start}' and '{mt_end}'
      and a.end_tm >= '{dt_start}'
      and a.end_tm <= '{dt_end}'
      and a.oper_id = '{oper_id}'
      and right(a.lot_cd, 2) = '{lot_code}'
      and c.eqp_recipe_id like '{recipe_info}%'
      and a.param_nm in {param_in}
"""
            # module_id 분기 (일부 pre_oper 만) — 원본 하드코딩 유지
            if pre_oper in ('V5071000B', 'X106100B', 'T5515000C'):
                query += "      and ( b.module_id = '2' or b.module_id = '3' )\n)"
            elif pre_oper in ('T5515000M', 'T5515000A'):
                query += "      and ( b.module_id = '2' or b.module_id = '3' or b.module_id = '5' )\n)"
            else:
                query += "\n)"

            query += """
select *
from (
    select src.*,
           row_number() over (partition by src.substrate_id, src.param_nm
                              order by src.pre_oper_time desc) as rn
    from src
    where src.r2r_rank = 1
) t
where rn = 1
"""
            df = run_query(lake, query)
            if df is not None and not df.empty:
                dfs.append(df)

    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True).drop_duplicates()
