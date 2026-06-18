import pandas as pd
import requests
from io import StringIO
from pathlib import Path
from datetime import datetime, timedelta

project_name = ''
api_key      = ''
headers      = {'h-api-token': api_key, 'Content-Type': 'application/json'}
access_url   = ''
wip_url      = ''

ELASTIC_MODELS = ['ELASTIC_NTH', 'ELASTIC_NTA']


def _calc_count(df):
    total, up, down = 0, 0, 0
    for _, row in df.iterrows():
        model = str(row['EQP_MODEL_NM'])
        cnt   = 0.5 if ('ELASTIC' in model or 'REX300X' in model) else 1
        total += cnt
        if row['MES_STAT_TYP'] == 'Up':     up   += cnt
        elif row['MES_STAT_TYP'] == 'Down': down += cnt
    return round(total, 1), round(up, 1), round(down, 1)


def get_equipment_data():
    try:
        resp = requests.post(access_url, headers=headers)
        df   = pd.read_json(StringIO(resp.text))
        has_underscore = df['EQP_ID'].str.contains('_')
        chamber_df     = df[has_underscore]
        base_ids       = chamber_df['EQP_ID'].str.split('_').str[0].unique()
        normal_df      = df[~has_underscore & ~df['EQP_ID'].isin(base_ids)]
        display_df     = pd.concat([chamber_df, normal_df])
        count_df  = display_df.drop_duplicates(subset='EQP_ID').copy()
        opta_df   = count_df[count_df['EQP_MODEL_NM'].str.contains('OPTA', na=False)]
        ch_df     = count_df[count_df['EQP_MODEL_NM'].str.contains('ELASTIC|REX300X|EAC', na=False)]
        other_df  = count_df[~count_df['EQP_MODEL_NM'].str.contains('OPTA|ELASTIC|REX300X|EAC', na=False)]
        opta_total,  opta_up,  opta_down  = _calc_count(opta_df)
        ch_total,    ch_up,    ch_down    = _calc_count(ch_df)
        other_total, other_up, other_down = _calc_count(other_df)
        total = round(opta_total + ch_total + other_total, 1)
        up    = round(opta_up    + ch_up    + other_up,    1)
        down  = round(opta_down  + ch_down  + other_down,  1)
        base_parts = {}
        for eqp_id in df['EQP_ID'].unique():
            if '_' in eqp_id: continue
            edf   = df[df['EQP_ID'] == eqp_id]
            model = edf.iloc[0]['EQP_MODEL_NM']
            if model not in ELASTIC_MODELS: continue
            parts_L, parts_R = [], []
            seen_L,  seen_R  = set(), set()
            for _, prow in edf.iterrows():
                part_nm = str(prow.get('PART_NM', '')).strip()
                if not pd.notna(prow.get('PART_NM')) or part_nm == '': continue
                curr   = prow.get('CURR_VAL', None)
                target = prow.get('TARGET_VAL', None)
                if not pd.notna(curr) or not pd.notna(target): continue
                pct = round(float(curr) / float(target) * 100, 1) if float(target) else 0
                part_data = {'PART_NM': part_nm, 'CURR_VAL': curr, 'TARGET_VAL': target, 'PCT': pct}
                suffix = part_nm.split('_')[-1] if '_' in part_nm else ''
                if 'R' in suffix and part_nm not in seen_R:
                    seen_R.add(part_nm); parts_R.append(part_data)
                elif 'L' in suffix and part_nm not in seen_L:
                    seen_L.add(part_nm); parts_L.append(part_data)
            base_parts[eqp_id + '_L'] = sorted(parts_L, key=lambda x: x['PCT'], reverse=True)
            base_parts[eqp_id + '_R'] = sorted(parts_R, key=lambda x: x['PCT'], reverse=True)
        grouped = {}
        for grp, gdf in display_df.groupby('EQP_OPER_GRP_CD'):
            eqs = []
            for eqp_id in gdf['EQP_ID'].unique():
                edf = gdf[gdf['EQP_ID'] == eqp_id]
                row = edf.iloc[0]
                parts, seen_parts = [], set()
                for _, prow in edf.iterrows():
                    part_nm = str(prow.get('PART_NM', '')).strip()
                    curr    = prow.get('CURR_VAL', None)
                    target  = prow.get('TARGET_VAL', None)
                    if not pd.notna(prow.get('PART_NM')) or part_nm == '' or part_nm in seen_parts: continue
                    if not pd.notna(curr) or not pd.notna(target): continue
                    seen_parts.add(part_nm)
                    pct = round(float(curr) / float(target) * 100, 1) if float(target) else 0
                    parts.append({'PART_NM': part_nm, 'CURR_VAL': curr, 'TARGET_VAL': target, 'PCT': pct})
                if not parts: parts = base_parts.get(eqp_id, [])
                parts = sorted(parts, key=lambda x: x['PCT'], reverse=True)
                eqs.append({'EQP_ID': eqp_id, 'EQP_MODEL_NM': row['EQP_MODEL_NM'],
                            'MES_STAT_TYP': row['MES_STAT_TYP'], 'EQP_STAT_CD': row['EQP_STAT_CD'], 'PARTS': parts})
            grouped[grp] = eqs
        opta_groups, ch_groups, other_groups = [], [], []
        for grp, eqs in grouped.items():
            models_in_grp = set(eq['EQP_MODEL_NM'] for eq in eqs)
            if any('OPTA' in m for m in models_in_grp): opta_groups.append(grp)
            elif any('ELASTIC' in m or 'REX300X' in m or 'EAC' in m for m in models_in_grp): ch_groups.append(grp)
            else: other_groups.append(grp)
        models = sorted(display_df['EQP_MODEL_NM'].dropna().unique().tolist())
        return {'success': True, 'grouped': grouped, 'models': models, 'total': total, 'up': up, 'down': down,
                'group_counts': {'opta': {'total': opta_total, 'up': opta_up, 'down': opta_down},
                                 'ch':   {'total': ch_total,   'up': ch_up,   'down': ch_down},
                                 'other':{'total': other_total,'up': other_up,'down': other_down}},
                'opta_groups': opta_groups, 'ch_groups': ch_groups, 'other_groups': other_groups, 'error': None}
    except Exception as e:
        return {'success': False, 'grouped': {}, 'models': [], 'total': 0, 'up': 0, 'down': 0, 'error': str(e)}


def get_wip_data():
    try:
        resp = requests.post(wip_url, headers=headers)
        df   = pd.read_json(StringIO(resp.text))
        df['main_qty'] = df['EOH_QTY'] - df['SUB_QTY']
        grp_df = df.groupby('SUB_GRP').agg(
            L_MOVE_QTY=('L_MOVE_QTY','sum'), MOVE_TARGET=('MOVE_TARGET','sum'),
            MOVE_QTY=('MOVE_QTY','sum'), BOH_QTY=('BOH_QTY','sum'),
            EOH_QTY=('EOH_QTY','sum'), SUB_QTY=('SUB_QTY','sum'),
            main_qty=('main_qty','sum'), WIP_TARGET=('WIP_TARGET','sum'),
            SEND_QTY=('SEND_QTY','sum'),
        ).reset_index()
        grp_df['WIP_TARGET']    = grp_df['WIP_TARGET'].apply(lambda x: int(x))
        grp_df['l_move_rate']   = (grp_df['L_MOVE_QTY'] / grp_df['MOVE_TARGET'] * 100).round(1)
        grp_df['move_rate']     = (grp_df['MOVE_QTY']   / grp_df['MOVE_TARGET'] * 100).round(1)
        grp_df['gap_qty']       = (grp_df['L_MOVE_QTY'] - grp_df['MOVE_TARGET']).astype(int)
        grp_df['today_gap_qty'] = (grp_df['MOVE_QTY']   - grp_df['MOVE_TARGET']).astype(int)
        grouped = grp_df.to_dict('records')
        return {'success': True, 'grouped': grouped, 'total_loss': int(df['LOSS_MONTH_QTY'].sum()),
                'move_ok': int((grp_df['l_move_rate'] >= 100).sum()),
                'wip_over': int((grp_df['EOH_QTY'] > grp_df['WIP_TARGET']).sum()),
                'group_count': len(grp_df), 'error': None}
    except Exception as e:
        return {'success': False, 'grouped': [], 'total_loss': 0, 'move_ok': 0, 'wip_over': 0, 'group_count': 0, 'error': str(e)}


# ============================================================
# equipment/services.py - get_rtd_data() 함수
# 기존 get_rtd_data() 전체를 이걸로 교체
# ============================================================
#
# ※ df는 이 함수 안에서 API 호출해서 가져오도록 네가 만들어둔 상태.
#   아래는 df를 받은 이후의 집계 로직만 보여줌.
#   네 함수 구조가 def get_rtd_data(): 라면 df 가져오는 부분은
#   그대로 두고, 그 아래 집계 로직만 이걸로 바꾸면 돼.
# ============================================================

def get_rtd_data():
    import pandas as pd

    # ── 1) API에서 df 가져오기 (네가 만들어둔 부분) ──────────
    # resp = requests.post(rtd_url, headers=headers, timeout=10)
    # df = pd.read_json(StringIO(resp.text), dtype={'LOT_CD': str})
    # ↑ 이 부분은 네 코드 그대로 두기
    #
    # 아래는 df가 준비됐다고 가정하고 진행
    # (테스트용으로 빈 df 방지)
    try:
        df  # noqa  -- 위에서 정의된 df 사용
    except NameError:
        df = pd.DataFrame()

    if df.empty:
        return {'success': False, 'error': '데이터 없음', 'cards': [],
                'fab_list': [], 'lot_cd_list': [], 'grp_list': []}

    try:
        # LOT_CD 문자열 보장
        df['LOT_CD'] = df['LOT_CD'].astype(str)

        # ── 필터 옵션 목록 ───────────────────────────────
        fab_list    = sorted(df['FAB'].dropna().unique().tolist())
        lot_cd_list = sorted(df['LOT_CD'].dropna().unique().tolist())
        grp_list    = sorted(df['EQP_OPER_GRP_CD'].dropna().unique().tolist())

        # ── 장비 유형 판별 ───────────────────────────────
        def get_eq_type(model):
            m = str(model).upper()
            if 'OPTA' in m:    return 'OPTA'
            if 'F_REX' in m or 'F-REX' in m or 'FREX' in m: return 'FREX'
            if 'ELASTIC' in m: return 'ELASTIC'
            if 'KCT' in m:     return 'KCT'
            return 'NORMAL'

        # ── 공정그룹(EQP_OPER_GRP_CD)별 전체 장비대수 ────
        #    유형에 따라 집계 단위가 다름
        def count_total_eq_in_grp(grp_df):
            eq_type = get_eq_type(grp_df['EQP_MODEL_NM'].iloc[0])
            all_ids = grp_df['EQP_ID'].dropna().unique().tolist()
            if eq_type == 'OPTA':
                prefixes = set()
                for i in all_ids:
                    s = str(i)
                    prefixes.add(s.split('_P')[0] if '_P' in s else s)
                return len(prefixes)
            elif eq_type in ('FREX', 'ELASTIC', 'KCT'):
                return len([i for i in all_ids if '_' in str(i)])
            else:
                base = [i for i in all_ids if '_' not in str(i)]
                return len(base) if base else len(all_ids)

        grp_total_cache = {grp: count_total_eq_in_grp(gdf)
                           for grp, gdf in df.groupby('EQP_OPER_GRP_CD')}

        # ── 유닛(장비/CH) 단위로 RTD 판정 ───────────────
        def build_units(sub_df, eq_type):
            """sub_df 안의 장비를 유형별 단위로 묶고 RTD 판정"""
            units = []
            if eq_type == 'OPTA':
                key_map = {}
                for _, row in sub_df.iterrows():
                    s = str(row['EQP_ID'])
                    key_map.setdefault(s.split('_P')[0] if '_P' in s else s, []).append(row)
            elif eq_type in ('FREX', 'ELASTIC', 'KCT'):
                key_map = {}
                for _, row in sub_df.iterrows():
                    s = str(row['EQP_ID'])
                    if '_' in s:
                        key_map.setdefault(s, []).append(row)
            else:
                key_map = {}
                for _, row in sub_df.iterrows():
                    s = str(row['EQP_ID'])
                    key_map.setdefault(s if '_' not in s else s.split('_')[0], []).append(row)

            for unit_id, rows in key_map.items():
                rdf = pd.DataFrame(rows)
                is_rtd = (rdf['RTD'].astype(str).str.upper() == 'Y').any()
                dr = rdf[rdf['RTD'].astype(str).str.upper() == 'Y'] if is_rtd else rdf.head(1)
                units.append({'unit_id': unit_id, 'is_rtd': is_rtd,
                              'detail_rows': dr.to_dict('records')})
            return units

        # ── 카드 = 공정(OPER_DESC) ───────────────────────
        #    카드 안에 LOT_CD + FLOW_ID 별 행(rows)
        cards = []
        for oper_desc, oper_df in df.groupby('OPER_DESC'):
            rows_data = []
            # 카드가 어느 공정그룹인지 (대표값)
            card_grp = oper_df['EQP_OPER_GRP_CD'].iloc[0]
            card_fab = oper_df['FAB'].iloc[0] if 'FAB' in oper_df.columns else ''

            # LOT_CD + FLOW_ID 조합별로 행 생성
            for (lot_cd, flow_id, grp), sub_df in oper_df.groupby(['LOT_CD', 'FLOW_ID', 'EQP_OPER_GRP_CD']):
                eq_type = get_eq_type(sub_df['EQP_MODEL_NM'].iloc[0])
                units   = build_units(sub_df, eq_type)

                total = grp_total_cache.get(grp, len(units))
                rtd   = sum(1 for u in units if u['is_rtd'])
                avail = total - rtd

                # 상세(모달)용 데이터
                detail_all = []
                for u in units:
                    for r in u['detail_rows']:
                        detail_all.append({
                            'EQP_ID':      r.get('EQP_ID', ''),
                            'LOT_CD':      str(r.get('LOT_CD', '')),
                            'FLOW_ID':     r.get('FLOW_ID', ''),
                            'RTD':         r.get('RTD', ''),
                            'RTD_USER_NM': r.get('RTD_USER_NM', ''),
                            'RTD_TM':      str(r.get('RTD_TM', '')),
                            'RTD_DESC':    r.get('RTD_DESC', ''),
                        })

                rows_data.append({
                    'lot_cd':     str(lot_cd),
                    'flow_id':    flow_id,
                    'grp':        grp,
                    'eq_type':    eq_type,
                    'total':      total,
                    'avail':      avail,
                    'rtd':        rtd,
                    'units':      units,
                    'detail_all': detail_all,
                })

            # 행 정렬: lot_cd > flow_id
            rows_data.sort(key=lambda x: (x['lot_cd'], x['flow_id']))

            # 카드 단위 요약 (RTD 있는 행이 하나라도 있으면 표시)
            card_has_rtd = any(r['rtd'] > 0 for r in rows_data)

            cards.append({
                'oper_desc':    oper_desc,
                'grp':          card_grp,
                'fab':          card_fab,
                'rows':         rows_data,
                'row_count':    len(rows_data),
                'has_rtd':      card_has_rtd,
            })

        # 카드 정렬: 공정명
        cards.sort(key=lambda x: x['oper_desc'])

        return {
            'success':     True,
            'error':       None,
            'cards':       cards,
            'fab_list':    fab_list,
            'lot_cd_list': lot_cd_list,
            'grp_list':    grp_list,
        }

    except Exception as e:
        import traceback
        return {
            'success':     False,
            'error':       str(e) + '\n' + traceback.format_exc(),
            'cards':       [],
            'fab_list':    [],
            'lot_cd_list': [],
            'grp_list':    [],
        }

def load_inline_data(days=15, rn_filter=None):
    import pandas as pd
    from datetime import datetime, timedelta
    dfs = []
    for i in range(days):
        d = datetime.now() - timedelta(days=i+1)
        fp = DATA_DIR / f'{d.strftime("%Y-%m-%d")}.parquet'
        if fp.exists(): dfs.append(pd.read_parquet(fp))
    if not dfs: return pd.DataFrame()
    result = pd.concat(dfs, ignore_index=True)


# ============================================================
# equipment/services.py - get_rtd_data() 최종본
# ============================================================
# 변경점:
#  - 컬럼명 소문자로 오므로 맨 앞에서 대문자로 통일
#  - RTD 판정: '금지'  (기존 'Y' → '금지')
#  - 모달 상세도 '금지'/'해제' 기준
# ============================================================

def get_rtd_data():
    import pandas as pd
    from io import StringIO

    # ── 1) Impala에서 df 가져오기 ────────────────────────
    #   네가 만들어둔 쿼리 실행 부분.
    #   예시 (실제 네 코드로 교체):
    #   query = """ ...위에서 정리한 SQL... """
    #   lake.auto_run_sync_paragraph(code=query)
    #   df = lake.get_rst().toPandas()
    #
    #   아래는 df가 준비됐다고 가정
    # ────────────────────────────────────────────────────
    try:
        df  # noqa
    except NameError:
        df = pd.DataFrame()

    if df.empty:
        return {'success': False, 'error': '데이터 없음', 'cards': [],
                'fab_list': [], 'lot_cd_list': [], 'grp_list': []}

    try:
        # ── 컬럼명 대문자 통일 (소문자로 내려옴) ─────────
        df.columns = df.columns.str.upper()

        # LOT_CD 문자열 보장
        df['LOT_CD'] = df['LOT_CD'].astype(str)

        # ── 필터 옵션 목록 ───────────────────────────────
        fab_list    = sorted(df['FAB'].dropna().unique().tolist())
        lot_cd_list = sorted(df['LOT_CD'].dropna().unique().tolist())
        grp_list    = sorted(df['EQP_OPER_GRP_CD'].dropna().unique().tolist())

        # ── 장비 유형 판별 ───────────────────────────────
        def get_eq_type(model):
            m = str(model).upper()
            if 'OPTA' in m:    return 'OPTA'
            if 'F_REX' in m or 'F-REX' in m or 'FREX' in m: return 'FREX'
            if 'ELASTIC' in m: return 'ELASTIC'
            if 'KCT' in m:     return 'KCT'
            return 'NORMAL'

        # ── RTD 판정 헬퍼: '금지'이면 True ───────────────
        def is_banned(series):
            return (series.astype(str) == '금지').any()

        # ── 공정그룹별 전체 장비대수 ─────────────────────
        def count_total_eq_in_grp(grp_df):
            eq_type = get_eq_type(grp_df['EQP_MODEL_NM'].iloc[0])
            all_ids = grp_df['EQP_ID'].dropna().unique().tolist()
            if eq_type == 'OPTA':
                prefixes = set()
                for i in all_ids:
                    s = str(i)
                    prefixes.add(s.split('_P')[0] if '_P' in s else s)
                return len(prefixes)
            elif eq_type in ('FREX', 'ELASTIC', 'KCT'):
                return len([i for i in all_ids if '_' in str(i)])
            else:
                base = [i for i in all_ids if '_' not in str(i)]
                return len(base) if base else len(all_ids)

        grp_total_cache = {grp: count_total_eq_in_grp(gdf)
                           for grp, gdf in df.groupby('EQP_OPER_GRP_CD')}

        # ── 유닛(장비/CH) 단위로 RTD 판정 ───────────────
        def build_units(sub_df, eq_type):
            units = []
            if eq_type == 'OPTA':
                key_map = {}
                for _, row in sub_df.iterrows():
                    s = str(row['EQP_ID'])
                    key_map.setdefault(s.split('_P')[0] if '_P' in s else s, []).append(row)
            elif eq_type in ('FREX', 'ELASTIC', 'KCT'):
                key_map = {}
                for _, row in sub_df.iterrows():
                    s = str(row['EQP_ID'])
                    if '_' in s:
                        key_map.setdefault(s, []).append(row)
            else:
                key_map = {}
                for _, row in sub_df.iterrows():
                    s = str(row['EQP_ID'])
                    key_map.setdefault(s if '_' not in s else s.split('_')[0], []).append(row)

            for unit_id, rows in key_map.items():
                rdf = pd.DataFrame(rows)
                banned = is_banned(rdf['RTD'])
                dr = rdf[rdf['RTD'].astype(str) == '금지'] if banned else rdf.head(1)
                units.append({'unit_id': unit_id, 'is_rtd': banned,
                              'detail_rows': dr.to_dict('records')})
            return units

        # ── 카드 = 공정(OPER_DESC), 안에 LOT+FLOW 행 ────
        cards = []
        for oper_desc, oper_df in df.groupby('OPER_DESC'):
            rows_data = []
            card_grp = oper_df['EQP_OPER_GRP_CD'].iloc[0]
            card_fab = oper_df['FAB'].iloc[0] if 'FAB' in oper_df.columns else ''

            for (lot_cd, flow_id, grp), sub_df in oper_df.groupby(['LOT_CD', 'FLOW_ID', 'EQP_OPER_GRP_CD']):
                eq_type = get_eq_type(sub_df['EQP_MODEL_NM'].iloc[0])
                units   = build_units(sub_df, eq_type)

                total = grp_total_cache.get(grp, len(units))
                rtd   = sum(1 for u in units if u['is_rtd'])
                avail = total - rtd

                detail_all = []
                for u in units:
                    for r in u['detail_rows']:
                        detail_all.append({
                            'EQP_ID':      r.get('EQP_ID', ''),
                            'LOT_CD':      str(r.get('LOT_CD', '')),
                            'FLOW_ID':     r.get('FLOW_ID', ''),
                            'RTD':         r.get('RTD', ''),
                            'RTD_USER_NM': r.get('RTD_USER_NM', ''),
                            'RTD_TM':      str(r.get('RTD_TM', '')),
                            'RTD_DESC':    r.get('RTD_DESC', ''),
                        })

                rows_data.append({
                    'lot_cd':     str(lot_cd),
                    'flow_id':    flow_id,
                    'grp':        grp,
                    'eq_type':    eq_type,
                    'total':      total,
                    'avail':      avail,
                    'rtd':        rtd,
                    'units':      units,
                    'detail_all': detail_all,
                })

            rows_data.sort(key=lambda x: (x['lot_cd'], x['flow_id']))
            card_has_rtd = any(r['rtd'] > 0 for r in rows_data)

            cards.append({
                'oper_desc': oper_desc,
                'grp':       card_grp,
                'fab':       card_fab,
                'rows':      rows_data,
                'row_count': len(rows_data),
                'has_rtd':   card_has_rtd,
            })

        cards.sort(key=lambda x: x['oper_desc'])

        return {
            'success':     True,
            'error':       None,
            'cards':       cards,
            'fab_list':    fab_list,
            'lot_cd_list': lot_cd_list,
            'grp_list':    grp_list,
        }

    except Exception as e:
        import traceback
        return {
            'success':     False,
            'error':       str(e) + '\n' + traceback.format_exc(),
            'cards':       [],
            'fab_list':    [],
            'lot_cd_list': [],
            'grp_list':    [],
        }
    if rn_filter is not None: result = result[result['rn'] == rn_filter]
    return result
