"""
run_monitoring.py   (프로젝트 루트 · manage.py 옆)
════════════════════════════════════════════════════════════
아침 점검용 이상 탐지 배치.
run_analysis_load.py 가 적재를 끝낸 뒤에 실행한다.

  python run_monitoring.py                  어제 기준
  python run_monitoring.py --date 20260722  특정 일자
  python run_monitoring.py --oper OP100     특정 공정만
  python run_monitoring.py --dry-run        저장하지 않고 결과만 출력
════════════════════════════════════════════════════════════
"""

import os
import sys
import argparse
import traceback
from pathlib import Path
from datetime import datetime, timedelta, date

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from equipment.monitoring import detect_all, save_anomalies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date',    default=None, help='YYYYMMDD (기본: 어제)')
    ap.add_argument('--oper',    default=None, help='특정 oper_id 만')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    target = (datetime.strptime(args.date, '%Y%m%d').date()
              if args.date else date.today() - timedelta(days=1))
    opers = [args.oper] if args.oper else None

    print(f'=== 이상 탐지 : {target} ===')

    try:
        target, items = detect_all(target=target, opers=opers)
    except Exception:
        traceback.print_exc()
        return 1

    if not items:
        print('이상 없음')
        return 0

    # 심각도 순 출력
    print(f'\n{len(items)}건 (심각도 순)\n')
    print(f'{"공정":<12} {"제품":<5} {"파라미터":<22} {"범위":<18} '
          f'{"규칙":<6} {"σ":>6} {"건수":>5}')
    print('-' * 88)
    for it in items[:40]:
        mark = ''
        if it.get('eqp_only'):
            mark += ' ★장비단독'
        if it.get('low_confidence'):
            mark += ' (수량적음)'
        print(f'{it["oper_id"]:<12} {it["lot_cd"]:<5} {it["param"][:22]:<22} '
              f'{it["scope"][:18]:<18} {it["rule"]:<6} '
              f'{it["severity"]:>6.2f} {it["n_hit"]:>5}{mark}')

    if len(items) > 40:
        print(f'... 외 {len(items) - 40}건')

    if args.dry_run:
        print('\n[dry-run] 저장하지 않음')
        return 0

    save_anomalies(target, items)
    return 0


if __name__ == '__main__':
    sys.exit(main())
