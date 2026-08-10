"""
equipment/load_service.py
════════════════════════════════════════════════════════════
적재(DB 생성) 요청 관리

  모니터링·분석 화면의 'DB 만들기' 버튼이 부르는 곳.

────────────────────────────────────────────────────────────
왜 잠금이 필요한가

  같은 공정을 두 사람이 동시에 적재하면 같은 테이블에 두 프로세스가
  쓴다. save_analysis_df 가 테이블을 다시 만들며 넣는 구조라
  한쪽 결과가 통째로 날아가거나 중간 상태가 섞인다.
  더 나쁜 건 아무 에러 없이 조용히 일어난다는 점이다.

  ★ 잠금 단위는 공정(OPER_ID)이다.
    서로 다른 공정을 동시에 적재하는 건 문제가 없으므로 막지 않는다.
    같은 공정만 막아 대기 시간을 최소화한다.

  ★ 잠금은 DB 행으로 잡는다(메모리 플래그 아님).
    gunicorn 워커가 여러 개라 프로세스 메모리로는 서로를 못 본다.

★ 화면은 요청 전에 status() 로 확인해 '누가 언제 시작했는지' 를
  보여주고, 마지막 적재 시각도 함께 알려 준다 —
  방금 누가 돌렸는데 또 돌리는 일을 줄이기 위한 것.
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback
from datetime import datetime, timedelta

from django.db import connections

T_JOB = 'cmp_load_job'

# 기본 적재 기간 (모니터링 화면의 'DB 만들기')
DEFAULT_DAYS = 45

# '실행중' 인데 이 시간이 지나면 멈춘 것으로 본다
STALE_MINUTES = 120

# 한 번에 돌릴 수 있는 공정 수 — 메모리 보호
#   ★ 동시에 여러 공정을 적재하면 DataFrame 이 그만큼 함께 메모리에 올라온다.
#     순차로 돌리면 최대 사용량이 '가장 큰 공정 하나' 로 유지된다.
MAX_CONCURRENT = 1


def _conn():
    return connections['analysis_db']


def _an_table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def ensure_tables():
    with _conn().cursor() as cur:
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_JOB} (
              id           BIGSERIAL PRIMARY KEY,
              oper_id      VARCHAR(100),
              status       VARCHAR(20) DEFAULT '대기',
              days         INTEGER,
              date_from    VARCHAR(20),
              date_to      VARCHAR(20),
              rows         INTEGER DEFAULT 0,
              message      TEXT,
              requested_by VARCHAR(100),
              requested_at TIMESTAMP,
              started_at   TIMESTAMP,
              finished_at  TIMESTAMP
            )
        ''')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{T_JOB}_oper '
                    f'ON {T_JOB} (oper_id, status)')
        # ★ 같은 공정에 '실행중' 행은 하나만 존재할 수 있다.
        #   두 요청이 동시에 들어와도 DB 가 두 번째를 거절한다.
        cur.execute(f'''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_{T_JOB}_running
            ON {T_JOB} (oper_id) WHERE status = '실행중'
        ''')


# ══════════════════════════════════════════════════════════
# 상태 조회 — 화면이 버튼을 누르기 전에 본다
# ══════════════════════════════════════════════════════════
def status(oper_ids=None):
    """
    공정별 적재 상태.
      running     지금 실행 중인지 (+ 누가 언제 시작했는지)
      last_at     마지막으로 적재를 끝낸 시각
      last_by     그 적재를 요청한 사람
      data_max    적재 테이블의 최신 데이터 날짜
      rows        마지막 적재 행수
    """
    ensure_tables()
    out = {}

    with _conn().cursor() as cur:
        # 실행 중인 것
        cur.execute(f'''
            SELECT oper_id, requested_by, started_at, message, days
            FROM {T_JOB} WHERE status = '실행중'
        ''')
        for oper_id, by, started, msg, days in cur.fetchall():
            out.setdefault(oper_id, {})['running'] = {
                'by': by or '', 'started_at': str(started)[:19] if started else '',
                'message': msg or '', 'days': days,
                'elapsed_min': int((datetime.now() - started).total_seconds() // 60)
                               if started else 0,
            }

        # 마지막으로 끝난 것 (공정별 1건)
        cur.execute(f'''
            SELECT DISTINCT ON (oper_id)
                   oper_id, finished_at, requested_by, rows, status, days
            FROM {T_JOB} WHERE status IN ('완료', '실패')
            ORDER BY oper_id, finished_at DESC
        ''')
        for oper_id, fin, by, rows, st, days in cur.fetchall():
            d = out.setdefault(oper_id, {})
            d['last_at'] = str(fin)[:19] if fin else ''
            d['last_by'] = by or ''
            d['last_rows'] = rows or 0
            d['last_status'] = st
            d['last_days'] = days

        # 적재 테이블의 실제 최신 데이터 날짜
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                    ['cmp_analysis_%'])
        tables = {r[0] for r in cur.fetchall()}

        ids = oper_ids or list(out.keys())
        for oper_id in ids:
            t = _an_table(oper_id)
            d = out.setdefault(oper_id, {})
            if t not in tables:
                d['data_max'] = None
                d['loaded'] = False
                continue
            d['loaded'] = True
            try:
                cur.execute(f'SELECT MAX("DATE"), COUNT(*) FROM {t}')
                mx, n = cur.fetchone()
                d['data_max'] = str(mx)[:19] if mx else None
                d['rows'] = n or 0
            except Exception:
                d['data_max'] = None

    return out


def any_running():
    """지금 돌고 있는 적재 목록 — 화면 경고용"""
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT oper_id, requested_by, started_at, message
            FROM {T_JOB} WHERE status = '실행중' ORDER BY started_at
        ''')
        return [{'oper_id': r[0], 'by': r[1] or '',
                 'started_at': str(r[2])[:19] if r[2] else '',
                 'message': r[3] or ''} for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════
# 요청 / 잠금
# ══════════════════════════════════════════════════════════
def claim(oper_id, days=DEFAULT_DAYS, user='', date_from=None, date_to=None):
    """
    공정 하나를 '실행중' 으로 잡는다.

    ★ INSERT 가 유니크 인덱스에 걸리면 이미 누군가 돌고 있는 것이다.
      조회 후 삽입하면 그 사이에 끼어들 수 있으므로 DB 제약으로 막는다.

    반환: (성공여부, 정보) — 실패하면 정보에 누가 돌리는 중인지 담긴다
    """
    ensure_tables()
    now = datetime.now()
    try:
        with _conn().cursor() as cur:
            cur.execute(f'''
                INSERT INTO {T_JOB} (oper_id, status, days, date_from, date_to,
                       message, requested_by, requested_at, started_at)
                VALUES (%s, '실행중', %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', [oper_id, int(days or DEFAULT_DAYS), date_from, date_to,
                  '적재 준비 중', str(user)[:100], now, now])
            return True, {'job_id': cur.fetchone()[0]}
    except Exception as e:
        # 유니크 위반 — 이미 실행 중
        cur_info = None
        try:
            with _conn().cursor() as cur:
                cur.execute(f'''
                    SELECT requested_by, started_at, message FROM {T_JOB}
                    WHERE oper_id = %s AND status = '실행중'
                ''', [oper_id])
                r = cur.fetchone()
                if r:
                    cur_info = {'by': r[0] or '(알 수 없음)',
                                'started_at': str(r[1])[:19] if r[1] else '',
                                'message': r[2] or ''}
        except Exception:
            pass
        if cur_info:
            return False, {'error': f'{oper_id} 는 이미 적재 중입니다',
                           'running': cur_info}
        return False, {'error': f'적재 요청 실패: {e}'}


def finish(job_id, status, rows=0, message=''):
    with _conn().cursor() as cur:
        cur.execute(f'''
            UPDATE {T_JOB} SET status=%s, rows=%s, message=%s, finished_at=%s
            WHERE id=%s
        ''', [status, int(rows or 0), str(message)[:2000], datetime.now(),
              int(job_id)])


def progress(job_id, message):
    with _conn().cursor() as cur:
        cur.execute(f'UPDATE {T_JOB} SET message=%s WHERE id=%s',
                    [str(message)[:2000], int(job_id)])


def reset_stale(minutes=STALE_MINUTES):
    """워커가 죽어 '실행중' 으로 굳은 것을 풀어 준다"""
    ensure_tables()
    cutoff = datetime.now() - timedelta(minutes=minutes)
    with _conn().cursor() as cur:
        cur.execute(f'''
            UPDATE {T_JOB} SET status='실패', finished_at=%s,
                   message='실행이 중단되어 잠금을 해제했습니다 (재실행 가능)'
            WHERE status='실행중' AND COALESCE(started_at, requested_at) < %s
            RETURNING oper_id
        ''', [datetime.now(), cutoff])
        ids = [r[0] for r in cur.fetchall()]
    if ids:
        print(f'[load] 멈춘 적재 {len(ids)}건 잠금 해제: {ids}')
    return ids


# ══════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════
def run_oper(oper_id, days=DEFAULT_DAYS, user='', date_from=None, date_to=None,
             lake=None):
    """
    공정 하나 적재. 정기 배치와 같은 파이프라인을 탄다.

    ★ 잠금을 잡고 시작하며, 어떤 경로로 끝나든 반드시 푼다(finally).
      풀지 못하면 그 공정은 영영 적재할 수 없게 된다.
    """
    ok, info = claim(oper_id, days=days, user=user,
                     date_from=date_from, date_to=date_to)
    if not ok:
        return {'ok': False, **info}

    job_id = info['job_id']
    try:
        from . import analysis_service as svc

        if lake is None:
            progress(job_id, 'Lake 연결 중')
            lake = svc.get_lake()

        progress(job_id, '기준정보 조회 중')
        df_info = svc.get_config()

        progress(job_id, f'{oper_id} 조회 중 (최근 {days}일)')
        df = svc.build_analysis_df(lake, df_info, oper_id, days=int(days))

        if df is None or df.empty:
            finish(job_id, '완료', 0, '조회 결과가 없습니다')
            return {'ok': True, 'rows': 0}

        progress(job_id, f'{len(df):,}행 저장 중')
        svc.save_analysis_df(df, oper_id)

        finish(job_id, '완료', len(df), f'{len(df):,}행 적재 완료')
        return {'ok': True, 'rows': len(df)}

    except Exception as e:
        traceback.print_exc()
        finish(job_id, '실패', 0, f'{e.__class__.__name__}: {e}')
        return {'ok': False, 'error': str(e)}


def run_many(oper_ids, days=DEFAULT_DAYS, user=''):
    """
    여러 공정을 순차로 적재한다.

    ★ 순차인 이유: 동시에 돌리면 DataFrame 이 공정 수만큼 함께 메모리에
      올라온다. 순차면 최대 사용량이 '가장 큰 공정 하나' 로 유지되어
      메모리를 예측할 수 있다.
    ★ 이미 다른 사람이 잡고 있는 공정은 건너뛴다 — 전체가 멈추지 않게.
    """
    from . import analysis_service as svc

    results, skipped = [], []
    lake = None
    for oper_id in oper_ids:
        with _conn().cursor() as cur:
            cur.execute(f"SELECT requested_by FROM {T_JOB} "
                        f"WHERE oper_id=%s AND status='실행중'", [oper_id])
            r = cur.fetchone()
        if r:
            skipped.append({'oper_id': oper_id, 'by': r[0] or ''})
            continue

        if lake is None:
            lake = svc.get_lake()
        print(f'[load] {oper_id} 적재 시작 (최근 {days}일)')
        results.append({'oper_id': oper_id,
                        **run_oper(oper_id, days=days, user=user, lake=lake)})

    return {'done': len(results), 'results': results, 'skipped': skipped}


def run_async(oper_ids, days=DEFAULT_DAYS, user=''):
    """
    웹에서 백그라운드로 적재한다.
    스레드에서는 Django 가 커넥션을 정리해 주지 않으므로 직접 닫는다.
    """
    import threading
    from django.db import connections as _conns

    ids = [str(o).upper().strip() for o in (oper_ids or []) if str(o).strip()]
    if not ids:
        return {'ok': False, 'error': '적재할 공정이 없습니다'}

    def _worker():
        try:
            run_many(ids, days=days, user=user)
        except Exception:
            traceback.print_exc()
        finally:
            _conns.close_all()

    threading.Thread(target=_worker, name='load-db', daemon=True).start()
    return {'ok': True, 'opers': ids, 'days': days}


def history(limit=50):
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT oper_id, status, days, rows, message, requested_by,
                   requested_at, started_at, finished_at
            FROM {T_JOB} ORDER BY id DESC LIMIT %s
        ''', [int(limit)])
        return [{'oper_id': r[0], 'status': r[1], 'days': r[2],
                 'rows': r[3] or 0, 'message': r[4] or '',
                 'requested_by': r[5] or '',
                 'requested_at': str(r[6])[:19] if r[6] else '',
                 'started_at': str(r[7])[:19] if r[7] else '',
                 'finished_at': str(r[8])[:19] if r[8] else ''}
                for r in cur.fetchall()]
