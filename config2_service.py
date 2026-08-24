"""
equipment/config2_service.py
════════════════════════════════════════════════════════════
기준정보 v2 — 연계 공정 다중 등록

  기존 config_service 는 그대로 둔다. 운영 중이므로 건드리지 않고
  새 테이블(cmp_cfg2_*)로 따로 쌓는다. 검증이 끝나면 그때 전환한다.

────────────────────────────────────────────────────────────
무엇이 달라졌나

  · 연계 공정을 최대 3개까지 등록한다 (기존은 사전공정 1개)
  · 연계 공정마다 타입을 지정한다 — SRC / REP / DEF
    타입이 곧 조회 쿼리를 정한다
  · 연계 공정마다 용도를 지정한다 — 모니터링 / 분석 / 둘 다
    정기 적재는 무거워지면 안 되므로 '모니터링·둘다' 만 매일 붙이고,
    '분석' 은 1회성 조회에서만 붙인다
  · 별칭(alias)으로 컬럼 이름을 짧게 유지한다

★ 컬럼 이름: <KIND>_<별칭>_<PARAM>
  예) SRC_M1CU_CU_THK_AVG · REP_ADICD_THK · DEF_AEI_SCRATCH
  OPER_ID 를 그대로 쓰면 SRC_V5071000B_CU_THK_AVG 처럼 길어져
  차트 범례와 LLM 프롬프트가 지저분해진다.

★ 조인 키: alias_lot_id + '.' + wf_id
  alias_lot_id 는 최초 lot_id 라 층이 바뀌어도 불변이다.
  wf_id 는 1~9 가 '01'~'09' 로 0 이 붙으므로 반드시 문자열로 다뤄야 한다 —
  정수로 바뀌면 'TA1234A.1' 이 되어 'TA1234A.01' 과 조용히 안 붙는다.
════════════════════════════════════════════════════════════
"""

import re

import pandas as pd
from django.db import connections

from . import param_types as pt

T_OPER  = 'cmp_cfg2_oper'
T_LOT   = 'cmp_cfg2_lot'
T_PARAM = 'cmp_cfg2_param'
T_LINK  = 'cmp_cfg2_link'

# 연계 공정 최대 개수 (별칭 기준)
MAX_LINKS = 3

KINDS  = ['SRC', 'REP', 'DEF']
SCOPES = ['both', 'mon', 'ana']

KIND_LABEL = {'SRC': 'SRC (측정값)', 'REP': 'REP (Response)',
              'DEF': 'DEF (Defect)'}
SCOPE_LABEL = {'both': '둘 다', 'mon': '모니터링', 'ana': '분석'}

# 웨이퍼 번호 자릿수 — '01'~'09' 로 0 이 붙는다
WF_PAD = 2


def _conn():
    return connections['analysis_db']


def _s(v):
    return str(v or '').strip()


def _up(v):
    return _s(v).upper()


def alias_from(desc, fallback_id):
    """
    연계 공정의 별칭(영문 공정명)을 만든다.

    ★ oper_desc 가 한글이면 slug 가 전부 지워 빈 별칭이 된다.
      그러면 컬럼 이름이 SRC__THK 처럼 망가진다.
    ★ 한글이 섞인 경우도 문제다 — 'M1 폴리 연마' 가 'M1' 로 잘려
      무슨 공정인지 알 수 없게 된다. 영문이 절반도 안 남으면
      공정 ID 를 쓰는 편이 낫다.
    """
    raw = _s(desc)
    a = slug(raw)[:20].strip('_')

    if not a or not any(c.isalpha() for c in a):
        return slug(fallback_id)[:20]

    # 원문에서 영숫자·공백이 아닌 글자(한글 등)의 비중
    drop = sum(1 for c in raw if not (c.isascii() and (c.isalnum() or c in ' _-')))
    if raw and drop / len(raw) > 0.3:
        return slug(fallback_id)[:20]
    return a


def slug(v):
    """컬럼명에 쓸 수 있게 — 영숫자·_ 만 남기고 대문자로"""
    out = re.sub(r'[^0-9A-Za-z_]+', '_', _s(v).upper())
    return re.sub(r'_+', '_', out).strip('_')


def wafer_key(alias_lot_id, wf_id):
    """
    병합 키. wf_id 는 반드시 0 패딩된 문자열로 만든다.

    ★ Lake 가 '01' 로 주더라도 중간에 정수로 바뀌면 '1' 이 되어
      다른 데이터와 안 붙는다. 여기서 형태를 고정한다.
    """
    w = _s(wf_id)
    if w.isdigit():
        w = w.zfill(WF_PAD)
    return f'{_s(alias_lot_id)}.{w}'


# ── chamber 파라미터 ──────────────────────────────────────
#   ★ 조회처를 가르는 기준은 '타입' 이 아니라 'param 이 chamber 인가' 다.
#     같은 사전공정이라도
#       param = CHAMBER  → apc_sk_wafer_hst_r2r_all_* (장비·챔버 이력)
#       param = 그 외    → tas_src_wf_metr_inf        (측정값)
#     기존 SRC 쿼리에서 left join 으로 붙어 있던 두 출처를 그대로 나눈 것이다.
CHAMBER_PARAMS = {'CHAMBER', 'CH', 'CHM', 'EQP_CH', 'MODULE', 'MODULE_ID',
                  '챔버'}


def is_chamber_param(param):
    return slug(param).upper() in CHAMBER_PARAMS or not slug(param)


def chm_columns(alias):
    """chamber 가 만드는 컬럼 두 개 — 장비와 챔버"""
    a = slug(alias)
    return [f'{a}_EQP', f'{a}_CH'] if a else []


def link_column(kind, alias, param):
    """
    연계 공정 '측정값' 컬럼 이름.
    예) link_column('SRC', 'M1CU', 'CU_THK_AVG') -> 'SRC_M1CU_CU_THK_AVG'

    ★ 규칙의 단일 소재지다.
    ★ chamber 는 여기서 다루지 않는다 — chm_columns() 가 맡는다.
      chamber param 이면 빈 문자열을 돌려주므로, 부르는 쪽은
      is_chamber_param() 으로 먼저 갈라야 한다.
    """
    k = _up(kind) if _up(kind) in KINDS else 'SRC'
    a, p = slug(alias), slug(param)
    if is_chamber_param(param) or not p:
        return ''
    return f'{k}_{a}_{p}' if a else f'{k}_{p}'


# ══════════════════════════════════════════════════════════
# 테이블
# ══════════════════════════════════════════════════════════
def ensure_tables():
    with _conn().cursor() as cur:
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_OPER} (
              oper_id   VARCHAR(100) PRIMARY KEY,
              oper_desc VARCHAR(200),
              fab       VARCHAR(50),
              eq_model  VARCHAR(50),
              use_yn    VARCHAR(1) DEFAULT 'Y',
              updated_by VARCHAR(100),
              updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_LOT} (
              id BIGSERIAL PRIMARY KEY,
              oper_id   VARCHAR(100),
              lot_cd    VARCHAR(50),
              recipe_id VARCHAR(200),
              use_yn    VARCHAR(1) DEFAULT 'Y'
            )
        ''')
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_PARAM} (
              id BIGSERIAL PRIMARY KEY,
              oper_id    VARCHAR(100),
              param      VARCHAR(100),
              param_type VARCHAR(20),
              use_yn     VARCHAR(1) DEFAULT 'Y'
            )
        ''')
        # 연계 공정 — 파라미터 1개당 1행
        #   같은 별칭이 여러 행에 반복되지만, 그래야 붙여넣기와 행 복제가
        #   자연스럽고 파라미터마다 사용 여부를 따로 끌 수 있다.
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_LINK} (
              id BIGSERIAL PRIMARY KEY,
              oper_id  VARCHAR(100),
              seq      INTEGER DEFAULT 1,
              kind     VARCHAR(10),
              alias    VARCHAR(50),
              link_id  VARCHAR(100),
              lot_cd   VARCHAR(50),
              param    VARCHAR(100),
              scope    VARCHAR(10) DEFAULT 'both',
              use_yn   VARCHAR(1) DEFAULT 'Y'
            )
        ''')
        # 이미 만들어진 테이블에는 lot_cd 가 없다 — 있으면 두고 없으면 추가
        cur.execute(f'ALTER TABLE {T_LINK} '
                    f'ADD COLUMN IF NOT EXISTS lot_cd VARCHAR(50)')

        for t in (T_LOT, T_PARAM, T_LINK):
            cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{t}_oper '
                        f'ON {t} (oper_id)')


# ══════════════════════════════════════════════════════════
# 조회
# ══════════════════════════════════════════════════════════
def list_opers():
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT o.oper_id, o.oper_desc, o.fab, o.eq_model, o.use_yn,
                   (SELECT COUNT(*) FROM {T_LOT}   l WHERE l.oper_id = o.oper_id),
                   (SELECT COUNT(*) FROM {T_PARAM} p WHERE p.oper_id = o.oper_id),
                   (SELECT COUNT(DISTINCT alias) FROM {T_LINK} k
                     WHERE k.oper_id = o.oper_id),
                   (SELECT COUNT(*) FROM {T_LINK} k WHERE k.oper_id = o.oper_id)
            FROM {T_OPER} o
            ORDER BY o.oper_desc NULLS LAST, o.oper_id
        ''')
        return [{'oper_id': r[0], 'oper_desc': r[1] or '', 'fab': r[2] or '',
                 'eq_model': r[3] or '', 'use_yn': r[4] or 'Y',
                 'n_lot': r[5], 'n_param': r[6],
                 'n_link': r[7], 'n_link_param': r[8]}
                for r in cur.fetchall()]


def get_oper(oper_id):
    ensure_tables()
    oper_id = _up(oper_id)
    with _conn().cursor() as cur:
        cur.execute(f'SELECT oper_id, oper_desc, fab, eq_model, use_yn '
                    f'FROM {T_OPER} WHERE oper_id = %s', [oper_id])
        row = cur.fetchone()
        if not row:
            return None

        cur.execute(f'SELECT lot_cd, recipe_id, use_yn FROM {T_LOT} '
                    f'WHERE oper_id = %s ORDER BY lot_cd, recipe_id', [oper_id])
        lots = [{'lot_cd': r[0] or '', 'recipe_id': r[1] or '',
                 'use_yn': r[2] or 'Y'} for r in cur.fetchall()]

        cur.execute(f'SELECT param, param_type, use_yn FROM {T_PARAM} '
                    f'WHERE oper_id = %s ORDER BY param', [oper_id])
        params = [{'param': r[0] or '', 'param_type': r[1] or '',
                   'use_yn': r[2] or 'Y'} for r in cur.fetchall()]

        cur.execute(f'''
            SELECT seq, kind, alias, link_id, lot_cd, param, scope, use_yn
            FROM {T_LINK} WHERE oper_id = %s
            ORDER BY seq, alias, param
        ''', [oper_id])
        links = [{'seq': r[0] or 1, 'kind': r[1] or 'SRC', 'alias': r[2] or '',
                  'link_id': r[3] or '', 'lot_cd': r[4] or '',
                  'param': r[5] or '', 'scope': r[6] or 'both',
                  'use_yn': r[7] or 'Y'}
                 for r in cur.fetchall()]

    return {'oper_id': row[0], 'oper_desc': row[1] or '', 'fab': row[2] or '',
            'eq_model': row[3] or '', 'use_yn': row[4] or 'Y',
            'lots': lots, 'params': params, 'links': links}


# ══════════════════════════════════════════════════════════
# 저장
# ══════════════════════════════════════════════════════════
def save_oper(d, user=''):
    """
    공정 1건 저장 (전체 교체).

    ★ 저장 전 검증에서 걸린 것은 저장하지 않는다 —
      기존 v1 에서 화면이 payload 를 빠뜨려 '지우기만 하고 안 넣는'
      사고가 있었다. 여기서는 빈 목록도 명시적으로 확인한다.
    """
    ensure_tables()
    oper_id = _up(d.get('oper_id'))
    if not oper_id:
        raise ValueError('OPER_ID 를 입력하세요')
    if not re.match(r'^[0-9A-Za-z_\-]+$', oper_id):
        raise ValueError('OPER_ID 에 사용할 수 없는 문자가 있습니다')

    lots, lseen = [], set()
    for it in d.get('lots') or []:
        lot = _up(it.get('lot_cd'))
        if not lot:
            continue
        key = (lot, _up(it.get('recipe_id')))
        if key in lseen:
            continue
        lseen.add(key)
        lots.append((lot, _s(it.get('recipe_id')),
                     'N' if _up(it.get('use_yn')) == 'N' else 'Y'))

    params, pseen = [], set()
    for it in d.get('params') or []:
        p = _up(it.get('param'))
        if not p or p in pseen:
            continue
        pseen.add(p)
        ptype = _up(it.get('param_type'))
        params.append((p, ptype if ptype in pt.TYPES else '',
                       'N' if _up(it.get('use_yn')) == 'N' else 'Y'))

    links, kseen, aliases = [], set(), []
    for it in d.get('links') or []:
        kind = _up(it.get('kind'))
        if kind not in KINDS:
            kind = 'SRC'
        alias = slug(it.get('alias'))
        link_id = _up(it.get('link_id'))
        prm = _up(it.get('param'))
        # 연계 공정은 본공정과 device 코드가 다를 수 있다.
        #   비워 두면 본공정의 LOT_CD 를 그대로 쓴다.
        lot_cd = _up(it.get('lot_cd'))
        if not alias or not link_id:
            continue                     # 별칭·공정ID 는 필수
        if alias not in aliases:
            aliases.append(alias)
        key = (kind, alias, lot_cd, prm)
        if key in kseen:
            continue
        kseen.add(key)
        scope = _s(it.get('scope')).lower()
        links.append((aliases.index(alias) + 1, kind, alias, link_id, lot_cd,
                      prm, scope if scope in SCOPES else 'both',
                      'N' if _up(it.get('use_yn')) == 'N' else 'Y'))

    if len(aliases) > MAX_LINKS:
        raise ValueError(f'연계 공정은 최대 {MAX_LINKS}개입니다 '
                         f'(현재 {len(aliases)}개: {", ".join(aliases)})')

    # 같은 별칭에 타입이 둘이면 컬럼 규칙이 흔들린다.
    #   ★ chamber 행은 예외다 — 컬럼이 <별칭>_EQP / <별칭>_CH 라
    #     측정값 컬럼과 겹치지 않는다. '챔버도 보고 측정값도 보는' 경우가
    #     흔하므로 같은 별칭에 함께 두는 것이 정상이다.
    by_alias = {}
    for _, kind, alias, link_id, _lc, _p, _sc, _u in links:
        if is_chamber_param(_p):
            continue
        prev = by_alias.setdefault(alias, (kind, link_id))
        if prev != (kind, link_id):
            raise ValueError(f'별칭 {alias} 에 타입·공정ID 가 두 가지입니다 — '
                             f'{prev[0]}/{prev[1]} vs {kind}/{link_id}')

    with _conn().cursor() as cur:
        cur.execute(f'''
            INSERT INTO {T_OPER} (oper_id, oper_desc, fab, eq_model, use_yn,
                                  updated_by, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (oper_id) DO UPDATE SET
              oper_desc=EXCLUDED.oper_desc, fab=EXCLUDED.fab,
              eq_model=EXCLUDED.eq_model, use_yn=EXCLUDED.use_yn,
              updated_by=EXCLUDED.updated_by, updated_at=NOW()
        ''', [oper_id, _s(d.get('oper_desc')), _s(d.get('fab')).lower(),
              _up(d.get('eq_model')),
              'N' if _up(d.get('use_yn')) == 'N' else 'Y', str(user)[:100]])

        for t in (T_LOT, T_PARAM, T_LINK):
            cur.execute(f'DELETE FROM {t} WHERE oper_id = %s', [oper_id])

        for lot, rcp, use in lots:
            cur.execute(f'INSERT INTO {T_LOT} (oper_id, lot_cd, recipe_id, '
                        f'use_yn) VALUES (%s,%s,%s,%s)',
                        [oper_id, lot, rcp, use])
        for p, ptype, use in params:
            cur.execute(f'INSERT INTO {T_PARAM} (oper_id, param, param_type, '
                        f'use_yn) VALUES (%s,%s,%s,%s)',
                        [oper_id, p, ptype, use])
        for seq, kind, alias, link_id, lot_cd, prm, scope, use in links:
            cur.execute(f'INSERT INTO {T_LINK} (oper_id, seq, kind, alias, '
                        f'link_id, lot_cd, param, scope, use_yn) '
                        f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        [oper_id, seq, kind, alias, link_id, lot_cd, prm,
                         scope, use])

    return {'oper_id': oper_id, 'lots': len(lots), 'params': len(params),
            'links': len(aliases), 'link_params': len(links)}


def delete_oper(oper_id):
    ensure_tables()
    oper_id = _up(oper_id)
    with _conn().cursor() as cur:
        for t in (T_LOT, T_PARAM, T_LINK, T_OPER):
            cur.execute(f'DELETE FROM {t} WHERE oper_id = %s', [oper_id])
    return True


# ══════════════════════════════════════════════════════════
# 적재 파이프라인이 쓰는 형태
# ══════════════════════════════════════════════════════════
def lots_of(oper_id):
    """
    기준정보에 등록된 LOT_CD 목록.

    ★ 조회 조건은 반드시 여기서 나와야 한다.
      적재 테이블에서 LOT_CD 를 긁어오면 샘플 랏(S5C 등)처럼
      등록하지 않은 device 가 섞여 엉뚱한 쿼리가 나간다.
    """
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT lot_cd FROM {T_LOT}
            WHERE oper_id = %s AND COALESCE(use_yn,'Y') <> 'N'
              AND lot_cd IS NOT NULL AND lot_cd <> ''
            ORDER BY 1
        """, [_up(oper_id)])
        return [r[0] for r in cur.fetchall()]


def links_of(oper_id, scope=None):
    """
    연계 공정을 별칭 단위로 묶어 돌려준다.

      scope='mon'  모니터링 적재용 (mon + both)
      scope='ana'  분석용 (ana + both)
      None         전부

    반환: [{'kind','alias','link_id','scope','params':[...], 'columns':[...]}]

    ★ 묶는 키는 (타입, 별칭)이다. 별칭만으로 묶으면 안 된다 —
      사전공정처럼 같은 별칭에 chamber 와 측정값이 함께
      등록되는 경우, 먼저 온 행의 타입으로 전체가 굳어
      챔버를 부르는데 SRC 쿼리가 나간다.
    """
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT seq, kind, alias, link_id, lot_cd, param, scope
            FROM {T_LINK}
            WHERE oper_id = %s AND COALESCE(use_yn,'Y') <> 'N'
            ORDER BY seq, kind, alias, param
        ''', [_up(oper_id)])
        rows = cur.fetchall()

    out = {}
    for seq, kind, alias, link_id, lot_cd, prm, sc in rows:
        if scope and sc not in (scope, 'both'):
            continue
        # ★ 묶음 키에 타입을 넣지 않는다 — chamber 와 측정값이 같은
        #   연계 공정(같은 별칭·공정ID)에 함께 등록되는 것이 정상이고,
        #   조회처는 아래에서 param 으로 갈린다.
        key = (alias, link_id)
        o = out.setdefault(key, {'seq': seq, 'kind': kind, 'alias': alias,
                                 'link_id': link_id, 'scope': sc,
                                 'lot_cds': [], 'params': [], 'columns': [],
                                 'want_chm': False})
        # 지정된 LOT_CD 만 모은다. 하나도 없으면 본공정 것을 쓴다는 뜻
        if lot_cd and lot_cd not in o['lot_cds']:
            o['lot_cds'].append(lot_cd)
        # ★ 한 연계 공정 안에서 chamber 와 측정값을 함께 등록할 수 있다.
        #   param 이 chamber 계열이면 장비·챔버 이력에서,
        #   아니면 측정값 테이블에서 가져온다.
        if is_chamber_param(prm):
            if not o['want_chm']:
                o['want_chm'] = True
                o['columns'] = chm_columns(alias) + o['columns']
        elif prm and prm not in o['params']:
            o['params'].append(prm)
            o['columns'].append(link_column(kind, alias, prm))
    return sorted(out.values(), key=lambda x: x['seq'])


def build_config_df(include_unused=False):
    """
    본공정 기준정보를 평면 DF 로 (기존 파이프라인과 같은 형태).
    컬럼: FAB, LOT_CD, OPER_ID, OPER_DESC, EQ_MODEL, RECIPE_ID, PARAM, PARAM_TYPE
    """
    ensure_tables()
    cond = "" if include_unused else "WHERE COALESCE(use_yn,'Y') <> 'N'"
    with _conn().cursor() as cur:
        cur.execute(f"SELECT oper_id, oper_desc, fab, eq_model FROM {T_OPER} "
                    f"{cond}")
        opers = {r[0]: r for r in cur.fetchall()}
        cur.execute(f"SELECT oper_id, lot_cd, recipe_id FROM {T_LOT} {cond}")
        lots = cur.fetchall()
        cur.execute(f"SELECT oper_id, param, param_type FROM {T_PARAM} {cond}")
        params = cur.fetchall()

    rows = []
    for oid, lot_cd, recipe in lots:
        o = opers.get(oid)
        if not o:
            continue
        for poid, prm, ptype in params:
            if poid != oid:
                continue
            rows.append({
                'FAB': o[2] or '', 'LOT_CD': lot_cd or '', 'OPER_ID': oid,
                'OPER_DESC': o[1] or '', 'EQ_MODEL': o[3] or '',
                'RECIPE_ID': recipe or '', 'PARAM': prm or '',
                'PARAM_TYPE': pt.resolve(prm, ptype),
            })
    df = pd.DataFrame(rows, columns=[
        'FAB', 'LOT_CD', 'OPER_ID', 'OPER_DESC', 'EQ_MODEL', 'RECIPE_ID',
        'PARAM', 'PARAM_TYPE'])

    # ★ v1(config_service)과 컬럼을 맞춘다.
    #   적재 파이프라인(get_oper_cond)이 PRE_OPER_* 를 참조하므로
    #   없으면 v2 로만 등록한 공정에서 KeyError 가 난다.
    #   v2 는 사전공정을 '연계 공정' 으로 다루므로, 그중 첫 SRC 를
    #   대표로 채워 기존 경로가 그대로 돌아가게 한다.
    pre = _first_pre_oper()
    df['PRE_OPER_ID'] = df['OPER_ID'].map(
        lambda o: pre.get(o, ('', '', ''))[0])
    df['PRE_OPER_DESC'] = df['OPER_ID'].map(
        lambda o: pre.get(o, ('', '', ''))[1])
    df['PRE_OPER_PARAM'] = df['OPER_ID'].map(
        lambda o: pre.get(o, ('', '', ''))[2])
    return df


def _first_pre_oper():
    """
    공정별 대표 사전공정 — {oper_id: (link_id, alias, param)}

    ★ v2 는 연계 공정을 여러 개 두므로 '사전공정' 이라는 단일 개념이 없다.
      기존 파이프라인 호환을 위해 chamber 를 보는 첫 연계를 대표로 쓴다.
      실제 연계 조회는 link_service 가 따로 하므로 여기 값은
      기존 코드가 참조할 때만 쓰인다.
    """
    ensure_tables()
    out = {}
    try:
        with _conn().cursor() as cur:
            cur.execute(f'''
                SELECT oper_id, link_id, alias, param
                FROM {T_LINK}
                WHERE COALESCE(use_yn,'Y') <> 'N'
                ORDER BY oper_id, seq, id
            ''')
            for oid, link_id, alias, prm in cur.fetchall():
                if oid in out:
                    continue
                out[oid] = (link_id or '', alias or '', prm or '')
    except Exception as e:
        print(f'[config2] 사전공정 대표값 조회 생략: '
              f'{e.__class__.__name__}: {e}')
    return out


def all_columns(oper_id, scope=None):
    """그 공정에서 만들어질 연계 컬럼 이름 전부 (점검·분석 대상 선정용)"""
    out = []
    for lk in links_of(oper_id, scope):
        for c in lk['columns']:
            if c and c not in out:
                out.append(c)
    return out


# ══════════════════════════════════════════════════════════
# 가져오기 / 검증
# ══════════════════════════════════════════════════════════
def import_from_v1(oper_id=None, overwrite=False):
    """
    기존 기준정보(cmp_cfg_*)를 v2 로 복사한다.

    ★ 사전공정 1개와 Response·Defect 스텝이 연계 공정으로 옮겨진다.
      기존 데이터는 건드리지 않는다 — 읽기만 한다.
    """
    ensure_tables()
    from . import config_service as v1

    done, skipped = [], []
    for o in v1.list_opers():
        oid = o['oper_id']
        if oper_id and oid != _up(oper_id):
            continue
        if not overwrite and get_oper(oid):
            skipped.append(oid)
            continue

        src = v1.get_oper(oid)
        if not src:
            continue

        links = []
        # ★ 사전공정 → param='CHAMBER'
        #   사전공정을 등록한 목적은 입고 장비·챔버(pre_eqp_id / pre_eqp_ch)를
        #   가져오는 것이다. 그 정보는 측정값 테이블이 아니라
        #   wafer-history 에 있고, 조회처는 param 이 가른다.
        if _s(src.get('pre_oper_id')):
            alias = alias_from(src.get('pre_oper_desc'), src['pre_oper_id'])
            # 사전공정을 등록한 목적은 입고 장비·챔버다 → param 을 CHAMBER 로
            links.append({
                'kind': 'SRC',
                'alias': alias,
                'link_id': src['pre_oper_id'],
                'param': 'CHAMBER',
                'scope': 'both',
            })
            # 사전공정의 측정값까지 보던 경우에만 SRC 를 따로 추가한다
            if _s(src.get('pre_oper_param')):
                links.append({
                    'kind': 'SRC',
                    'alias': alias,
                    'link_id': src['pre_oper_id'],
                    'param': _up(src.get('pre_oper_param')),
                    'scope': 'both',
                })
        # Response → REP, Defect → DEF
        for key, kind in (('resps', 'REP'), ('defects', 'DEF')):
            for r in src.get(key) or []:
                if not _s(r.get('step_id')):
                    continue
                links.append({
                    'kind': kind,
                    'alias': alias_from(r.get('step_desc'), r.get('step_id')),
                    'link_id': _up(r.get('step_id')),
                    'lot_cd': _up(r.get('lot_cd')),
                    'param': _up(r.get('param')),
                    'scope': 'both',
                    'use_yn': r.get('use_yn', 'Y'),
                })

        save_oper({**src, 'links': links}, user='import_v1')
        done.append(oid)

    return {'imported': done, 'skipped': skipped}


def overview():
    """
    전체 셋업 현황 — 공정별 구성과 만들어질 컬럼을 한눈에.

    ★ 공정을 하나씩 열어 보면 전체가 어떻게 구성됐는지 알 수 없다.
      어떤 공정에 연계가 빠졌는지, 어떤 별칭이 여러 공정에 쓰였는지,
      모니터링 적재가 얼마나 무거워질지를 이 표로 판단한다.
    """
    ensure_tables()
    rows, alias_use, kind_cnt = [], {}, {'SRC': 0, 'REP': 0, 'DEF': 0}
    tot_mon = tot_ana = 0

    for o in list_opers():
        oid = o['oper_id']
        links = links_of(oid)
        mon = [c for lk in links_of(oid, 'mon') for c in lk['columns'] if c]
        ana = [c for lk in links_of(oid, 'ana') for c in lk['columns'] if c]
        tot_mon += len(mon)
        tot_ana += len(ana)

        items = []
        for lk in links:
            kind_cnt[lk['kind']] = kind_cnt.get(lk['kind'], 0) + 1
            alias_use.setdefault(lk['alias'], []).append(oid)
            items.append({
                'kind': lk['kind'], 'alias': lk['alias'],
                'link_id': lk['link_id'], 'scope': lk['scope'],
                'n_param': len(lk['params']),
                'columns': lk['columns'][:6],
                'more': max(0, len(lk['columns']) - 6),
            })

        # 한눈에 문제를 알 수 있게 짧은 경고만 남긴다 (자세한 건 validate)
        warn = []
        if not o['n_lot']:
            warn.append('device 없음')
        if not o['n_param']:
            warn.append('파라미터 없음')
        if not links:
            warn.append('연계 공정 없음')
        if len(items) > MAX_LINKS:
            warn.append(f'연계 {len(items)}개 (상한 {MAX_LINKS})')

        rows.append({
            'oper_id': oid, 'oper_desc': o['oper_desc'],
            'fab': o['fab'], 'use_yn': o['use_yn'],
            'n_lot': o['n_lot'], 'n_param': o['n_param'],
            'links': items,
            'n_mon_col': len(mon), 'n_ana_col': len(ana),
            'warn': warn,
        })

    # 여러 공정이 같은 별칭을 쓰면 컬럼 이름이 겹칠 수 있다(테이블은 달라도
    # 나중에 합쳐 볼 때 헷갈린다) — 알려만 준다
    shared = {a: v for a, v in alias_use.items() if len(set(v)) > 1}

    return {
        'rows': rows,
        'n_oper': len(rows),
        'n_mon_col': tot_mon, 'n_ana_col': tot_ana,
        'kind_cnt': kind_cnt,
        'shared_alias': {a: sorted(set(v)) for a, v in shared.items()},
    }


def validate(oper_id=None):
    """저장된 기준정보의 문제를 찾는다"""
    ensure_tables()
    out = []
    for o in list_opers():
        oid = o['oper_id']
        if oper_id and oid != _up(oper_id):
            continue
        d = get_oper(oid)
        issues = []

        if not d['lots']:
            issues.append('등록된 device(LOT_CD)가 없습니다')
        if not d['params']:
            issues.append('본공정 파라미터가 없습니다')
        if not _s(d['fab']):
            issues.append('FAB 이 비어 있습니다')

        aliases = {}
        for lk in d['links']:
            a = lk['alias']
            if not a:
                issues.append(f"별칭이 빈 연계 공정이 있습니다 ({lk['link_id']})")
                continue
            aliases.setdefault(a, set())
            if not is_chamber_param(lk['param']):
                aliases[a].add((lk['kind'], lk['link_id']))
            if is_chamber_param(lk['param']):
                continue      # 장비·챔버는 PARAM 이 필요 없다
            if not lk['param']:
                issues.append(f'{a}: 관리 PARAM 이 없어 조회되지 않습니다')
            elif not link_column(lk['kind'], a, lk['param']):
                issues.append(f"{a}: 컬럼 이름을 만들 수 없습니다 "
                              f"(PARAM={lk['param']})")

        if len(aliases) > MAX_LINKS:
            issues.append(f'연계 공정이 {len(aliases)}개로 상한'
                          f'({MAX_LINKS})을 넘었습니다')
        for a, kinds in aliases.items():
            if len(kinds) > 1:
                issues.append(f'별칭 {a} 에 타입·공정ID 가 둘 이상입니다')

        # 컬럼 이름 충돌
        cols = {}
        seen_chm = set()
        for lk in d['links']:
            if is_chamber_param(lk['param']):
                if lk['alias'] in seen_chm:
                    continue            # 같은 별칭의 챔버는 한 번만 센다
                seen_chm.add(lk['alias'])
                names = chm_columns(lk['alias'])
            else:
                c = link_column(lk['kind'], lk['alias'], lk['param'])
                names = [c] if c else []
            for c in names:
                cols[c] = cols.get(c, 0) + 1
        dup = [c for c, n in cols.items() if n > 1]
        if dup:
            issues.append(f'컬럼 이름이 겹칩니다: {", ".join(dup[:5])}')

        if issues:
            out.append({'oper_id': oid, 'oper_desc': o['oper_desc'],
                        'issues': issues})
    return out
