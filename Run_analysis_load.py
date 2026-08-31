"""
run_analysis_load.py   (프로젝트 루트 · manage.py 옆)
════════════════════════════════════════════════════════════
산포 분석 데이터 배치 적재. 사내 스케줄러가 이 파일을 실행한다.

  python run_analysis_load.py                  전체 공정, 30일
  python run_analysis_load.py --oper OP100     특정 공정만
  python run_analysis_load.py --days 7         기간 변경
  python run_analysis_load.py --jobs 4         동시 4개 처리 (빠름)
  python run_analysis_load.py --full           증분 없이 전체 다시 받기
  python run_analysis_load.py --dry-run        조회만 하고 저장 안 함

[스케줄러 환경 대응]
  · 작업 디렉터리가 어디든 동작하도록 절대경로 기준으로 설정
  · 로그를 파일과 표준출력에 동시 기록 (logs/analysis_load_YYYYMMDD.log)
  · 중복 실행 방지 (락 파일)
  · 실패 시 exit code 1 → 스케줄러가 실패를 인지
  · ★ 스케줄러가 넘기는 빈 인자('None', 'null', 따옴표만 남은 값)를 무시
════════════════════════════════════════════════════════════
"""

import os
import sys
import gc
import time
import atexit
import logging
import argparse
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime

# ── 경로 고정 ─────────────────────────────────────────────
#    스케줄러는 임의의 디렉터리에서 실행하므로 파일 위치 기준으로 잡는다
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

LOG_DIR  = BASE_DIR / 'logs'
LOCK_PATH = BASE_DIR / '.analysis_load.lock'
LOCK_STALE_SEC = 6 * 60 * 60        # 6시간 지난 락은 죽은 것으로 간주


# ══════════════════════════════════════════════════════════
# 로깅
# ══════════════════════════════════════════════════════════
def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"analysis_load_{datetime.now():%Y%m%d}.log"

    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                            datefmt='%H:%M:%S')
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    return log_file


# ══════════════════════════════════════════════════════════
# 인자 정리
#
#   ★ 스케줄러가 인자 칸을 비워 두면 도구에 따라 'None' 이나 'null' 이라는
#     '글자' 를 그대로 넘긴다. 이 스크립트에는 위치 인자가 없으므로
#     argparse 가 'unrecognized arguments: None' 으로 즉시 종료해 버린다.
#     배치가 통째로 안 도는 원인이 되므로 여기서 걸러 낸다.
#
#   ★ 근본 해결은 스케줄러의 '인수' 칸을 비우는 것이다.
#     무엇을 무시했는지 로그에 남겨 설정을 고칠 수 있게 한다.
# ══════════════════════════════════════════════════════════
JUNK_ARGS = {'none', 'null', 'nil', 'nan', '-', '""', "''"}


def clean_argv(argv):
    """의미 없는 인자를 걸러 내고 (정리된 목록, 무시한 목록) 반환"""
    keep, dropped = [], []
    for a in argv:
        t = str(a).strip().strip('"').strip("'")
        if not t or t.lower() in JUNK_ARGS:
            dropped.append(a)
            continue
        keep.append(a)
    return keep, dropped


# ══════════════════════════════════════════════════════════
# 중복 실행 방지
#   Lake 조회가 길어 다음 스케줄이 겹칠 수 있다.
#   동시에 돌면 같은 테이블에 DELETE/INSERT 가 뒤엉킨다.
# ══════════════════════════════════════════════════════════
def _pid_alive(pid):
    """
    그 프로세스가 아직 살아 있나.

    ★ 이게 없으면 비정상 종료로 남은 락에 6시간 동안 묶인다.
      실제로 '이미 실행 중' 이라며 건너뛰는데 정작 아무것도 안 도는
      상황이 나온다.
    """
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False

    if os.name == 'nt':
        # 윈도우 — tasklist 로 확인
        try:
            import subprocess
            out = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return True          # 확인 못 하면 살아 있다고 본다 (보수적)
    else:
        try:
            os.kill(pid, 0)      # 신호 0 = 존재 확인만
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True          # 남의 프로세스지만 살아 있음
        except Exception:
            return True


def acquire_lock(force=False):
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        pid = LOCK_PATH.read_text().strip()

        if force:
            logging.warning(f"--force — 기존 락 무시 (pid={pid})")
        elif not _pid_alive(pid):
            # ★ 프로세스가 이미 죽었으면 락은 의미가 없다.
            logging.warning(f"죽은 락 발견 (pid={pid}, {age/60:.0f}분 전) "
                            f"→ 정리하고 진행합니다")
        elif age < LOCK_STALE_SEC:
            logging.warning(
                f"이미 실행 중 (pid={pid}, {age/60:.0f}분 경과) → 이번 회차 건너뜀")
            logging.warning(
                f"  · 실제로 안 돌고 있다면: python run_analysis_load.py --force")
            logging.warning(f"  · 또는 락 파일 삭제: {LOCK_PATH}")
            return False
        else:
            logging.warning(f"오래된 락 ({age/3600:.1f}시간) → 무시하고 진행")

    LOCK_PATH.write_text(str(os.getpid()))
    atexit.register(release_lock)
    return True


def release_lock():
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--oper',    default=None,   help='특정 oper_id 만 처리')
    ap.add_argument('--days',    type=int, default=30, help='조회 기간(일)')
    ap.add_argument('--full',    action='store_true',
                    help='증분을 쓰지 않고 기간 전체를 다시 받습니다')
    ap.add_argument('--jobs',    type=int, default=1,
                    help='동시에 처리할 공정 수 (기본 1=순차). '
                         '대부분 Lake 대기 시간이라 4 정도면 크게 빨라진다. '
                         '메모리가 빠듯하면 올리지 말 것')
    ap.add_argument('--dry-run', action='store_true',  help='저장하지 않고 조회만')
    ap.add_argument('--force',   action='store_true',
                    help='남아 있는 락을 무시하고 실행')

    # ★ 걸러 낸 뒤 parse_known_args 로 받는다.
    #   그래도 모르는 인자가 남으면 무시하고 로그로만 알린다 —
    #   인자 하나 때문에 배치 전체가 안 도는 게 더 큰 손해다.
    argv, junk = clean_argv(sys.argv[1:])
    args, unknown = ap.parse_known_args(argv)

    log_file = setup_logging()
    logging.info("=" * 56)
    logging.info(f"산포 분석 적재 시작  (days={args.days}"
                 f"{', oper=' + args.oper if args.oper else ''}"
                 f"{', DRY-RUN' if args.dry_run else ''})")
    logging.info(f"로그: {log_file}")

    if junk:
        logging.warning(f"빈 인자 무시: {junk} "
                        f"— 스케줄러의 '인수' 칸을 비워 두세요")
    if unknown:
        logging.warning(f"모르는 인자 무시: {unknown}")

    if not acquire_lock(force=args.force):
        return 0                      # 중복 실행은 실패가 아님

    # Django 초기화는 경로/락 설정 후에
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()

    from equipment.analysis_service import (
        get_lake, get_config, get_oper_list, build_analysis_df, save_analysis_df,
    )

    t_all = time.time()

    # ── 기준정보 ──────────────────────────────────────────
    try:
        df_info = get_config()
        opers   = get_oper_list(df_info)
    except Exception:
        logging.error("기준정보(구닥스) 조회 실패")
        logging.error(traceback.format_exc())
        return 1

    if args.oper:
        opers = opers[opers['OPER_ID'] == args.oper]
        if opers.empty:
            logging.error(f"기준정보에 {args.oper} 없음")
            return 1

    logging.info(f"대상 공정 {len(opers)}개")

    # ── Lake 연결 (한 번만) ───────────────────────────────
    try:
        lake = get_lake()
    except Exception:
        logging.error("Lake 연결 실패")
        logging.error(traceback.format_exc())
        return 1

    # ── 공정별 처리 ───────────────────────────────────────
    ok, fail, empty = 0, 0, 0
    failed_opers = []
    _lock = threading.Lock()

    def _one(oper_id, oper_desc):
        """공정 하나 — 성공/빈결과/실패 중 하나를 돌려준다"""
        nonlocal ok, fail, empty
        t0 = time.time()
        df = None
        try:
            logging.info(f"── [{oper_id}] {oper_desc}")
            # ★ Lake 연결은 스레드마다 따로 만든다 — 하나를 공유하면
            #   동시에 쓰다가 응답이 뒤섞인다.
            lk = get_lake() if args.jobs > 1 else lake

            # ★ 증분 — 마지막 데이터 이후만 받는다.
            #   매시간 45일치를 통째로 받으면 공정 수만큼 시간이 늘어난다.
            #   마지막 데이터가 너무 오래됐으면(정체) 자동으로 전체 조회로
            #   돌아간다 — incremental_range 가 그때 None 을 준다.
            d_from = d_to = None
            last_at = None
            if not args.full:
                try:
                    from equipment import load_service as _ls
                    rng = _ls.incremental_range(oper_id)
                    if rng:
                        d_from, d_to, last_at = rng
                        logging.info(f"   증분 {d_from} ~ {d_to} "
                                     f"(마지막 {last_at})")
                except Exception as e:
                    logging.warning(f"   증분 범위 계산 실패, 전체 조회: {e}")

            df = build_analysis_df(lk, df_info, oper_id, days=args.days,
                                   date_from=d_from, date_to=d_to)

            if df is None or df.empty:
                with _lock:
                    empty += 1
                logging.warning(f"   [{oper_id}] 데이터 없음 (건너뜀)")
                return

            if args.dry_run:
                logging.info(f"   [{oper_id}] [dry-run] {len(df):,}행 · "
                             f"{len(df.columns)}컬럼 ({time.time()-t0:.0f}s)")
            else:
                # 증분이면 그 구간만 교체한다
                save_analysis_df(df, oper_id,
                                 date_from=d_from if last_at else None)
                logging.info(f"   [{oper_id}] 완료 {len(df):,}행 "
                             f"({time.time()-t0:.0f}s)")
            with _lock:
                ok += 1

        except Exception:
            with _lock:
                fail += 1
                failed_opers.append(str(oper_id))
            logging.error(f"   [{oper_id}] 실패")
            logging.error(traceback.format_exc())
        finally:
            # 공정 수가 많으므로 매번 정리 (메모리 누적 방지)
            df = None
            gc.collect()

    rows = [(r['OPER_ID'], r.get('OPER_DESC', ''))
            for _, r in opers.iterrows()]

    if args.jobs > 1:
        # ★ 시간의 대부분은 Lake 응답 대기다. 동시에 던지면 그만큼 겹친다.
        #   다만 DataFrame 이 함께 메모리에 올라오므로, 메모리가 빠듯한
        #   환경에서는 --jobs 1 (기본)로 두는 편이 안전하다.
        logging.info(f"동시 {args.jobs}개로 처리합니다 ({len(rows)}개 공정)")
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            list(ex.map(lambda t: _one(*t), rows))
    else:
        for oid, desc in rows:
            _one(oid, desc)

    # ── 결과 ──────────────────────────────────────────────
    logging.info("=" * 56)
    logging.info(f"완료  성공 {ok} / 데이터없음 {empty} / 실패 {fail}"
                 f"  ({time.time()-t_all:.0f}s)")
    if failed_opers:
        logging.error(f"실패 공정: {', '.join(failed_opers)}")

    # 스케줄러가 실패를 인지하도록 exit code 반환
    return 1 if fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logging.warning("사용자 중단")
        sys.exit(130)
    except Exception:
        logging.error("예기치 못한 오류")
        logging.error(traceback.format_exc())
        sys.exit(1)
