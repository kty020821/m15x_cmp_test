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
import time
import traceback
from datetime import datetime, timedelta

from django.db import connections

T_JOB = 'cmp_load_job'

# 기본 적재 기간 — 처음 적재하는 공정에만 적용된다
DEFAULT_DAYS = 45

# 증분 적재 시 겹쳐서 다시 가져올 기간 (일)
#   ★ 마지막 적재 시각 정각부터 받으면 경계에 걸친 웨이퍼를 놓친다.
#     Lake 는 뒤늦게 들어오는 데이터도 있어 며칠 겹쳐 받는 편이 안전하다.
#     겹친 구간은 저장할 때 그만큼만 지우고 다시 넣으므로 중복이 안 생긴다.
OVERLAP_DAYS = 3

# 동시에 적재할 공정 수.
#   ★ 시간의 대부분은 Lake 응답 대기다. 순차로 돌리면 공정 수에
#     그대로 비례해 늘어난다.
#   ★ 너무 크게 잡으면 Lake 쪽 부담과 메모리가 늘고, 512MiB 웹서버에서는
#     여러 DataFrame 을 동시에 들고 있다 죽을 수 있다.
LOAD_WORKERS = 4

# 증분을 쓸 수 있는 최대 지연(시간).
#   ★ 마지막 데이터가 이보다 오래됐으면 증분이 어딘가에서 막힌 것이다.
#     그대로 두면 계속 그 자리에 머무므로, 기본 기간으로 다시 받는다.
INC_MAX_AGE_H = 72

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
              data_max     VARCHAR(30),
              message      TEXT,
              requested_by VARCHAR(100),
              requested_at TIMESTAMP,
              started_at   TIMESTAMP,
              finished_at  TIMESTAMP
            )
        ''')
        # 예전에 만든 표에는 없는 칸 — 있으면 그냥 넘어간다
        cur.execute(f'ALTER TABLE {T_JOB} '
                    f'ADD COLUMN IF NOT EXISTS data_max VARCHAR(30)')
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

        # ── 최신 데이터 날짜 ────────────────────────────
        #   ★ 적재 테이블을 건드리지 않는다.
        #     MAX("DATE") 는 인덱스가 있어도 공정 수만큼 왕복이 생기고,
        #     테이블이 크면 그 자체가 무겁다. 패널을 열 때마다 그걸
        #     반복하니 버벅였다.
        #   ★ 대신 적재를 끝낼 때 기록해 둔 값을 읽는다.
        #     큐 표는 행이 몇백 개뿐이라 즉시 나온다.
        cur.execute(f'''
            SELECT DISTINCT ON (oper_id) oper_id, data_max
            FROM {T_JOB}
            WHERE status = '완료' AND data_max IS NOT NULL
            ORDER BY oper_id, finished_at DESC
        ''')
        for oper_id, dmax in cur.fetchall():
            out.setdefault(oper_id, {})['data_max'] = dmax

        # 적재 테이블이 실제로 있는지 (가벼운 조회)
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                    ['cmp_analysis_%'])
        tables = {r[0] for r in cur.fetchall()}

        for oper_id in (oper_ids or list(out.keys())):
            d = out.setdefault(oper_id, {})
            d['loaded'] = _an_table(oper_id) in tables
            d.setdefault('data_max', None)

    return out


def refresh_data_max(oper_ids=None):
    """
    적재 테이블을 실제로 훑어 최신 데이터 날짜를 다시 잰다.

    ★ 평소에는 안 쓴다 — 무거워서 화면이 버벅이던 원인이다.
      적재 기록이 없는 기존 공정처럼, 값이 비어 있을 때만 눌러서 채운다.
    """
    ensure_tables()
    out = {}
    with _conn().cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                    ['cmp_analysis_%'])
        tables = {r[0] for r in cur.fetchall()}

        ids = oper_ids or [o for o in tables]
        for oper_id in (oper_ids or []):
            t = _an_table(oper_id)
            if t not in tables:
                continue
            try:
                cur.execute(f'SELECT MAX("DATE") FROM {t}')
                mx = cur.fetchone()[0]
                out[oper_id] = str(mx)[:19] if mx else None
            except Exception as e:
                print(f'[load] {oper_id} 최신일 조회 실패: '
                      f'{e.__class__.__name__}: {e}')

        # 기록으로 남겨 다음부터는 훑지 않게 한다
        for oper_id, dmax in out.items():
            if not dmax:
                continue
            cur.execute(f"""
                UPDATE {T_JOB} SET data_max=%s
                WHERE id = (SELECT id FROM {T_JOB}
                            WHERE oper_id=%s AND status='완료'
                            ORDER BY finished_at DESC LIMIT 1)
            """, [dmax, oper_id])

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


def finish(job_id, status, rows=0, message='', data_max=None):
    """
    작업 종료 기록.

    ★ data_max(최신 데이터 날짜)를 여기서 남긴다.
      화면이 그 값을 읽으므로 적재 테이블을 다시 훑지 않아도 된다 —
      공정이 많을 때 패널이 버벅이던 원인이었다.
    """
    with _conn().cursor() as cur:
        cur.execute(f"""
            UPDATE {T_JOB} SET status=%s, rows=%s, message=%s,
                   finished_at=%s, data_max=COALESCE(%s, data_max)
            WHERE id=%s
        """, [status, int(rows or 0), str(message)[:2000], datetime.now(),
              data_max, int(job_id)])


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
def incremental_range(oper_id, overlap_days=OVERLAP_DAYS):
    """
    증분 적재 기간을 정한다.

      적재 이력이 있으면  마지막 데이터 날짜 - overlap_days ~ 지금
      없으면              None (처음이므로 기본 기간을 쓴다)

    ★ 매일 45일치를 다시 받는 건 낭비다. 하루치가 새로 쌓였으면
      그 하루(+겹침)만 받으면 된다.
    ★ 겹쳐 받는 이유는 두 가지다 — 경계에 걸친 웨이퍼를 놓치지 않기 위해,
      그리고 Lake 에 늦게 들어온 데이터를 반영하기 위해.
    """
    t = _an_table(oper_id)
    try:
        with _conn().cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", [t])
            if not cur.fetchone()[0]:
                return None                  # 테이블 없음 = 첫 적재
            cur.execute(f'SELECT MAX("DATE") FROM {t}')
            mx = cur.fetchone()[0]
    except Exception as e:
        print(f'[load] {oper_id} 마지막 적재일 조회 실패 — 기본 기간 사용: '
              f'{e.__class__.__name__}: {e}')
        return None

    if not mx:
        return None                          # 테이블은 있는데 비어 있음

    # ★ 마지막 데이터가 너무 오래됐으면 증분을 쓰지 않는다.
    #   증분은 '마지막 시각 이후' 만 받으므로, 그 시각이 한 번 잘못
    #   굳으면 계속 그 자리에 머문다 — 예전에 며칠씩 밀린 채
    #   아무도 모르고 지나간 적이 있다.
    #   오래 밀렸으면 기본 기간으로 통째로 다시 받아 바로잡는다.
    age_h = (datetime.now() - mx).total_seconds() / 3600
    if age_h > INC_MAX_AGE_H:
        print(f'[load] {oper_id} 마지막 데이터가 {age_h/24:.1f}일 전 '
              f'({str(mx)[:19]}) — 증분 대신 기본 기간으로 다시 받습니다')
        return None

    start = mx - timedelta(days=int(overlap_days))
    return (start.strftime('%Y-%m-%d'), datetime.now().strftime('%Y-%m-%d'),
            str(mx)[:19])


def run_oper(oper_id, days=DEFAULT_DAYS, user='', date_from=None, date_to=None,
             lake=None, incremental=False):
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

        # ── 증분 적재 ────────────────────────────────────
        #   마지막 데이터 시각부터 지금까지만 받는다.
        #   처음 적재하는 공정이면 기본 기간을 그대로 쓴다.
        #   ★ 겹침(OVERLAP_DAYS)을 두는 이유는 두 가지다 —
        #     경계에 걸친 웨이퍼를 놓치지 않기 위해,
        #     그리고 Lake 에 늦게 들어온 데이터를 반영하기 위해.
        last_at = None
        if incremental and not date_from and not date_to:
            rng = incremental_range(oper_id)
            if rng:
                date_from, date_to, last_at = rng
                print(f'[load] {oper_id} 증분 — 마지막 데이터 {last_at} · '
                      f'{date_from} ~ {date_to} 조회')
            else:
                print(f'[load] {oper_id} 첫 적재 — 최근 {days}일 조회')
        else:
            print(f'[load] {oper_id} 전체 교체 — 최근 {days}일 조회')

        span = (f'{date_from} ~ {date_to}' if (date_from and date_to)
                else f'최근 {days}일')
        progress(job_id, f'{oper_id} 조회 중 ({span})')
        df = svc.build_analysis_df(lake, df_info, oper_id, days=int(days),
                                   date_from=date_from, date_to=date_to)

        if df is None or df.empty:
            finish(job_id, '완료', 0, '조회 결과가 없습니다')
            return {'ok': True, 'rows': 0}

        # ── 연계 공정 (기준정보 v2 에 등록된 것만) ──────
        #   ★ 정기 적재는 '모니터링·둘다' 만 붙인다.
        #     분석 전용까지 매일 붙이면 적재가 무거워진다.
        #   ★ 등록이 없으면 그대로 통과한다 — 연계 없이도
        #     기존 병합 테이블은 완성돼야 한다.
        try:
            from . import link_service as lks
            progress(job_id, '연계 공정 조회 중')
            df = lks.merge_links(df, lake, oper_id, svc.run_query,
                                 scope='mon', days=int(days),
                                 date_from=date_from, date_to=date_to)
        except Exception as e:
            traceback.print_exc()
            print(f'[load] {oper_id} 연계 공정 병합 실패 — 본공정만 저장합니다: '
                  f'{e.__class__.__name__}: {e}')

        progress(job_id, f'{len(df):,}행 저장 중')
        # ★ 증분으로 받았으면 그 구간만 교체한다.
        #   LOT_CD 전체를 지우면 조회하지 않은 이전 기간이 사라진다.
        svc.save_analysis_df(df, oper_id,
                             date_from=date_from if last_at else None)

        finish(job_id, '완료', len(df), f'{len(df):,}행 적재 완료 ({span})')
        return {'ok': True, 'rows': len(df)}

    except Exception as e:
        traceback.print_exc()
        finish(job_id, '실패', 0, f'{e.__class__.__name__}: {e}')
        return {'ok': False, 'error': str(e)}


def run_many(oper_ids, days=DEFAULT_DAYS, user='',
             date_from=None, date_to=None, incremental=False):
    """
    여러 공정을 순차로 적재한다.

    ★ 순차인 이유: 동시에 돌리면 DataFrame 이 공정 수만큼 함께 메모리에
      올라온다. 순차면 최대 사용량이 '가장 큰 공정 하나' 로 유지되어
      메모리를 예측할 수 있다.
    ★ 이미 다른 사람이 잡고 있는 공정은 건너뛴다 — 전체가 멈추지 않게.
    """
    from . import analysis_service as svc
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time

    results, skipped = [], []

    # 이미 남이 잡고 있는 공정은 건너뛴다
    todo = []
    for oper_id in oper_ids:
        with _conn().cursor() as cur:
            cur.execute(f"SELECT requested_by FROM {T_JOB} "
                        f"WHERE oper_id=%s AND status='실행중'", [oper_id])
            r = cur.fetchone()
        if r:
            skipped.append({'oper_id': oper_id, 'by': r[0] or ''})
        else:
            todo.append(oper_id)

    if not todo:
        return {'done': 0, 'results': [], 'skipped': skipped}

    # ★ 공정을 병렬로 돌린다.
    #   대부분의 시간은 Lake 응답을 기다리는 데 쓰인다 — 순차로 돌리면
    #   공정 수에 그대로 비례해 늘어난다. 동시에 던지면 대기 시간이 겹친다.
    #   ★ 너무 많이 띄우면 Lake 쪽에 부담이 되고 메모리도 늘어나므로
    #     LOAD_WORKERS 로 제한한다.
    t0 = _time.time()
    print(f'[load] {len(todo)}개 공정 적재 시작 '
          f'(동시 {LOAD_WORKERS}개 · 최근 {days}일)')

    def _one(oper_id):
        # ★ Lake 연결은 스레드마다 따로 만든다 —
        #   하나를 공유하면 동시에 쓰다가 응답이 뒤섞인다.
        lake = svc.get_lake()
        return {'oper_id': oper_id,
                **run_oper(oper_id, days=days, user=user, lake=lake,
                           date_from=date_from, date_to=date_to,
                           incremental=incremental)}

    with ThreadPoolExecutor(max_workers=LOAD_WORKERS) as ex:
        futs = {ex.submit(_one, o): o for o in todo}
        for f in as_completed(futs):
            oper_id = futs[f]
            try:
                results.append(f.result())
            except Exception as e:
                traceback.print_exc()
                results.append({'oper_id': oper_id, 'ok': False,
                                'error': f'{e.__class__.__name__}: {e}'})
                print(f'[load] {oper_id} 실패: {e}')

    el = _time.time() - t0
    print(f'[load] 전체 완료 — {len(results)}개 공정 · {el/60:.1f}분 '
          f'(공정당 평균 {el/max(len(results),1):.0f}초)')

    return {'done': len(results), 'results': results, 'skipped': skipped}


# ══════════════════════════════════════════════════════════
# 작업 큐 + 백그라운드 워커
#
#   ★ 예전엔 요청마다 스레드를 띄웠다. 그러면 gunicorn 워커가
#     재활용되거나 타임아웃될 때 그 스레드가 같이 죽어, 사용자가
#     페이지를 옮기면 적재가 멈춘 것처럼 보였다.
#
#   ★ 지금은 '대기' 상태로 DB 에 넣기만 하고 즉시 응답한다.
#     실행은 워커 루프가 맡으므로 요청과 수명이 분리된다.
#     진행 상황도 DB 에 있으니 다른 사람이 다른 화면에서도 볼 수 있다.
#
#   ★ 워커는 프로세스당 하나만 돈다. gunicorn 워커가 여러 개여도
#     '실행중' 유니크 인덱스가 같은 공정의 중복 실행을 막는다.
# ══════════════════════════════════════════════════════════
import threading

_worker_lock = threading.Lock()
_worker_thread = None

POLL_SEC = 5                 # 큐 확인 주기 (초)
T_SCHED = 'cmp_load_schedule'


def enqueue(oper_ids, days=DEFAULT_DAYS, user='', date_from=None,
            date_to=None, incremental=False, source='web'):
    """
    적재 요청을 큐에 넣는다. 실행은 워커가 한다.

    ★ 이미 대기·실행 중인 공정은 다시 넣지 않는다 —
      버튼을 두 번 눌러도 두 번 돌지 않게.
    """
    ensure_tables()
    ids = [str(o).upper().strip() for o in (oper_ids or []) if str(o).strip()]
    if not ids:
        return {'ok': False, 'error': '적재할 공정이 없습니다'}

    now = datetime.now()
    tag = '대기 중 · 증분' if incremental else f'대기 중 ({source})'
    queued, skipped = [], []

    with _conn().cursor() as cur:
        cur.execute(
            "SELECT oper_id, status FROM " + T_JOB +
            " WHERE status IN ('대기', '실행중')")
        busy = {r[0]: r[1] for r in cur.fetchall()}

        for oper_id in ids:
            if oper_id in busy:
                skipped.append({'oper_id': oper_id, 'status': busy[oper_id]})
                continue
            cur.execute(
                "INSERT INTO " + T_JOB +
                " (oper_id, status, days, date_from, date_to, message,"
                "  requested_by, requested_at)"
                " VALUES (%s, '대기', %s, %s, %s, %s, %s, %s)",
                [oper_id, int(days or DEFAULT_DAYS), date_from, date_to,
                 tag, str(user)[:100], now])
            queued.append(oper_id)

    ensure_worker()
    return {'ok': True, 'queued': queued, 'skipped': skipped,
            'days': days, 'incremental': bool(incremental)}


def ensure_worker():
    """
    워커가 없으면 띄운다.

    ★ 프로세스가 재시작되면 워커도 사라지므로, 요청이 들어올 때마다
      확인해 되살린다. 대기 중인 작업이 남아 있으면 이어서 처리한다.
    """
    global _worker_thread
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return False
        _worker_thread = threading.Thread(target=_worker_loop,
                                          name='cmp-load-worker', daemon=True)
        _worker_thread.start()
        print('[load] 적재 워커 시작')
        return True


def worker_alive():
    return bool(_worker_thread and _worker_thread.is_alive())


def _worker_loop():
    """
    큐를 확인하며 하나씩 처리한다.

    ★ 순차 처리다. 동시에 여러 공정을 돌리면 DataFrame 이 그만큼
      함께 메모리에 올라온다. 순차면 최대 사용량이
      '가장 큰 공정 하나' 로 유지되어 예측할 수 있다.
    ★ 어떤 예외가 나도 루프는 죽지 않는다 — 워커가 죽으면
      큐에 쌓인 나머지가 전부 멈춘다.
    """
    from django.db import connections as _conns
    idle = 0

    while True:
        try:
            reset_stale()                 # 굳은 잠금 정리
            due = _due_schedule()         # 예약된 정기 적재
            if due:
                _enqueue_schedule(due)

            job = _take_next()
            if not job:
                idle += 1
                time.sleep(POLL_SEC)
                continue

            idle = 0
            _run_job(job)

        except Exception:
            traceback.print_exc()
            time.sleep(POLL_SEC)
        finally:
            # 스레드에서는 Django 가 커넥션을 정리해 주지 않는다
            try:
                if idle == 0 or idle % 12 == 0:
                    _conns.close_all()
            except Exception:
                pass


def _take_next():
    """대기 중인 것 하나를 집는다 (오래 기다린 것부터)"""
    with _conn().cursor() as cur:
        cur.execute(
            "SELECT id, oper_id, days, date_from, date_to, requested_by,"
            " message FROM " + T_JOB +
            " WHERE status = '대기' ORDER BY requested_at, id LIMIT 1")
        r = cur.fetchone()
    if not r:
        return None
    return {'id': r[0], 'oper_id': r[1], 'days': r[2],
            'date_from': r[3], 'date_to': r[4], 'user': r[5],
            'incremental': '증분' in (r[6] or '')}


def _run_job(job):
    """
    대기 → 실행중 → 완료/실패.

    ★ 같은 공정이 이미 실행 중이면 유니크 인덱스가 막는다.
      그 경우 이유를 남기고 이 작업은 취소한다.
    """
    oper_id = job['oper_id']

    try:
        with _conn().cursor() as cur:
            cur.execute(
                "UPDATE " + T_JOB +
                " SET status='실행중', started_at=%s, message='적재 준비 중'"
                " WHERE id=%s AND status='대기'",
                [datetime.now(), job['id']])
            if not cur.rowcount:
                return                  # 다른 워커가 먼저 집었다
    except Exception as e:
        finish(job['id'], '실패', 0, f'같은 공정이 이미 적재 중입니다: {e}')
        return

    span = '증분' if job['incremental'] else f"최근 {job['days']}일"
    print(f'[load] {oper_id} 적재 시작 ({span})')

    try:
        _do_load(job)
    except Exception as e:
        traceback.print_exc()
        finish(job['id'], '실패', 0, f'{e.__class__.__name__}: {e}')


def _do_load(job):
    """실제 적재 — run_oper 과 같은 흐름이되 큐 항목을 쓴다"""
    from . import analysis_service as svc

    job_id, oper_id = job['id'], job['oper_id']
    days = int(job['days'] or DEFAULT_DAYS)
    date_from, date_to = job['date_from'], job['date_to']

    progress(job_id, 'Lake 연결 중')
    lake = svc.get_lake()

    progress(job_id, '기준정보 조회 중')
    df_info = svc.get_config()

    last_at = None
    if job['incremental'] and not date_from and not date_to:
        rng = incremental_range(oper_id)
        if rng:
            date_from, date_to, last_at = rng

    span = (f'{date_from} ~ {date_to}' if (date_from and date_to)
            else f'최근 {days}일')
    progress(job_id, f'{oper_id} 조회 중 ({span})')

    df = svc.build_analysis_df(lake, df_info, oper_id, days=days,
                               date_from=date_from, date_to=date_to)

    if df is None or df.empty:
        finish(job_id, '완료', 0, f'조회 결과가 없습니다 ({span})')
        return

    # 연계 공정 (기준정보 v2) — 실패해도 본공정은 저장한다
    try:
        from . import link_service as lks
        progress(job_id, '연계 공정 조회 중')
        df = lks.merge_links(df, lake, oper_id, svc.run_query,
                             scope='mon', days=days,
                             date_from=date_from, date_to=date_to)
    except Exception as e:
        traceback.print_exc()
        print(f'[load] {oper_id} 연계 병합 실패 — 본공정만 저장: {e}')

    progress(job_id, f'{len(df):,}행 저장 중')
    # ★ 증분이면 다시 조회한 구간만 교체한다 (last_at 이 있을 때).
    #   전체 조회면 date_from 없이 = LOT_CD 전체를 갈아 끼운다.
    svc.save_analysis_df(df, oper_id,
                         date_from=date_from if last_at else None)

    # ★ 최신 데이터 날짜는 지금 손에 든 DataFrame 에서 바로 얻는다.
    #   나중에 테이블을 훑는 것보다 훨씬 싸다.
    dmax = None
    try:
        col = next((c for c in df.columns if str(c).upper() == 'DATE'), None)
        if col is not None:
            v = df[col].max()
            dmax = str(v)[:19] if v is not None else None
    except Exception:
        pass

    # ★ 증분인데 최신 시각이 그대로면 정체다.
    #   조회는 성공했는데 새 데이터가 하나도 없다는 뜻이라,
    #   조용히 넘어가면 며칠씩 밀린 채 아무도 모른다.
    if last_at and dmax and str(dmax)[:19] <= str(last_at)[:19]:
        print(f'[load] ★ {oper_id} 증분인데 최신 시각이 그대로입니다 '
              f'({last_at}) — Lake 에 새 데이터가 없거나 조회 조건을 '
              f'확인하세요')

    # ★ 일부 LOT_CD 가 실패했으면 그 사실을 결과에 남긴다.
    #   '완료' 로만 표시하면 빠진 데이터를 모르고 지나친다.
    try:
        fails = svc.get_fails()
    except Exception:
        fails = []

    if fails:
        keys = ', '.join(f['key'] for f in fails[:4])
        more = f' 외 {len(fails) - 4}건' if len(fails) > 4 else ''
        finish(job_id, '완료', len(df),
               f'{len(df):,}행 적재 ({span}) · 조회 실패 {len(fails)}건 '
               f'[{keys}{more}] — 그 부분은 빠져 있습니다', data_max=dmax)
    else:
        finish(job_id, '완료', len(df), f'{len(df):,}행 적재 완료 ({span})',
               data_max=dmax)


def run_async(oper_ids, days=DEFAULT_DAYS, user='',
              date_from=None, date_to=None, incremental=False):
    """예전 이름 — 이제 큐에 넣는다"""
    res = enqueue(oper_ids, days=days, user=user, date_from=date_from,
                  date_to=date_to, incremental=incremental)
    if res.get('ok'):
        res['opers'] = res.get('queued', [])
    return res


def queue_status():
    """큐 상태 — 어느 화면에서든 진행 상황을 볼 수 있게 한다"""
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) FROM " + T_JOB +
            " WHERE status IN ('대기', '실행중') GROUP BY status")
        cnt = dict(cur.fetchall())

        cur.execute(
            "SELECT oper_id, status, message, requested_by, started_at,"
            " requested_at FROM " + T_JOB +
            " WHERE status IN ('대기', '실행중')"
            " ORDER BY CASE status WHEN '실행중' THEN 0 ELSE 1 END,"
            " requested_at")
        items = [{'oper_id': r[0], 'status': r[1], 'message': r[2] or '',
                  'by': r[3] or '',
                  'started_at': str(r[4])[:19] if r[4] else '',
                  'requested_at': str(r[5])[:19] if r[5] else ''}
                 for r in cur.fetchall()]

    return {'waiting': cnt.get('대기', 0), 'running': cnt.get('실행중', 0),
            'items': items, 'worker': worker_alive()}


def cancel(oper_id=None, all_waiting=False):
    """대기 중인 작업을 취소한다 (실행 중인 것은 멈출 수 없다)"""
    ensure_tables()
    with _conn().cursor() as cur:
        if all_waiting:
            cur.execute(
                "UPDATE " + T_JOB +
                " SET status='실패', finished_at=%s,"
                " message='사용자가 취소했습니다'"
                " WHERE status='대기' RETURNING oper_id", [datetime.now()])
        else:
            cur.execute(
                "UPDATE " + T_JOB +
                " SET status='실패', finished_at=%s,"
                " message='사용자가 취소했습니다'"
                " WHERE status='대기' AND oper_id=%s RETURNING oper_id",
                [datetime.now(), str(oper_id).upper()])
        return [r[0] for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════
# 정기 적재 예약
#
#   ★ 웹에서 켜고 끄는 하루 1회 예약이다. 워커가 큐를 확인하는
#     김에 시각도 함께 보므로 별도 스케줄러가 필요 없다.
#   ★ 사내 스케줄러(run_analysis_load.py)와 병행해도 된다 —
#     같은 공정이 겹치면 큐가 중복을 막는다.
# ══════════════════════════════════════════════════════════
def ensure_sched_table():
    """
    예약 표를 만든다.

    ★ 트랜잭션이 열린 채 요청이 끝나면 저장이 롤백된다.
      설정 저장은 곧바로 반영돼야 하므로 자동커밋을 확인한다.
    """
    conn = _conn()
    if not conn.get_autocommit():
        conn.set_autocommit(True)
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS " + T_SCHED + " ("
            "  id BIGSERIAL PRIMARY KEY,"
            "  name        VARCHAR(100),"
            "  enabled     VARCHAR(1) DEFAULT 'N',"
            "  hour        INTEGER DEFAULT 6,"
            "  minute      INTEGER DEFAULT 0,"
            "  incremental VARCHAR(1) DEFAULT 'Y',"
            "  days        INTEGER DEFAULT 45,"
            "  last_run_at TIMESTAMP,"
            "  updated_by  VARCHAR(100),"
            "  updated_at  TIMESTAMP DEFAULT NOW())")


def get_schedule():
    """예약 설정 — 없으면 기본값(꺼짐)을 만들어 돌려준다"""
    ensure_sched_table()
    with _conn().cursor() as cur:
        cur.execute(
            "SELECT id, name, enabled, hour, minute, incremental, days,"
            " last_run_at FROM " + T_SCHED + " ORDER BY id LIMIT 1")
        r = cur.fetchone()
        if not r:
            cur.execute(
                "INSERT INTO " + T_SCHED + " (name, enabled)"
                " VALUES ('전체 공정 정기 적재', 'N') RETURNING id")
            return {'id': cur.fetchone()[0], 'name': '전체 공정 정기 적재',
                    'enabled': 'N', 'hour': 6, 'minute': 0,
                    'incremental': 'Y', 'days': 45, 'last_run_at': None}
    return {'id': r[0], 'name': r[1] or '', 'enabled': r[2] or 'N',
            'hour': r[3], 'minute': r[4], 'incremental': r[5] or 'Y',
            'days': r[6], 'last_run_at': str(r[7])[:19] if r[7] else None}


def _yn(v, default='N'):
    return 'Y' if str(v).upper() in ('Y', 'TRUE', '1', 'ON') else default


def save_schedule(d, user=''):
    """
    예약 설정 저장.

    ★ 저장 후 다시 읽어 돌려준다 — 화면이 그 값을 그대로 그리므로
      DB 와 화면이 어긋날 수 없다.
    """
    ensure_sched_table()
    cur_s = get_schedule()
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE " + T_SCHED +
            " SET enabled=%s, hour=%s, minute=%s, incremental=%s, days=%s,"
            " updated_by=%s, updated_at=NOW() WHERE id=%s",
            [_yn(d.get('enabled')),
             max(0, min(23, int(d.get('hour', 6) or 0))),
             0,                     # 분은 쓰지 않는다 (시 단위 예약)
             _yn(d.get('incremental', 'Y'), 'Y'),
             max(1, int(d.get('days', 45) or 45)),
             str(user)[:100], cur_s['id']])
        if not cur.rowcount:
            # 행이 없어졌으면 새로 만든다 (표를 지운 경우 등)
            cur.execute(
                "INSERT INTO " + T_SCHED +
                " (name, enabled, hour, minute, incremental, days,"
                "  updated_by, updated_at)"
                " VALUES ('전체 공정 정기 적재', %s, %s, 0, %s, %s, %s, NOW())",
                [_yn(d.get('enabled')),
                 max(0, min(23, int(d.get('hour', 6) or 0))),
                 _yn(d.get('incremental', 'Y'), 'Y'),
                 max(1, int(d.get('days', 45) or 45)),
                 str(user)[:100]])

    ensure_worker()
    saved = get_schedule()
    print(f"[load] 예약 저장 — 사용 {saved['enabled']} · "
          f"{saved['hour']}시 · 증분 {saved['incremental']}")
    return saved


def _due_schedule():
    """
    지금 돌려야 할 예약이 있나.

    ★ 시 단위다. 분까지 맞추면 서버가 그 1분에 안 떠 있으면 건너뛴다.
      '그 시각대에 들어섰고 오늘 아직 안 돌았으면 실행' 이 더 튼튼하다.
    """
    try:
        s = get_schedule()
    except Exception:
        return None
    if s['enabled'] != 'Y':
        return None

    now = datetime.now()
    if now.hour != s['hour']:
        return None
    if s.get('last_run_at') and \
            str(s['last_run_at'])[:10] == now.strftime('%Y-%m-%d'):
        return None                 # 오늘 이미 돌았다
    return s


def _sched_opers():
    """예약 적재 대상 — v1·v2 기준정보를 합친다"""
    out = []
    for mod in ('config_service',):
        try:
            m = __import__(f'{__package__}.{mod}', fromlist=[mod])
            for o in m.list_opers():
                if str(o.get('use_yn') or 'Y').upper() == 'N':
                    continue
                if o['oper_id'] not in out:
                    out.append(o['oper_id'])
        except Exception as e:
            print(f'[load] {mod} 공정 목록 조회 생략: '
                  f'{e.__class__.__name__}: {e}')
    return out


def _enqueue_schedule(s):
    """예약된 전 공정 적재를 큐에 넣는다"""
    opers = _sched_opers()
    if not opers:
        print('[load] 정기 적재 — 대상 공정이 없습니다')
        return

    res = enqueue(opers, days=s['days'], user='schedule',
                  incremental=(s['incremental'] == 'Y'), source='정기')
    print(f"[load] 정기 적재 시작 — 대상 {len(opers)}개 중 "
          f"{len(res.get('queued', []))}개 큐 등록")

    with _conn().cursor() as cur:
        cur.execute("UPDATE " + T_SCHED + " SET last_run_at=%s WHERE id=%s",
                    [datetime.now(), s['id']])


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
