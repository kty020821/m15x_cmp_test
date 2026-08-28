"""
equipment/monitor_job.py
════════════════════════════════════════════════════════════
Inline Monitoring 점검을 서버에서 돌린다.

  ★ 예전에는 브라우저가 공정마다 요청을 보내며 진행했다.
    페이지를 떠나면 그 반복이 끊겨 점검이 중간에 멈췄다.
    이제 요청은 '시작해 달라' 만 하고, 실제 점검은 서버 스레드가 한다.

  ★ 진행 상황은 DB 에 적는다. 브라우저를 닫아도 계속 돌고,
    다시 들어오면 그 시점의 진행 상황이 보인다.
    다른 사람도 같은 상태를 본다.
════════════════════════════════════════════════════════════
"""
import json
import threading
import traceback
from datetime import datetime

from django.db import connections

from . import monitor_service as ms

T_JOB = 'cmp_monitor_job'

_lock = threading.Lock()
_thread = None


def _conn():
    return connections['analysis_db']


def ensure_table():
    with _conn().cursor() as cur:
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_JOB} (
              id           BIGSERIAL PRIMARY KEY,
              status       VARCHAR(20) DEFAULT '실행중',
              total        INTEGER DEFAULT 0,
              done         INTEGER DEFAULT 0,
              now_label    VARCHAR(200),
              message      TEXT,
              notes        TEXT,
              started_by   VARCHAR(100),
              started_at   TIMESTAMP DEFAULT NOW(),
              finished_at  TIMESTAMP
            )
        ''')
        # ★ 동시에 두 번 돌면 결과가 뒤섞인다. DB 가 막게 한다.
        cur.execute(f'''
            CREATE UNIQUE INDEX IF NOT EXISTS ix_{T_JOB}_running
            ON {T_JOB} ((1)) WHERE status = '실행중'
        ''')


def status():
    """지금 점검이 돌고 있나 — 화면이 몇 초마다 물어본다"""
    ensure_table()
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT id, status, total, done, now_label, message, notes,
                   started_by, started_at, finished_at
            FROM {T_JOB} ORDER BY id DESC LIMIT 1
        ''')
        r = cur.fetchone()

    if not r:
        return {'running': False}

    return {
        'running': r[1] == '실행중',
        'job_id': r[0], 'status': r[1],
        'total': r[2] or 0, 'done': r[3] or 0,
        'now_label': r[4] or '', 'message': r[5] or '',
        'notes': json.loads(r[6]) if r[6] else [],
        'started_by': r[7] or '',
        'started_at': str(r[8])[:19] if r[8] else '',
        'finished_at': str(r[9])[:19] if r[9] else '',
    }


def start(opers, user=''):
    """
    점검 시작.

    ★ 이미 돌고 있으면 거절한다 — 두 번 돌면 결과가 뒤섞인다.
      누가 언제 시작했는지 알려 주어 기다릴지 판단할 수 있게 한다.
    """
    ensure_table()
    cur_st = status()
    if cur_st.get('running'):
        return {'ok': False,
                'error': f"이미 점검 중입니다 "
                         f"({cur_st['started_by'] or '누군가'}가 "
                         f"{cur_st['started_at']} 시작 · "
                         f"{cur_st['done']}/{cur_st['total']})",
                'status': cur_st}

    with _conn().cursor() as cur:
        try:
            cur.execute(f'''
                INSERT INTO {T_JOB} (status, total, done, started_by)
                VALUES ('실행중', %s, 0, %s) RETURNING id
            ''', [len(opers), str(user)[:100]])
            job_id = cur.fetchone()[0]
        except Exception:
            # 유니크 인덱스에 걸림 = 그 사이 누가 시작했다
            return {'ok': False, 'error': '이미 점검 중입니다',
                    'status': status()}

    t = threading.Thread(target=_run, args=(job_id, opers), daemon=True)
    t.start()
    return {'ok': True, 'job_id': job_id, 'total': len(opers)}


def _progress(job_id, done, label):
    try:
        with _conn().cursor() as cur:
            cur.execute(f'UPDATE {T_JOB} SET done=%s, now_label=%s '
                        f'WHERE id=%s', [done, str(label)[:200], job_id])
    except Exception:
        pass


def _finish(job_id, status_txt, message='', notes=None):
    try:
        with _conn().cursor() as cur:
            cur.execute(f'''
                UPDATE {T_JOB} SET status=%s, message=%s, notes=%s,
                       finished_at=%s WHERE id=%s
            ''', [status_txt, str(message)[:2000],
                  json.dumps(notes or [], ensure_ascii=False),
                  datetime.now(), job_id])
    except Exception:
        pass


def _run(job_id, opers):
    """
    점검 본체 — 공정을 하나씩 돈다.

    ★ 한 공정이 실패해도 나머지는 계속한다.
      공정 하나 때문에 전체 점검이 없던 일이 되면 안 된다.
    """
    notes = []
    try:
        # ★ 이번 점검에서 나온 공정만 남기고 나머지는 지운다.
        #   결과는 공정 단위로 덮어써서, 기준정보에서 빠진 공정이나
        #   파라미터의 결과가 계속 남아 화면에서는 방금 점검한 것처럼
        #   보였다.
        #   ★ 먼저 지우지 않는다 — 점검이 통째로 실패하면 아무것도
        #     안 남는다. 끝난 뒤에 '이번에 안 나온 것' 만 지운다.
        done_opers = set()

        for i, o in enumerate(opers):
            oper_id = o.get('oper_id')
            label = o.get('label') or oper_id
            _progress(job_id, i, label)
            try:
                r = ms.run_check(oper_id, label)
                done_opers.add(str(oper_id))
                if r.get('note'):
                    notes.append(f"{label}: {r['note']}")
            except Exception as e:
                traceback.print_exc()
                notes.append(f'{label}: 실패 — {e.__class__.__name__}: {e}')
                print(f'[monitor] {oper_id} 점검 실패: {e}')

        # 이번에 점검하지 않은 공정의 옛 결과를 지운다
        try:
            stale = _drop_stale(done_opers)
            if stale:
                notes.append(f'점검 대상에서 빠진 공정 {stale}건의 '
                             f'옛 결과를 지웠습니다')
                print(f'[monitor] 옛 결과 {stale}건 정리')
        except Exception as e:
            print(f'[monitor] 옛 결과 정리 실패: {e.__class__.__name__}: {e}')

        _progress(job_id, len(opers), '')
        _finish(job_id, '완료',
                f'{len(opers)}개 공정 점검 완료'
                + (f' · 건너뜀 {len(notes)}개' if notes else ''),
                notes)
        print(f'[monitor] 점검 완료 — {len(opers)}개 공정')
    except Exception as e:
        traceback.print_exc()
        _finish(job_id, '실패', f'{e.__class__.__name__}: {e}', notes)


def _drop_stale(keep_opers):
    """
    이번 점검에 나오지 않은 공정의 결과를 지운다.

    ★ 기준정보에서 공정을 빼도 옛 결과가 남아 화면에 계속 보였다.
      점검이 하나도 성공하지 못했으면 아무것도 지우지 않는다 —
      그 경우 지우면 멀쩡한 결과까지 잃는다.
    """
    if not keep_opers:
        return 0
    with _conn().cursor() as cur:
        ph = ','.join(['%s'] * len(keep_opers))
        cur.execute(f'DELETE FROM {ms.RESULT_TABLE} '
                    f'WHERE oper_id NOT IN ({ph})', list(keep_opers))
        return cur.rowcount or 0


def cancel():
    """돌고 있는 점검을 '중단' 으로 표시한다 (스레드는 스스로 끝난다)"""
    ensure_table()
    with _conn().cursor() as cur:
        cur.execute(f"UPDATE {T_JOB} SET status='중단', finished_at=%s "
                    f"WHERE status='실행중'", [datetime.now()])
        return cur.rowcount
