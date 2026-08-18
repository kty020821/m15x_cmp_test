"""
equipment/link_service.py
════════════════════════════════════════════════════════════
연계 공정 조회 · 병합 (기준정보 v2 기반)

  config2 에 등록한 연계 공정을 타입별 쿼리로 조회해
  본공정 병합 테이블에 컬럼으로 붙인다.

────────────────────────────────────────────────────────────
타입별 조회 대상

  SRC  tas_src_wf_metr_inf   다른 공정의 측정값
  REP  tas_rep_wf_metr_inf   Response 계측
  DEF  tas_dft_wf_inf        Defect 계측

★ 세 쿼리 모두 alias_lot_id 와 wf_id 를 돌려준다.
  그 둘로 만든 키(alias_lot_id + '.' + 0패딩 wf_id)가 조인 키다.
  alias_lot_id 는 최초 lot_id 라 층이 바뀌어도 불변이다.

★ wf_id 는 1~9 가 '01'~'09' 로 0 이 붙는다.
  중간에 정수로 바뀌면 'TA1234A.1' 이 되어 'TA1234A.01' 과 안 붙고,
  에러 없이 값이 전부 NULL 로만 나온다. 양쪽 모두 wafer_key() 로 만든다.

★ mt 는 언제나 between 이다. 청크로 쪼개지 않는다 —
  청크마다 mt 범위가 달라져 기간 끝쪽 월이 빠지는 사고가 있었다.

★ 기존 analysis_service 는 건드리지 않는다.
  운영 중인 경로이므로 여기서 따로 붙이고, 검증이 끝나면 합친다.
════════════════════════════════════════════════════════════
"""

from datetime import date, timedelta

import pandas as pd

from . import config2_service as cfg2

VERBOSE = True

# ── 조회 SQL ──────────────────────────────────────────────
#   ★ 조건절 컬럼 이름은 손대지 않는다. 채우는 자리는
#     mt_s / mt_e / lot_cd / link_id / params 뿐이다.
SQL_SRC = """
select alias_lot_id, wf_id, param_nm, meas_val, end_tm,
       RANK() over(partition by lot_id, wf_id, param_nm
                   order by end_tm ASC) r
from lake_catalog.tas.tas_src_wf_metr_inf
where mt between '{mt_s}' and '{mt_e}'
  and lot_cd = '{lot_cd}'
  and oper_id = '{link_id}'
  and param_nm in ({params})
"""

SQL_REP = """
select alias_lot_id, wf_id, param_nm, meas_val, end_tm
from lake_catalog.tas.tas_rep_wf_metr_inf
where mt between '{mt_s}' and '{mt_e}'
  and lot_cd = '{lot_cd}'
  and oper_id = '{link_id}'
  and param_nm in ({params})
"""

SQL_DEF = """
select alias_lot_id, wf_id, defect_class_nm, meas_defect_cnt, end_tm
from lake_catalog.tas.tas_dft_wf_inf
where mt between '{mt_s}' and '{mt_e}'
  and lot_cd = '{lot_cd}'
  and step_id = '{link_id}'
  and defect_class_nm in ({params})
"""

# ── chamber: 장비·챔버 이력 ───────────────────────────────
#   ★ chamber 정보는 SRC(측정값) 테이블에 없다.
#     기존 SRC 쿼리가 left join 하던 wafer-history 에서 온다.
#   ★ 이 apc_sk_wafer_hst_r2r_all_* 은 R2R 계산 이력을 담는
#     기존 APC 테이블(apc_sk_r2r_*)과 전혀 다른 테이블이다. 혼동 금지.
#   ★ fab 별로 테이블이 나뉘어 있어 UNION 해야 한다.
#   ★ dt 로 파티션을 좁힌다 — 여기만 mt 가 아니라 dt(YYYYMMDD)다.
#   ★ resource_type='INDEPENDENT' 는 반드시 있어야 한다.
#     빼면 같은 웨이퍼가 여러 행으로 늘어나 조인 시 행이 뻥튀기된다.
CHM_UNITS = ('m10', 'm11', 'm14', 'm15')

SQL_CHM_UNIT = """        select lot_id, wf_id, eqp_id, module_id,
               MAX(last_update_dtts) as last_update_dtts
        from lake_catalog.apc.apc_sk_wafer_hst_r2r_all_{unit}
        where dt between '{dt_s}' and '{dt_e}'
          and operation_id like '{oper_pfx}%'
          and resource_type = 'INDEPENDENT'
        group by lot_id, wf_id, eqp_id, module_id"""

# 공정별 module_id 제한 (기존 SRC 쿼리에서 그대로 가져옴)
#   챔버 번호 체계가 공정마다 달라, 해당하는 것만 남긴다.
CHM_MODULE_FILTER = {
    'V5071000B': ('2', '3'),
    'X106100B':  ('2', '3'),
    'T5515000C': ('2', '3'),
    'T5515000M': ('2', '3', '5'),
    'T5515000A': ('2', '3', '5'),
}


def build_chm_sql(link_id, dt_s, dt_e):
    """
    장비·챔버 이력 조회 SQL.

    ★ operation_id 는 like 접두 매칭이다 — 기존 SRC 쿼리가
      pre_oper[:-1] 로 마지막 한 글자를 떼고 like 를 걸었다.
      끝자리가 리비전이라 그것까지 맞추면 안 잡힌다.
    """
    pfx = str(link_id or '')[:-1] if link_id else ''
    body = "\n        union\n".join(
        SQL_CHM_UNIT.format(unit=u, dt_s=dt_s, dt_e=dt_e, oper_pfx=pfx)
        for u in CHM_UNITS)

    mods = CHM_MODULE_FILTER.get(str(link_id or '').upper())
    where = ''
    if mods:
        cond = " or ".join(f"h.module_id = '{m}'" for m in mods)
        where = f"\nwhere ( {cond} )"

    return f"""
select h.lot_id, h.wf_id, h.eqp_id, h.module_id, h.last_update_dtts
from (
{body}
) h{where}
"""


SQL = {'SRC': SQL_SRC, 'REP': SQL_REP, 'DEF': SQL_DEF}

# 조회 후 컬럼 이름 통일 — 뒤 단계를 하나로 쓰기 위한 것
RENAME = {
    'SRC': {'param_nm': 'param', 'meas_val': 'value'},
    'REP': {'param_nm': 'param', 'meas_val': 'value'},
    'DEF': {'defect_class_nm': 'param', 'meas_defect_cnt': 'value'},
}

# SRC 연계는 rework 전 값을 쓴다 (본공정과 같은 기준)
SRC_PICK_FIRST = True


def _month_range(days=30, date_from=None, date_to=None):
    """조회 기간을 덮는 mt(YYYYMM) 시작·끝"""
    if date_from or date_to:
        d2 = pd.to_datetime(date_to).date() if date_to else date.today()
        d1 = (pd.to_datetime(date_from).date() if date_from
              else d2 - timedelta(days=days))
        if d1 > d2:
            d1, d2 = d2, d1
    else:
        d2 = date.today()
        d1 = d2 - timedelta(days=days)
    return d1.strftime('%Y%m'), d2.strftime('%Y%m')


def _day_range(days=30, date_from=None, date_to=None):
    """dt 파티션용 시작·끝 (YYYYMMDD) — 챔버 이력은 mt 가 아니라 dt 로 자른다"""
    if date_from or date_to:
        d2 = pd.to_datetime(date_to).date() if date_to else date.today()
        d1 = (pd.to_datetime(date_from).date() if date_from
              else d2 - timedelta(days=days))
        if d1 > d2:
            d1, d2 = d2, d1
    else:
        d2 = date.today()
        d1 = d2 - timedelta(days=days)
    return d1.strftime('%Y%m%d'), d2.strftime('%Y%m%d')


def _quote_list(vals):
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in vals)


def preview_sql(oper_id, scope=None, days=30, date_from=None, date_to=None,
                lot_cds=None):
    """
    실행하지 않고 던질 쿼리만 만든다 (Lake 에 붙여넣어 확인용).
    """
    mt_s, mt_e = _month_range(days, date_from, date_to)
    dt_s, dt_e = _day_range(days, date_from, date_to)
    base_lots = [str(v).upper() for v in (lot_cds or []) if str(v).strip()]
    if not base_lots:
        base_lots = [str(v).upper() for v in cfg2.lots_of(oper_id) if v]
    if not base_lots:
        base_lots = ['<LOT_CD 미등록>']

    out = []
    for lk in cfg2.links_of(oper_id, scope):
        lots = [str(v).upper() for v in (lk.get('lot_cds') or []) if v] \
               or base_lots

        if lk.get('want_chm'):
            out.append({
                'kind': 'CHAMBER', 'alias': lk['alias'],
                'link_id': lk['link_id'], 'lot_cd': '(해당 없음)',
                'query': build_chm_sql(lk['link_id'], dt_s, dt_e).strip(),
            })

        params = [p for p in lk['params'] if p]
        if not params:
            continue
        for lot_cd in lots:
            out.append({
                'kind': lk['kind'], 'alias': lk['alias'],
                'link_id': lk['link_id'], 'lot_cd': lot_cd,
                'query': SQL[lk['kind']].format(
                    mt_s=mt_s, mt_e=mt_e, lot_cd=lot_cd,
                    link_id=lk['link_id'],
                    params=_quote_list(params)).strip(),
            })
    return out


# ══════════════════════════════════════════════════════════
# 조회
# ══════════════════════════════════════════════════════════
def fetch_link(lake, link, lot_cds, mt_s, mt_e, run_query, on_progress=None):
    """
    연계 공정 1건 조회 (long).

    반환 컬럼: wkey, param, value, end_tm
    등록 파라미터가 없으면 빈 DataFrame.
    """
    kind = link['kind']
    params = [p for p in link['params'] if p]
    if not params:
        if VERBOSE:
            print(f"  [{kind}:{link['alias']}] 관리 PARAM 없음 — 조회 생략")
        return pd.DataFrame()

    sql = SQL[kind]
    p_in = _quote_list(params)
    dfs, done = [], 0
    total = max(1, len(lot_cds))

    for lot_cd in lot_cds:
        query = sql.format(mt_s=mt_s, mt_e=mt_e, lot_cd=lot_cd,
                           link_id=link['link_id'], params=p_in)
        if done == 0 and VERBOSE:
            print(f"\n─── [{kind}:{link['alias']}] 실행 쿼리 " + '─' * 30)
            print(query.strip())
            print('─' * 58)
        try:
            d = run_query(lake, query)
        except Exception:
            print(f"\n[{kind}:{link['alias']}] 조회 실패 — 아래 쿼리를 확인하세요")
            print(query.strip())
            raise

        if d is not None and not d.empty:
            dfs.append(d)

        done += 1
        n = sum(len(x) for x in dfs)
        if on_progress:
            on_progress(done, total,
                        f"{kind}:{link['alias']} {lot_cd} · {n:,}행")
        if VERBOSE:
            print(f"  [{kind}:{link['alias']}] {done}/{total} {lot_cd} "
                  f"mt {mt_s}~{mt_e} 누적 {n:,}행")

    if not dfs:
        return pd.DataFrame()

    out = pd.concat(dfs, ignore_index=True)
    out.columns = out.columns.str.lower()
    out = out.rename(columns=RENAME[kind])

    # SRC 는 같은 웨이퍼·파라미터에 측정이 여러 번 쌓인다 (rework)
    #   본공정과 같은 기준으로 첫 측정만 남긴다
    if kind == 'SRC' and 'r' in out.columns and SRC_PICK_FIRST:
        out = out[out['r'] == 1].drop(columns=['r'])
    elif 'r' in out.columns:
        out = out.drop(columns=['r'])

    out['wkey'] = [cfg2.wafer_key(a, w)
                   for a, w in zip(out['alias_lot_id'], out['wf_id'])]
    return out.drop_duplicates(subset=['wkey', 'param'], keep='first')


def fetch_chm(lake, link, dt_s, dt_e, run_query, on_progress=None):
    """
    연계 공정의 장비·챔버 이력 조회.

    반환 컬럼: wkey, eqp_id, module_id
    ★ SRC 와 달리 lot_cd 로 거르지 않는다 — 이 테이블에는 lot_cd 가 없고
      operation_id 접두와 dt 로만 좁힌다.
    """
    query = build_chm_sql(link['link_id'], dt_s, dt_e)
    if VERBOSE:
        print(f"\n─── [CHM:{link['alias']}] 실행 쿼리 " + '─' * 30)
        print(query.strip())
        print('─' * 58)
    try:
        d = run_query(lake, query)
    except Exception:
        print(f"\n[CHM:{link['alias']}] 조회 실패 — 아래 쿼리를 확인하세요")
        print(query.strip())
        raise

    if d is None or d.empty:
        return pd.DataFrame()

    d = d.copy()
    d.columns = d.columns.str.lower()

    # 같은 웨이퍼에 이력이 여러 건이면 가장 나중 것만 (마지막 처리 장비)
    if 'last_update_dtts' in d.columns:
        d = (d.sort_values('last_update_dtts')
               .drop_duplicates(subset=['lot_id', 'wf_id'], keep='last'))

    d['wkey'] = [cfg2.wafer_key(a, w)
                 for a, w in zip(d['lot_id'], d['wf_id'])]
    if on_progress:
        on_progress(1, 1, f"CHM:{link['alias']} · {len(d):,}행")
    if VERBOSE:
        print(f"  [CHM:{link['alias']}] {len(d):,}행")
    return d[['wkey', 'eqp_id', 'module_id']]


def pivot_chm(df, link):
    """
    장비·챔버를 컬럼으로.
      <별칭>_EQP  장비
      <별칭>_CH   챔버(module_id)

    ★ 값이 아니라 라벨이므로 <KIND>_ 접두어를 붙이지 않는다.
      차트 범례·그룹 기준으로 쓰이는 값이라 이름이 짧은 편이 낫다.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    a = cfg2.slug(link['alias'])
    return (df.rename(columns={'eqp_id': f'{a}_EQP',
                               'module_id': f'{a}_CH'})
              .drop_duplicates(subset=['wkey'], keep='last'))


def pivot_link(df, link):
    """
    long → wide.
      값 컬럼 : <KIND>_<별칭>_<PARAM>
      시각    : <KIND>_<별칭>_DATE (웨이퍼별 마지막 계측 시각)
    """
    if df is None or df.empty:
        return pd.DataFrame()

    d = df.copy()
    d['__col'] = [cfg2.link_column(link['kind'], link['alias'], p)
                  for p in d['param']]
    d = d[d['__col'] != '']
    if d.empty:
        return pd.DataFrame()

    d['value'] = pd.to_numeric(d['value'], errors='coerce')
    wide = d.pivot_table(index='wkey', columns='__col', values='value',
                         aggfunc='last').reset_index()
    wide.columns.name = None

    if 'end_tm' in d.columns:
        tcol = f"{link['kind']}_{cfg2.slug(link['alias'])}_DATE"
        tm = (d.groupby('wkey', as_index=False)['end_tm'].max()
                .rename(columns={'end_tm': tcol}))
        wide = wide.merge(tm, on='wkey', how='left')
    return wide


def _base_key(base):
    """
    본공정 테이블에 조인 키를 만든다.

    ★ SUBSTRATE_ID 가 이미 'alias_lot_id.wf_id' 형태지만, 그 wf_id 의
      0 패딩이 유지됐다는 보장이 없다. 여기서 다시 정규화해
      연계 쪽 키와 형태를 맞춘다.
    """
    cols = {c.upper(): c for c in base.columns}

    if 'SUBSTRATE_ID' in cols:
        sid = base[cols['SUBSTRATE_ID']].astype(str)
        lot = sid.str.rsplit('.', n=1).str[0]
        wf = sid.str.rsplit('.', n=1).str[-1]
        return [cfg2.wafer_key(a, w) for a, w in zip(lot, wf)]

    if 'LOT_ID' in cols and 'WF_ID' in cols:
        return [cfg2.wafer_key(a, w)
                for a, w in zip(base[cols['LOT_ID']], base[cols['WF_ID']])]
    return None


def merge_links(base, lake, oper_id, run_query, scope=None, days=30,
                date_from=None, date_to=None, lot_cds=None, on_progress=None):
    """
    본공정 병합 테이블에 연계 공정 컬럼을 붙인다.

    ★ 항상 left join — 연계 계측은 표본이라 값이 없는 웨이퍼가 정상이다.
      inner 로 붙이면 그 웨이퍼가 통째로 사라진다.
    ★ 등록이 없으면 base 를 그대로 돌려준다.
    """
    if base is None or base.empty:
        return base

    links = cfg2.links_of(oper_id, scope)
    if not links:
        if VERBOSE:
            print(f'  [link] {oper_id}: 등록된 연계 공정 없음 — 건너뜀')
        return base

    key = _base_key(base)
    if key is None:
        print('[link] SUBSTRATE_ID / LOT_ID+WF_ID 가 없어 연계를 붙이지 '
              f'못했습니다 — base 컬럼: {list(base.columns)[:12]}')
        return base

    out = base.copy()
    out['__wkey'] = key

    mt_s, mt_e = _month_range(days, date_from, date_to)
    dt_s, dt_e = _day_range(days, date_from, date_to)
    # ── 조회할 LOT_CD ────────────────────────────────────
    #   ★ 기준정보에 등록된 device 만 쓴다.
    #     적재 테이블에서 긁어오면 샘플 랏(S5C 등) 처럼 등록하지 않은
    #     device 가 섞여 의도치 않은 쿼리가 나간다.
    base_lots = [str(v).upper() for v in (lot_cds or []) if str(v).strip()]
    if not base_lots:
        base_lots = [str(v).upper() for v in cfg2.lots_of(oper_id) if v]
    if not base_lots:
        # 기준정보에 device 가 없으면 조회할 대상이 없다.
        #   적재 데이터로 대신하지 않는다 — 그게 S5C 가 들어온 경로였다.
        print(f'[link] {oper_id}: 기준정보에 등록된 LOT_CD 가 없어 '
              f'연계 조회를 건너뜁니다 (config2 에서 device 를 등록하세요)')
        return base

    for lk in links:
        # ★ 연계 공정은 본공정과 device 코드가 다를 수 있다.
        #   기준정보에 LOT_CD 를 적었으면 그것만, 비웠으면 본공정 것을 쓴다.
        lots = [str(v).upper() for v in (lk.get('lot_cds') or []) if v] \
               or base_lots

        # ★ 한 연계 공정이 두 가지를 함께 가져올 수 있다.
        #   param 이 chamber 면 장비·챔버 이력에서, 그 외 param 은
        #   측정값 테이블에서. 기존 SRC 쿼리가 left join 으로 붙여 두던
        #   두 출처를 그대로 나눈 것이다.
        jobs = []
        if lk.get('want_chm'):
            jobs.append('CHM')
        if lk.get('params'):
            jobs.append(lk['kind'])

        for tag in jobs:
            if tag == 'CHM':
                wide = pivot_chm(
                    fetch_chm(lake, lk, dt_s, dt_e, run_query,
                              on_progress=on_progress), lk)
            else:
                wide = pivot_link(
                    fetch_link(lake, lk, lots, mt_s, mt_e, run_query,
                               on_progress=on_progress), lk)
            if wide is None or wide.empty:
                if VERBOSE:
                    print(f"  [{tag}:{lk['alias']}] 결과 없음")
                continue

            wide = wide.rename(columns={'wkey': '__wkey'})
            before = set(out.columns)
            out = out.merge(wide, on='__wkey', how='left')
            added = [c for c in out.columns if c not in before]
            if VERBOSE and added:
                hit = int(out[added[0]].notna().sum())
                print(f"  [{tag}:{lk['alias']}] 컬럼 {len(added)}개 추가 · "
                      f"값 있는 웨이퍼 {hit:,}장")
                if hit == 0:
                    print('    값이 하나도 안 붙었습니다 — 조인 키'
                          '(wf_id 0 패딩)를 확인하세요')

    return out.drop(columns=['__wkey'])
