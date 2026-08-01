"""
equipment/adhoc_service.py
════════════════════════════════════════════════════════════
1회성 임의 기간 조회 (Ad-hoc Query)

  정기 적재는 30일 롤링이라 그 밖의 기간은 볼 수 없다.
  "작년 8월 그 공정 데이터를 보고 싶다" 같은 1회성 조회를 위한 것.

────────────────────────────────────────────────────────────
왜 요청 → 실행 → 조회 3단계인가

  웹 프로세스는 Lake(사내 모듈)를 쓸 수 없다. 쓸 수 있더라도
  StarRocks 클러스터를 띄우고 1년치를 훑는 데 몇 분이 걸려
  웹 요청 안에서 끝낼 수 없다.

  그래서 웹은 요청만 큐에 넣고(cmp_adhoc_job),
  배치 서버의 러너(run_adhoc.py)가 집어서 실행하고,
  웹은 상태를 확인하다 완료되면 결과를 분석한다.

★ 결과 테이블 이름을 cmp_analysis_adhoc_<job_id> 로 만든다.
  기존 분석 API 가 oper_id 를 받아 cmp_analysis_<oper_id> 를 읽으므로,
  oper_id='ADHOC_<job_id>' 로 호출하면 분석 화면을 한 줄도 고치지 않고
  그대로 쓸 수 있다.

★ 정기 적재 테이블은 건드리지 않는다. 1회성 결과는 따로 쌓이고
  보관 기간이 지나면 자동으로 지운다.
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback
from datetime import datetime, timedelta

from django.db import connections

T_JOB = 'cmp_adhoc_job'

# 결과 보관 기간 — 지나면 테이블과 요청 기록을 함께 지운다
KEEP_DAYS = 7

# 한 번에 조회할 수 있는 최대 기간 (Lake 부하·시간 보호)
MAX_RANGE_DAYS = 400

# '실행중' 인데 이 시간이 지나도록 끝나지 않으면 멈춘 것으로 보고
# 대기로 되돌릴 수 있게 한다 (워커 재시작 등으로 스레드가 사라진 경우)
STALE_MINUTES = 30

STATUS = ('대기', '실행중', '완료', '실패', '취소')


def _conn():
    return connections['analysis_db']


def _slug(v):
    return re.sub(r'[^0-9A-Za-z_]', '_', str(v)).lower()


def adhoc_oper_id(job_id):
    """분석 API 에 넘길 가상 OPER_ID"""
    return f'ADHOC_{job_id}'


def adhoc_table(job_id):
    return f'cmp_analysis_{_slug(adhoc_oper_id(job_id))}'


# ══════════════════════════════════════════════════════════
# 큐 테이블
# ══════════════════════════════════════════════════════════
def ensure_tables():
    with _conn().cursor() as cur:
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_JOB} (
              id          BIGSERIAL PRIMARY KEY,
              status      VARCHAR(20) DEFAULT '대기',
              title       VARCHAR(200),
              cond        TEXT,
              rows        INTEGER DEFAULT 0,
              message     TEXT,
              requested_by VARCHAR(100),
              requested_at TIMESTAMP,
              started_at  TIMESTAMP,
              finished_at TIMESTAMP
            )
        ''')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{T_JOB}_status '
                    f'ON {T_JOB} (status, id)')


def _exists(cur, t):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", [t])
    return bool(cur.fetchone()[0])


# ══════════════════════════════════════════════════════════
# 요청
# ══════════════════════════════════════════════════════════
def submit(cond, user=''):
    """
    조회 요청을 큐에 넣는다.

    cond 에 담기는 것 (전부 사용자 입력)
      date_from, date_to   조회 기간 (필수)
      fab                  m15x 등 (필수 — 테이블명에 들어감)
      oper_id              조회할 공정 (필수)
      lot_cds              [LOT_CD, ...] (필수)
      recipes              [RECIPE_ID, ...] (선택 — 비우면 레시피 조건 없음)
      params               [PARAM, ...]     (필수 — 조회할 측정 항목)
      eq_model             EBARA / KCT_NTA / KCT_NTH / OPTA (선택)
      pre_oper_id          사전공정 (선택)
      pre_oper_desc, pre_oper_param (선택)
    """
    ensure_tables()
    c = _normalize(cond)
    title = (f"{c['oper_id']} · {', '.join(c['lot_cds'])} · "
             f"{c['date_from']}~{c['date_to']}")

    with _conn().cursor() as cur:
        cur.execute(f'''
            INSERT INTO {T_JOB} (status, title, cond, requested_by, requested_at)
            VALUES ('대기', %s, %s, %s, %s) RETURNING id
        ''', [title[:200], json.dumps(c, ensure_ascii=False),
              str(user)[:100], datetime.now()])
        job_id = cur.fetchone()[0]
    return {'job_id': job_id, 'title': title}


def _normalize(cond):
    """입력 검증 + 정리. 잘못된 요청은 여기서 걸러 배치까지 안 간다."""
    def s(k):
        return str(cond.get(k) or '').strip()

    def lst(k):
        v = cond.get(k) or []
        if isinstance(v, str):
            v = re.split(r'[,\s]+', v)
        return [str(x).strip() for x in v if str(x).strip()]

    date_from, date_to = s('date_from'), s('date_to')
    if not date_from or not date_to:
        raise ValueError('조회 기간(시작일·종료일)을 입력하세요')
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    try:
        d1 = datetime.strptime(date_from, '%Y-%m-%d')
        d2 = datetime.strptime(date_to, '%Y-%m-%d')
    except ValueError:
        raise ValueError('날짜는 YYYY-MM-DD 형식이어야 합니다')
    span = (d2 - d1).days + 1
    if span > MAX_RANGE_DAYS:
        raise ValueError(f'조회 기간이 너무 깁니다 ({span}일). '
                         f'{MAX_RANGE_DAYS}일 이내로 나눠서 요청하세요')

    oper_id = s('oper_id').upper()
    if not oper_id:
        raise ValueError('공정(OPER_ID)을 입력하세요')
    if not re.match(r'^[0-9A-Za-z_\-]+$', oper_id):
        raise ValueError('OPER_ID 에 사용할 수 없는 문자가 있습니다')

    fab = s('fab').lower()
    if not fab:
        raise ValueError('FAB 을 입력하세요 (조회 테이블명에 들어갑니다)')

    lot_cds = [v.upper() for v in lst('lot_cds')]
    if not lot_cds:
        raise ValueError('LOT_CD 를 하나 이상 입력하세요')

    params = [v.upper() for v in lst('params')]
    if not params:
        raise ValueError('조회할 파라미터를 하나 이상 입력하세요')

    return {
        'date_from': date_from, 'date_to': date_to, 'span_days': span,
        'fab': fab, 'oper_id': oper_id,
        'lot_cds': lot_cds,
        'recipes': lst('recipes'),
        'params': params,
        'eq_model': s('eq_model').upper(),
        'pre_oper_id': s('pre_oper_id').upper(),
        'pre_oper_desc': s('pre_oper_desc'),
        'pre_oper_param': s('pre_oper_param').upper(),
    }


def prefill(oper_id):
    """
    기준정보에서 조회 조건 초안을 만들어 준다.
    매번 파라미터 100개를 손으로 넣을 수는 없으므로,
    등록된 공정을 고르면 채워 놓고 날짜만 바꾸게 한다.
    """
    try:
        from . import config_service as cfg
        d = cfg.get_oper(oper_id)
    except Exception as e:
        print(f'[adhoc] 기준정보 조회 실패: {e.__class__.__name__}: {e}')
        return None
    if not d:
        return None

    lots = [l for l in d['lots'] if l['use_yn'] != 'N']
    return {
        'fab': d['fab'], 'oper_id': d['oper_id'], 'eq_model': d['eq_model'],
        'lot_cds': sorted({l['lot_cd'] for l in lots if l['lot_cd']}),
        'recipes': sorted({l['recipe_id'] for l in lots if l['recipe_id']}),
        'params': [p['param'] for p in d['params'] if p['use_yn'] != 'N'],
        'pre_oper_id': d['pre_oper_id'],
        'pre_oper_desc': d['pre_oper_desc'],
        'pre_oper_param': d['pre_oper_param'],
    }


# ══════════════════════════════════════════════════════════
# 조회 / 삭제
# ══════════════════════════════════════════════════════════
def list_jobs(limit=50):
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT id, status, title, rows, message, requested_by,
                   requested_at, started_at, finished_at, cond
            FROM {T_JOB} ORDER BY id DESC LIMIT %s
        ''', [int(limit)])
        rows = cur.fetchall()

    out = []
    for r in rows:
        try:
            cond = json.loads(r[9]) if r[9] else {}
        except Exception:
            cond = {}
        out.append({
            'job_id': r[0], 'status': r[1], 'title': r[2] or '',
            'rows': r[3] or 0, 'message': r[4] or '',
            'requested_by': r[5] or '',
            'requested_at': str(r[6])[:19] if r[6] else '',
            'started_at': str(r[7])[:19] if r[7] else '',
            'finished_at': str(r[8])[:19] if r[8] else '',
            'cond': cond,
            'oper_id': adhoc_oper_id(r[0]),      # 분석 API 에 넘길 값
            'lot_cds': cond.get('lot_cds', []),
        })
    return out


def get_job(job_id):
    jobs = [j for j in list_jobs(limit=1000) if j['job_id'] == int(job_id)]
    return jobs[0] if jobs else None


def delete_job(job_id):
    """요청 기록과 결과 테이블을 함께 지운다"""
    ensure_tables()
    table = adhoc_table(job_id)
    with _conn().cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS {table}')
        cur.execute(f'DELETE FROM {T_JOB} WHERE id = %s', [int(job_id)])
    return True


def cleanup(days=KEEP_DAYS):
    """보관 기간이 지난 1회성 결과 정리"""
    ensure_tables()
    cutoff = datetime.now() - timedelta(days=days)
    removed = []
    with _conn().cursor() as cur:
        cur.execute(f'SELECT id FROM {T_JOB} WHERE requested_at < %s', [cutoff])
        ids = [r[0] for r in cur.fetchall()]
        for jid in ids:
            cur.execute(f'DROP TABLE IF EXISTS {adhoc_table(jid)}')
            removed.append(jid)
        if ids:
            cur.execute(f'DELETE FROM {T_JOB} WHERE id = ANY(%s)', [ids])
    if removed:
        print(f'[adhoc] 보관 기간({days}일) 지난 요청 {len(removed)}건 정리')
    return removed


def claim_job(job_id):
    """
    요청을 '실행중' 으로 선점한다.

    ★ 원자적 UPDATE 여야 한다.
      gunicorn 워커가 여러 개면 '조회 후 갱신' 사이에 다른 워커가 끼어들어
      같은 요청을 두 번 실행할 수 있다. WHERE 절에 상태를 넣고
      RETURNING 으로 실제 갱신 여부를 받아야 한 번만 잡힌다.

    반환: 선점 성공 여부
    """
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f'''
            UPDATE {T_JOB}
            SET status = '실행중', started_at = %s, message = '실행 준비 중',
                finished_at = NULL
            WHERE id = %s AND status IN ('대기', '실패')
            RETURNING id
        ''', [datetime.now(), int(job_id)])
        return cur.fetchone() is not None


def reset_stale(minutes=STALE_MINUTES):
    """
    '실행중' 인데 오래 멈춰 있는 요청을 '대기' 로 되돌린다.

    웹에서 백그라운드 스레드로 돌리면 gunicorn 워커가 재시작될 때
    작업이 사라지는데 상태는 '실행중' 으로 남는다. 그러면 다시 실행할
    방법이 없으므로 되살릴 수 있게 한다.
    """
    ensure_tables()
    cutoff = datetime.now() - timedelta(minutes=minutes)
    with _conn().cursor() as cur:
        cur.execute(f'''
            UPDATE {T_JOB}
            SET status = '대기',
                message = '실행이 중단되어 대기로 되돌렸습니다 (재실행 가능)'
            WHERE status = '실행중' AND COALESCE(started_at, requested_at) < %s
            RETURNING id
        ''', [cutoff])
        ids = [r[0] for r in cur.fetchall()]
    if ids:
        print(f'[adhoc] 멈춘 요청 {len(ids)}건을 대기로 되돌림: {ids}')
    return ids


def run_job_async(job_id):
    """
    웹에서 백그라운드로 실행한다.

    ★ 웹 요청 안에서 직접 돌리면 안 된다 — 1년치 조회는 몇 분 걸려
      게이트웨이 타임아웃에 걸린다. 스레드로 띄우고 화면은 상태를 폴링한다.

    ★ 스레드에서는 DB 커넥션을 직접 닫아야 한다.
      Django 는 요청이 끝날 때 커넥션을 정리하는데, 스레드에는 그 훅이
      없어서 그냥 두면 커넥션이 쌓인다.

    ★ 선점에 실패하면(이미 다른 워커가 잡았거나 상태가 안 맞으면)
      스레드를 띄우지 않는다.
    """
    import threading
    from django.db import connections as _conns

    if not claim_job(job_id):
        job = get_job(job_id)
        st = job['status'] if job else '없음'
        return {'ok': False,
                'error': f'실행할 수 없는 상태입니다 ({st}). '
                         f'이미 실행 중이거나 완료된 요청입니다.'}

    def _worker():
        try:
            run_job(job_id, claimed=True)
        except Exception as e:
            traceback.print_exc()
            try:
                _set_status(job_id, '실패', finished=True,
                            message=f'{e.__class__.__name__}: {e}')
            except Exception:
                pass
        finally:
            _conns.close_all()          # 스레드 커넥션 정리

    t = threading.Thread(target=_worker, name=f'adhoc-{job_id}', daemon=True)
    t.start()
    return {'ok': True, 'job_id': job_id}


def _set_status(job_id, status, message=None, rows=None, started=False,
                finished=False):
    sets, vals = ['status = %s'], [status]
    if message is not None:
        sets.append('message = %s'); vals.append(str(message)[:2000])
    if rows is not None:
        sets.append('rows = %s'); vals.append(int(rows))
    if started:
        sets.append('started_at = %s'); vals.append(datetime.now())
    if finished:
        sets.append('finished_at = %s'); vals.append(datetime.now())
    vals.append(int(job_id))
    with _conn().cursor() as cur:
        cur.execute(f'UPDATE {T_JOB} SET {", ".join(sets)} WHERE id = %s', vals)


# ══════════════════════════════════════════════════════════
# 실행 — 배치 서버에서만 돈다 (Lake 접근 필요)
# ══════════════════════════════════════════════════════════
def run_job(job_id, lake=None, claimed=False):
    """
    요청 1건 실행. 기존 조회 함수를 그대로 쓰되 날짜 범위만 사용자 값으로.

    ★ 정기 적재와 같은 파이프라인을 탄다 —
      fetch → pivot/prepare → merge → finalize → 저장.
      다르게 만들면 같은 데이터인데 결과가 달라진다.

    claimed=True 면 호출자가 이미 claim_job 으로 선점한 것이므로
    상태 검사를 건너뛴다 (웹 백그라운드 실행 경로).
    """
    from . import analysis_service as svc

    job = get_job(job_id)
    if not job:
        return {'ok': False, 'error': f'요청 {job_id} 을 찾을 수 없습니다'}

    if not claimed:
        # 러너(run_adhoc.py)도 같은 선점 규칙을 쓴다 —
        # 웹에서 실행 중인 요청을 러너가 중복 실행하지 않도록.
        if not claim_job(job_id):
            return {'ok': False,
                    'error': f"실행할 수 없는 상태입니다 ({job['status']})"}

    c = job['cond']
    _set_status(job_id, '실행중', message='조회 준비 중', started=True)

    try:
        if lake is None:
            lake = svc.get_lake()

        # 정기 적재의 cond 형태로 맞춘다 (get_oper_cond 가 만들던 것)
        cond = {
            'fab': c['fab'],
            'lot_cd_list': c['lot_cds'],
            'oper_id': c['oper_id'],
            'oper_desc': c.get('oper_desc', ''),
            'eq_model': c.get('eq_model', ''),
            'recipe_list': c.get('recipes', []),
            'param_list': svc._expand_chamber_params(c['params'],
                                                     c.get('eq_model', '')),
            'pre_oper_id': c.get('pre_oper_id', ''),
            'pre_oper_desc': c.get('pre_oper_desc', ''),
            'pre_oper_param': c.get('pre_oper_param', ''),
        }
        d1, d2 = c['date_from'], c['date_to']

        # ── 진행 상황을 화면에 그대로 흘려보낸다 ──────────
        #   "SRC 조회 중" 만 뜨면 얼마나 남았는지 알 수 없어
        #   멈춘 것처럼 느껴진다. 청크 진행률과 누적 행수를 보여준다.
        t0 = datetime.now()
        stage = {'name': ''}

        def prog(done, total, label):
            el = (datetime.now() - t0).seconds
            _set_status(job_id, '실행중',
                        message=f'{stage["name"]} {done}/{total} · {label} '
                                f'({el // 60}분 {el % 60}초 경과)')

        def mark(name):
            stage['name'] = name
            _set_status(job_id, '실행중', message=f'{name} 시작')
            return datetime.now()

        took = {}

        t = mark('SRC(측정값) 조회')
        df_src = svc.fetch_src(lake, cond, date_from=d1, date_to=d2,
                               on_progress=prog)
        took['SRC'] = (datetime.now() - t).seconds

        t = mark('APC 조회')
        df_apc = svc.fetch_apc(lake, cond, date_from=d1, date_to=d2,
                               on_progress=prog)
        took['APC'] = (datetime.now() - t).seconds

        t = mark('MES(LC) 조회')
        df_mes = svc.fetch_mes(lake, cond, df_src, date_from=d1, date_to=d2,
                               on_progress=prog)
        took['MES'] = (datetime.now() - t).seconds

        t = mark('정리·머지')
        w = svc.pivot_src(df_src)
        a = svc.prepare_apc(df_apc)
        m = svc.merge_sources(w, a, df_mes)
        df = svc.finalize_df(m, cond, df_src)
        took['머지'] = (datetime.now() - t).seconds

        # 어느 단계가 오래 걸렸는지 남긴다 — 다음 조회 계획에 쓴다
        detail = ' · '.join(f'{k} {v}초' for k, v in took.items())
        print(f'[adhoc] 요청 {job_id} 단계별 소요 — {detail}')

        if df is None or df.empty:
            _set_status(job_id, '완료', rows=0, finished=True,
                        message=f'조회 결과 없음 ({detail}) — 기간·공정·LOT_CD·'
                                f'파라미터 이름을 확인하세요')
            return {'ok': True, 'rows': 0}

        # ★ 결과는 1회성 테이블에. 정기 적재 테이블은 건드리지 않는다.
        _set_status(job_id, '실행중', message=f'{len(df):,}행 저장 중')
        svc.save_analysis_df(df, adhoc_oper_id(job_id))

        total_sec = (datetime.now() - t0).seconds
        _set_status(job_id, '완료', rows=len(df), finished=True,
                    message=f'{len(df):,}행 · {total_sec // 60}분 {total_sec % 60}초 '
                            f'({detail})')
        return {'ok': True, 'rows': len(df)}

    except Exception as e:
        traceback.print_exc()
        _set_status(job_id, '실패', finished=True,
                    message=f'{e.__class__.__name__}: {e}')
        return {'ok': False, 'error': str(e)}


def run_pending(limit=3, lake=None):
    """
    대기 중인 요청을 처리한다. 배치 러너(run_adhoc.py)가 호출한다.
    Lake 연결은 한 번 만들어 재사용한다.
    """
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f"SELECT id FROM {T_JOB} WHERE status = '대기' "
                    f"ORDER BY id LIMIT %s", [int(limit)])
        ids = [r[0] for r in cur.fetchall()]

    if not ids:
        return {'processed': 0, 'results': []}

    from . import analysis_service as svc
    if lake is None:
        lake = svc.get_lake()

    results = []
    for jid in ids:
        # 웹에서 이미 실행 중일 수 있으므로 선점에 성공한 것만 처리한다
        if not claim_job(jid):
            print(f'[adhoc] 요청 {jid} 은 이미 다른 곳에서 실행 중 — 건너뜀')
            continue
        print(f'[adhoc] 요청 {jid} 실행')
        results.append({'job_id': jid, **run_job(jid, lake=lake, claimed=True)})
    return {'processed': len(results), 'results': results}
