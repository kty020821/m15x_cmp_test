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


def get_rtd_data(df):
    if df.empty:
        return {'success': False, 'error': '데이터 없음', 'cards': [],
                'fab_list': [], 'lot_cd_list': [], 'grp_list': []}
    try:
        fab_list    = sorted(df['FAB'].dropna().unique().tolist())
        lot_cd_list = sorted(df['LOT_CD'].dropna().unique().tolist())
        grp_list    = sorted(df['EQP_OPER_GRP_CD'].dropna().unique().tolist())

        def get_eq_type(model):
            m = str(model).upper()
            if 'OPTA' in m:    return 'OPTA'
            if 'F_REX' in m or 'F-REX' in m or 'FREX' in m: return 'FREX'
            if 'ELASTIC' in m: return 'ELASTIC'
            if 'KCT' in m:     return 'KCT'
            return 'NORMAL'

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

        grp_total_cache = {grp: count_total_eq_in_grp(gdf) for grp, gdf in df.groupby('EQP_OPER_GRP_CD')}

        cards = []
        for keys, oper_df in df.groupby(['OPER_DESC', 'LOT_CD', 'FLOW_ID', 'EQP_OPER_GRP_CD']):
            oper_desc, lot_cd, flow_id, grp = keys
            eq_type = get_eq_type(oper_df['EQP_MODEL_NM'].iloc[0])

            if eq_type == 'OPTA':
                prefix_map = {}
                for _, row in oper_df.iterrows():
                    s = str(row['EQP_ID'])
                    prefix_map.setdefault(s.split('_P')[0] if '_P' in s else s, []).append(row)
                units = []
                for prefix, rows in prefix_map.items():
                    rdf = pd.DataFrame(rows)
                    is_rtd = (rdf['RTD'].str.upper() == 'Y').any()
                    dr = rdf[rdf['RTD'].str.upper() == 'Y'] if is_rtd else rdf.head(1)
                    units.append({'unit_id': prefix, 'is_rtd': is_rtd, 'detail_rows': dr.to_dict('records')})
            elif eq_type in ('FREX', 'ELASTIC', 'KCT'):
                ch_map = {}
                for _, row in oper_df.iterrows():
                    s = str(row['EQP_ID'])
                    if '_' in s: ch_map.setdefault(s, []).append(row)
                units = []
                for ch_id, rows in ch_map.items():
                    rdf = pd.DataFrame(rows)
                    is_rtd = (rdf['RTD'].str.upper() == 'Y').any()
                    dr = rdf[rdf['RTD'].str.upper() == 'Y'] if is_rtd else rdf.head(1)
                    units.append({'unit_id': ch_id, 'is_rtd': is_rtd, 'detail_rows': dr.to_dict('records')})
            else:
                base_map = {}
                for _, row in oper_df.iterrows():
                    s = str(row['EQP_ID'])
                    base_map.setdefault(s if '_' not in s else s.split('_')[0], []).append(row)
                units = []
                for eq_id, rows in base_map.items():
                    rdf = pd.DataFrame(rows)
                    is_rtd = (rdf['RTD'].str.upper() == 'Y').any()
                    dr = rdf[rdf['RTD'].str.upper() == 'Y'] if is_rtd else rdf.head(1)
                    units.append({'unit_id': eq_id, 'is_rtd': is_rtd, 'detail_rows': dr.to_dict('records')})

            rtd_count   = sum(1 for u in units if u['is_rtd'])
            total_in_grp = grp_total_cache.get(grp, len(units))
            detail_all = []
            for u in units:
                for r in u['detail_rows']:
                    detail_all.append({
                        'EQP_ID': r.get('EQP_ID',''), 'LOT_CD': r.get('LOT_CD',''),
                        'FLOW_ID': r.get('FLOW_ID',''), 'RTD': r.get('RTD',''),
                        'RTD_USER_NM': r.get('RTD_USER_NM',''), 'RTD_TM': str(r.get('RTD_TM','')),
                        'RTD_DESC': r.get('RTD_DESC',''),
                    })
            cards.append({
                'fab': oper_df['FAB'].iloc[0] if 'FAB' in oper_df.columns else '',
                'lot_cd': lot_cd, 'flow_id': flow_id, 'oper_desc': oper_desc, 'grp': grp,
                'eq_type': eq_type, 'total': total_in_grp, 'avail': total_in_grp - rtd_count,
                'rtd': rtd_count, 'units': units, 'detail_all': detail_all,
            })

        cards.sort(key=lambda x: (x['oper_desc'], x['lot_cd']))
        return {'success': True, 'error': None, 'cards': cards,
                'fab_list': fab_list, 'lot_cd_list': lot_cd_list, 'grp_list': grp_list}
    except Exception as e:
        import traceback
        return {'success': False, 'error': str(e) + '\n' + traceback.format_exc(),
                'cards': [], 'fab_list': [], 'lot_cd_list': [], 'grp_list': []}


DATA_DIR = Path(__file__).resolve().parent.parent / 'data' / 'inline'
DATA_DIR.mkdir(parents=True, exist_ok=True)
inline_url = ''

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
    if rn_filter is not None: result = result[result['rn'] == rn_filter]
    return result
