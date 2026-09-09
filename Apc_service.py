"""
equipment/apc_service.py
════════════════════════════════════════════════════════════
APC 설정 비교

  두 대상(FAB · EQP_ID · RECIPE_ID)의 APC 모델링 설정을 나란히 놓고
  무엇이 다른지 본다.

  ★ 데이터 모양
      SETUP_KEY_VALUE   'EQP_ID=CMP01;PROCESS_ID=P1;RECIPE_ID=R1;'
      SETUP_DATA_VALUE  'GAIN=0.8;LIMIT=120;MODE=AUTO;'
    한 셀에 여러 항목이 ; 와 = 로 붙어 있다. 그대로는 비교할 수 없어
    항목별로 펼쳐서 맞춰 본다.

  ★ SETUP_KEY_RAWID 는 APC 화면의 어느 영역인지를 뜻한다(434/437).
    합쳐도 값은 같지만, 섞으면 어느 화면 설정인지 알 수 없어 나눠 둔다.

  ★ 저장하지 않는다. 설정은 자주 바뀌지 않아 쌓아둘 이유가 적고,
    쌓아두면 '지금 설정' 인지 '그때 설정' 인지 헷갈린다.
════════════════════════════════════════════════════════════
"""
import json
import re

import pandas as pd
import requests
from django.conf import settings

API_URL = 'http://dp.skhynix.com:8080/datahub/v1/api'

# API 이름 형식 — FAB 이 들어간다 (소문자).
#   ★ 이 이름이 틀리면 404 (NotFoundException) 가 난다.
#     형식이 바뀌면 여기만 고치면 된다.
API_NAME = '{fab}-cmp-apc-modeling-table'
PAGE_SIZE = 10000
RETRY = 5
TIMEOUT = 60

# SETUP_KEY_RAWID → 화면 영역 이름
AREA_NAME = {
    '434': 'Modeling',
    '437': 'Condition',
}


def _cfg():
    """settings.py 에서 읽는다 — 없으면 빈 값"""
    return {
        'project': str(getattr(settings, 'APC_PROJECT', '') or '').strip(),
        'key': str(getattr(settings, 'APC_API_KEY', '') or '').strip(),
    }


def config_ok():
    c = _cfg()
    return bool(c['project'] and c['key'])


def check(fab='m15x', eqp_id='*', recipe_id='*'):
    """
    설정과 연결을 확인한다 — 조회가 안 될 때 어디가 문제인지 본다.

    python manage.py shell -c "from equipment import apc_service as a; a.check('m15x','CMP01')"
    """
    c = _cfg()
    print(f"APC_PROJECT : {c['project'] or '(없음)'}")
    print(f"APC_API_KEY : "
          f"{(c['key'][:4] + '…' + c['key'][-4:]) if len(c['key']) > 8 else ('설정됨' if c['key'] else '(없음)')}")
    if not (c['project'] and c['key']):
        print('→ settings.py 에 APC_PROJECT / APC_API_KEY 를 넣으세요')
        return

    api_name = API_NAME.format(fab=str(fab).lower())
    url = f"{API_URL}/{c['project']}/{api_name}"
    print(f'요청 주소   : {url}')

    try:
        df = fetch(fab, eqp_id, recipe_id)
        print(f'조회 결과   : {len(df):,}행')
        if len(df):
            print(f'컬럼        : {", ".join(map(str, df.columns))}')
            print('\n첫 행:')
            for k, v in df.iloc[0].items():
                print(f'  {k}: {str(v)[:120]}')
        else:
            print('→ 조건에 맞는 데이터가 없습니다 '
                  '(FAB·EQP_ID·RECIPE_ID 를 확인하세요)')
    except Exception as e:
        print(f'\n실패: {e}')


def fetch(fab, eqp_id, recipe_id):
    """
    한 대상의 APC 설정을 받아온다.

    ★ 페이지를 끝까지 넘긴다. 한 번에 다 오지 않는다.
    ★ 실패하면 RETRY 번까지 다시 — 사내 API 가 가끔 일시적으로 막힌다.
    """
    c = _cfg()
    if not (c['project'] and c['key']):
        raise ValueError('APC_PROJECT / APC_API_KEY 가 settings.py 에 '
                         '없습니다')

    fab = str(fab or '').strip().lower()
    if not fab:
        raise ValueError('FAB 을 입력하세요')

    api_name = API_NAME.format(fab=fab)
    url = f"{API_URL}/{c['project']}/{api_name}"
    headers = {'Content-Type': 'application/json', 'h-api-token': c['key']}

    # 빈 값은 * (전체)로 — API 의 와일드카드
    eq = str(eqp_id or '').strip() or '*'
    rc = str(recipe_id or '').strip() or '*'
    bind = (f'EQP_ID={eq};PROCESS_ID=*;OPERATION_ID=*;'
            f'RECIPE_ID={rc};%')

    body = {'bindParams': [bind]}

    # ★ 실패하면 왜 실패했는지 남긴다.
    #   '응답 오류' 만으로는 주소가 틀린 건지, 키가 틀린 건지,
    #   조건이 안 맞는 건지 알 수 없다.
    res, last = None, ''
    for i in range(RETRY):
        try:
            res = requests.post(url, headers=headers,
                                data=json.dumps(body), timeout=TIMEOUT)
            if res.status_code == 200:
                break
            last = (f'HTTP {res.status_code} · '
                    f'{(res.text or "")[:300]}')
            print(f'[apc] 시도 {i + 1}/{RETRY} — {last}')
        except Exception as e:
            last = f'{e.__class__.__name__}: {e}'
            print(f'[apc] 시도 {i + 1}/{RETRY} — {last}')
        res = None

    if res is None:
        hint = ''
        if 'HTTP 401' in last or 'HTTP 403' in last:
            hint = ' — API 키(APC_API_KEY)를 확인하세요'
        elif 'HTTP 404' in last:
            hint = (f' — 주소나 이름이 틀렸을 수 있습니다 '
                    f'(api_name: {api_name})')
        elif 'Timeout' in last or 'Connection' in last:
            hint = ' — 서버에서 dp.skhynix.com 으로 나갈 수 있는지 확인하세요'
        raise RuntimeError(f'APC API 응답 오류: {last}{hint}\n'
                           f'  요청: {url}\n'
                           f'  조건: {bind}')

    # ★ 응답 형태가 한 가지가 아니다.
    #   리스트를 바로 주기도 하고, {'Content': [...]} 로 감싸기도 한다.
    #   어느 쪽이든 받아들인다 — 형태 하나를 가정하면
    #   'list object has no attribute get' 으로 죽는다.
    try:
        raw = json.loads(res.text)
    except Exception as e:
        raise RuntimeError(f'APC 응답을 해석하지 못했습니다: {e}\n'
                           f'  받은 내용: {(res.text or "")[:300]}')

    if isinstance(raw, list):
        content = raw
    elif isinstance(raw, dict):
        # 흔히 쓰는 이름부터 찾고, 없으면 리스트인 값을 쓴다
        content = None
        for k in ('Content', 'content', 'data', 'Data',
                  'result', 'Result', 'rows'):
            if isinstance(raw.get(k), list):
                content = raw[k]
                break
        if content is None:
            content = next((v for v in raw.values()
                            if isinstance(v, list)), [])
    else:
        content = []

    if not content:
        return pd.DataFrame()
    return pd.DataFrame(content)


# ── 챔버 짝 ──────────────────────────────────────────────
#   같은 자리를 뜻하는 챔버끼리 묶어 본다.
#   ★ 왼쪽/오른쪽(또는 AB/CD)으로 나뉜 장비는 챔버 이름만 다르고
#     같은 위치를 가리킨다. 이름이 다르다고 '다름' 으로 잡으면
#     실제 차이가 그 속에 묻힌다.
#   ★ 단, 레시피에 _AB / _CD / _L / _R 이 붙은 경우에만 적용한다.
#     그 표기가 없으면 챔버 이름이 진짜로 다른 설정을 뜻한다.
CH_PAIRS = [
    ('PA', 'PC'),
    ('PB', 'PD'),
    ('PL1', 'PR1'),
    ('PL2', 'PR2'),
    ('PL', 'PR'),
    ('P1', 'P2'),
]

# 이 꼬리표가 레시피에 있을 때만 짝을 묶는다
CH_SUFFIX = ('_AB', '_CD', '_L', '_R')

# 짝 → 대표 이름 (긴 것부터 봐야 PL1 이 PL 로 잘못 잡히지 않는다)
_CH_MAP = {}
for _a, _b in CH_PAIRS:
    _CH_MAP[_a] = f'{_a}/{_b}'
    _CH_MAP[_b] = f'{_a}/{_b}'
_CH_KEYS = sorted(_CH_MAP, key=len, reverse=True)


def has_ch_suffix(recipe):
    """레시피에 _AB / _CD / _L / _R 이 붙어 있나"""
    r = str(recipe or '').strip().upper()
    return any(r.endswith(sfx) or f'{sfx}_' in r or f'{sfx}.' in r
               for sfx in CH_SUFFIX)


def normalize_ch(text, enabled):
    """
    챔버 이름을 대표 이름으로 바꾼다 (PA → PA/PC).

    ★ enabled 가 False 면 그대로 둔다 — 꼬리표가 없는 레시피에서는
      챔버 이름이 진짜 다른 설정을 뜻한다.
    ★ 단어 경계로만 바꾼다. 'PART' 안의 'PA' 까지 바꾸면 안 된다.
    ★ 한 번에 바꾼다 — 차례로 치환하면 'PA' → 'PA/PC' 로 바뀐 결과에
      다시 'PA' 가 들어 있어 또 걸린다.
    """
    if not enabled:
        return text

    pat = '|'.join(re.escape(k) for k in _CH_KEYS)
    return re.sub(rf'(?<![0-9A-Za-z])({pat})(?![0-9A-Za-z])',
                  lambda m: _CH_MAP[m.group(1)], str(text or ''))


def _split_pairs(text):
    """
    'A=1;B=2;' → {'A': '1', 'B': '2'}

    ★ 값 안에 = 가 또 있을 수 있어 첫 번째에서만 자른다.
    ★ 빈 조각과 = 없는 조각은 버린다 — 끝의 ; 때문에 생긴다.
    """
    out = {}
    for part in str(text or '').split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        k, _, v = part.partition('=')
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def to_items(df, pair_ch=True):
    """
    조회 결과를 '항목 하나 = 한 줄' 로 펼친다.

    반환: {(area, key, item): value}
      area  SETUP_KEY_RAWID (434/437)
      key   그 설정이 붙은 대상 (EQP_ID/RECIPE_ID 조합)
      item  설정 항목 이름

    ★ pair_ch 가 True 면 챔버 짝을 대표 이름으로 묶는다 (PA → PA/PC).
      레시피에 _AB/_CD/_L/_R 이 붙은 행에만 적용한다 — 그 표기가
      없으면 챔버 이름이 진짜 다른 설정을 뜻하기 때문이다.
    """
    out = {}
    if df is None or df.empty:
        return out

    cols = {str(c).upper(): c for c in df.columns}
    c_key = cols.get('SETUP_KEY_VALUE')
    c_val = cols.get('SETUP_DATA_VALUE')
    c_area = cols.get('SETUP_KEY_RAWID')

    if not c_key or not c_val:
        # ★ 컬럼 이름이 다르면 조용히 빈 결과가 된다 —
        #   '조회는 됐는데 아무것도 안 나온다' 가 가장 찾기 어렵다.
        print(f'[apc] SETUP_KEY_VALUE / SETUP_DATA_VALUE 컬럼이 없습니다. '
              f'실제 컬럼: {", ".join(map(str, df.columns))}')
        return out

    for _, r in df.iterrows():
        area = str(r[c_area]) if c_area else ''
        kv = _split_pairs(r[c_key])

        # 이 행에 챔버 짝을 적용할지 — 레시피 표기로 판단한다
        pair = pair_ch and has_ch_suffix(kv.get('RECIPE_ID', ''))

        # 이 설정이 어느 대상의 것인지 — 사람이 읽을 수 있게 압축
        key = ' · '.join(f'{k}={normalize_ch(v, pair)}'
                         for k, v in kv.items() if v and v != '*')

        for item, value in _split_pairs(r[c_val]).items():
            # 항목 이름과 값 양쪽에 챔버가 들어갈 수 있다
            out[(area, key, normalize_ch(item, pair))] = \
                normalize_ch(value, pair)
    return out


def compare(ref, tgt, pair_ch=True):
    """
    두 대상을 항목별로 맞춰 본다.

      ref, tgt  {'fab','eqp_id','recipe_id'}
      pair_ch   챔버 짝을 묶어 볼지 (PA↔PC 등)

    반환: {'rows': [...], 'summary': {...}, 'ref': ..., 'tgt': ...}
      rows 의 status 는 넷 중 하나
        same     값이 같다
        diff     값이 다르다
        ref_only 기준에만 있다
        tgt_only 대상에만 있다
    """
    df_r = fetch(ref.get('fab'), ref.get('eqp_id'), ref.get('recipe_id'))
    df_t = fetch(tgt.get('fab'), tgt.get('eqp_id'), tgt.get('recipe_id'))

    a, b = to_items(df_r, pair_ch), to_items(df_t, pair_ch)

    # ★ 서로 다른 장비·레시피를 비교하는 것이므로 key(EQP_ID·RECIPE_ID)는
    #   당연히 다르다. 그걸 비교 기준에 넣으면 모든 항목이
    #   '한쪽에만 있음' 으로 잡혀 아무것도 맞춰지지 않는다.
    #   맞추는 기준은 (영역, 항목) 이고, key 는 참고로만 남긴다.
    def _fold(d):
        out = {}
        for (area, key, item), v in d.items():
            out[(area, item)] = {'value': v, 'key': key}
        return out

    fa, fb = _fold(a), _fold(b)

    rows = []
    for k in sorted(set(fa) | set(fb)):
        area, item = k
        ra, rb = fa.get(k), fb.get(k)
        va = ra['value'] if ra else None
        vb = rb['value'] if rb else None
        key = (ra or rb or {}).get('key', '')

        if va is None:
            status = 'tgt_only'
        elif vb is None:
            status = 'ref_only'
        elif str(va) == str(vb):
            status = 'same'
        else:
            status = 'diff'

        rows.append({
            'area': area, 'area_name': AREA_NAME.get(area, area or '(미지정)'),
            'key': key, 'item': item,
            'ref': va, 'tgt': vb, 'status': status,
            # 어느 대상의 설정인지 (기준·대상이 다를 수 있다)
            'ref_key': (ra or {}).get('key', ''),
            'tgt_key': (rb or {}).get('key', ''),
        })

    cnt = {'same': 0, 'diff': 0, 'ref_only': 0, 'tgt_only': 0}
    for r in rows:
        cnt[r['status']] += 1

    return {
        'rows': rows,
        'summary': {**cnt, 'total': len(rows),
                    'ref_rows': len(df_r), 'tgt_rows': len(df_t)},
        'ref': dict(ref), 'tgt': dict(tgt),
        'areas': sorted({r['area'] for r in rows}),
        'pair_ch': bool(pair_ch),
    }
