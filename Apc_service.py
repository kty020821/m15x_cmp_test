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

import pandas as pd
import requests
from django.conf import settings

API_URL = 'http://dp.skhynix.com:8080/datahub/v1/api'
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

    api_name = f'{str(fab).lower()}-cmp-apc-modeling-table1'
    url = f"{API_URL}/{c['project']}/{api_name}/page"
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

    api_name = f'{fab}-cmp-apc-modeling-table1'
    url = f"{API_URL}/{c['project']}/{api_name}/page"
    headers = {'Content-Type': 'application/json', 'h-api-token': c['key']}

    # 빈 값은 * (전체)로 — API 의 와일드카드
    eq = str(eqp_id or '').strip() or '*'
    rc = str(recipe_id or '').strip() or '*'
    bind = (f'EQP_ID={eq};PROCESS_ID=*;OPERATION_ID=*;'
            f'RECIPE_ID={rc};%')

    pages, page_no = [], 1
    while True:
        body = {'pageNumber': page_no, 'pageSize': PAGE_SIZE,
                'sortBy': 'RAWID', 'sortOrder': 'ASC',
                'bindParams': [bind]}

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

        try:
            content = json.loads(res.text).get('Content') or []
        except Exception as e:
            raise RuntimeError(f'APC 응답을 해석하지 못했습니다: {e}')

        if not content:
            break
        pages.append(pd.DataFrame(content))
        page_no += 1

        # 마지막 페이지면 그만 (한 페이지 분량이 안 될 때)
        if len(content) < PAGE_SIZE:
            break

    if not pages:
        return pd.DataFrame()
    return pd.concat(pages, ignore_index=True)


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


def to_items(df):
    """
    조회 결과를 '항목 하나 = 한 줄' 로 펼친다.

    반환: {(area, key, item): value}
      area  SETUP_KEY_RAWID (434/437)
      key   그 설정이 붙은 대상 (EQP_ID/RECIPE_ID 조합)
      item  설정 항목 이름
    """
    out = {}
    if df is None or df.empty:
        return out

    cols = {str(c).upper(): c for c in df.columns}
    c_key = cols.get('SETUP_KEY_VALUE')
    c_val = cols.get('SETUP_DATA_VALUE')
    c_area = cols.get('SETUP_KEY_RAWID')

    if not c_key or not c_val:
        return out

    for _, r in df.iterrows():
        area = str(r[c_area]) if c_area else ''
        kv = _split_pairs(r[c_key])

        # 이 설정이 어느 대상의 것인지 — 사람이 읽을 수 있게 압축
        key = ' · '.join(f'{k}={v}' for k, v in kv.items()
                         if v and v != '*')

        for item, value in _split_pairs(r[c_val]).items():
            out[(area, key, item)] = value
    return out


def compare(ref, tgt):
    """
    두 대상을 항목별로 맞춰 본다.

      ref, tgt  {'fab','eqp_id','recipe_id'}

    반환: {'rows': [...], 'summary': {...}, 'ref': ..., 'tgt': ...}
      rows 의 status 는 넷 중 하나
        same     값이 같다
        diff     값이 다르다
        ref_only 기준에만 있다
        tgt_only 대상에만 있다
    """
    df_r = fetch(ref.get('fab'), ref.get('eqp_id'), ref.get('recipe_id'))
    df_t = fetch(tgt.get('fab'), tgt.get('eqp_id'), tgt.get('recipe_id'))

    a, b = to_items(df_r), to_items(df_t)

    rows = []
    for k in sorted(set(a) | set(b)):
        area, key, item = k
        va, vb = a.get(k), b.get(k)

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
    }
