"""
run_analysis_load.py   (프로젝트 루트 · manage.py 옆)
════════════════════════════════════════════════════════════
산포 분석 데이터 배치 적재. 사내 스케줄러가 이 파일을 실행한다.

  python run_analysis_load.py                  전체 공정, 30일
  python run_analysis_load.py --oper OP100     특정 공정만
  python run_analysis_load.py --days 7         기간 변경
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
def acquire_lock():
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < LOCK_STALE_SEC:
            pid = LOCK_PATH.read_text().strip()
            logging.warning(f"이미 실행 중 (pid={pid}, {age/60:.0f}분 경과) → 이번 회차 건너뜀")
            return False
        logging.warning(f"오래된 락 발견 ({age/3600:.1f}시간) → 무시하고 진행")

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
    ap.add_argument('--dry-run', action='store_true',  help='저장하지 않고 조회만')

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

    if not acquire_lock():
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

    for _, row in opers.iterrows():
        oper_id   = row['OPER_ID']
        oper_desc = row.get('OPER_DESC', '')
        t0 = time.time()
        df = None
        try:
            logging.info(f"── [{oper_id}] {oper_desc}")
            df = build_analysis_df(lake, df_info, oper_id, days=args.days)

            if df is None or df.empty:
                empty += 1
                logging.warning(f"   데이터 없음 (건너뜀)")
                continue

            if args.dry_run:
                logging.info(f"   [dry-run] {len(df):,}행 · {len(df.columns)}컬럼 "
                             f"({time.time()-t0:.0f}s)")
            else:
                save_analysis_df(df, oper_id)
                logging.info(f"   완료 {len(df):,}행 ({time.time()-t0:.0f}s)")
            ok += 1

        except Exception:
            fail += 1
            failed_opers.append(str(oper_id))
            logging.error(f"   [{oper_id}] 실패")
            logging.error(traceback.format_exc())
        finally:
            # 공정 수가 많으므로 매번 정리 (메모리 누적 방지)
            df = None
            gc.collect()

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
