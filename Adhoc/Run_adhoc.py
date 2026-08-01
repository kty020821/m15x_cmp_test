"""
run_adhoc.py  (프로젝트 루트)
════════════════════════════════════════════════════════════
1회성 조회 요청 실행 러너

  웹은 Lake 를 못 읽으므로, 웹에서 등록한 조회 요청을 여기서 처리한다.
  사내 스케줄러에 1~5분 주기로 걸어 두면 사실상 자동으로 동작한다.

    python run_adhoc.py             대기 건 처리 (기본 3건)
    python run_adhoc.py --limit 1   1건만
    python run_adhoc.py --job 12    특정 요청만 (실패 건 재실행에도 사용)
    python run_adhoc.py --cleanup   보관 기간 지난 결과 정리

  ※ 정기 적재(run_analysis_load.py)와 같은 조회 함수를 쓴다.
    다르게 만들면 같은 데이터인데 결과가 달라진다.
════════════════════════════════════════════════════════════
"""

import os
import sys
import argparse
import traceback
from datetime import datetime

import django

# ★ 사내 프로젝트의 settings 모듈 경로로 바꿀 것
#   run_analysis_load.py 에 있는 값과 같아야 한다.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from equipment import adhoc_service as ah      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=3,
                    help='한 번에 처리할 대기 건수 (기본 3)')
    ap.add_argument('--job', type=int, default=None,
                    help='특정 요청만 실행 (실패 건 재실행 포함)')
    ap.add_argument('--cleanup', action='store_true',
                    help='보관 기간 지난 결과만 정리하고 종료')
    args = ap.parse_args()

    started = datetime.now()
    print(f'[adhoc] 시작 {started:%Y-%m-%d %H:%M:%S}')

    try:
        # 오래된 1회성 결과부터 정리한다 (테이블이 무한정 쌓이지 않게)
        ah.cleanup()

        if args.cleanup:
            print('[adhoc] 정리만 수행하고 종료')
            return 0

        if args.job:
            res = ah.run_job(args.job)
            ok = bool(res.get('ok'))
            print(f'[adhoc] 요청 {args.job}: '
                  f'{"완료 " + str(res.get("rows", 0)) + "행" if ok else "실패 " + str(res.get("error"))}')
            return 0 if ok else 1

        res = ah.run_pending(limit=args.limit)
        if not res['processed']:
            print('[adhoc] 대기 중인 요청 없음')
            return 0

        fail = 0
        for r in res['results']:
            if r.get('ok'):
                print(f'  요청 {r["job_id"]}: 완료 {r.get("rows", 0):,}행')
            else:
                fail += 1
                print(f'  요청 {r["job_id"]}: 실패 — {r.get("error")}')

        print(f'[adhoc] 처리 {res["processed"]}건 (실패 {fail}건) · '
              f'소요 {(datetime.now() - started).seconds}초')
        return 1 if fail else 0

    except Exception as e:
        print(f'[adhoc] 러너 오류: {e.__class__.__name__}: {e}')
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
