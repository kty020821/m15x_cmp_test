SELECT h.eqp_id,
       m.eqp_model_nm,
       m.eqp_oper_grp_cd,
       h.event_tm,
       h.comm_stat_typ,
       h.eqp_stat_cd,
       h.mes_stat_typ,
       h.event_desc,
       c.svid_desc AS part_nm,
       c.curr_val,
       c.target_val
FROM (
    SELECT eqp_id,
           event_tm,
           comm_stat_typ,
           eqp_stat_cd,
           mes_eqp_stat_cd,
           mes_stat_typ,
           event_desc,
           ROW_NUMBER() OVER (PARTITION BY eqp_id ORDER BY event_tm DESC) AS rn
    FROM mes_mes_eqpmasext_his_m15
    WHERE event_tm >= date_sub(now(), 7)
      AND (   eqp_id LIKE '5CMP1E%'
           OR eqp_id LIKE '5KCC88%'
           OR eqp_id LIKE '5CLM1F0%'
           OR eqp_id LIKE '5KCCK0%'
           OR eqp_id LIKE '5CLMK1%' )
      AND eqp_id NOT LIKE '%\_P1' ESCAPE '\'
      AND eqp_id NOT LIKE '%\_P2' ESCAPE '\'
      AND eqp_id NOT LIKE '%\_P3' ESCAPE '\'
      AND eqp_id NOT LIKE '%\_P4' ESCAPE '\'
) h
LEFT JOIN mes_mes_eqp_mas_m15 m  ON h.eqp_id = m.eqp_id
LEFT JOIN pms_fab_cbm_modeling c ON h.eqp_id = c.eq_id
WHERE h.rn = 1
