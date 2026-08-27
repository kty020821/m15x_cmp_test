"""
equipment/management/commands/fresh.py

  DB 최신 데이터 시각 확인.

    python manage.py fresh              전 공정
    python manage.py fresh V5071000B    한 공정만

  ★ 적재는 '돌린 시점' 의 Lake 데이터까지만 가져온다.
    07시에 돌렸으면 그 뒤 들어온 건 다음 적재 때 붙는다.
    그래서 마지막 적재 시각을 함께 보여 준다 — 그래야
    '누락' 인지 '아직 안 받은 것' 인지 구분된다.

  ※ equipment/management/__init__.py 와
    equipment/management/commands/__init__.py (빈 파일) 이 필요합니다.
"""
import re
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import connections


class Command(BaseCommand):
    help = 'DB 최신 데이터 시각과 지연을 보여줍니다'

    def add_arguments(self, ap):
        ap.add_argument('oper_id', nargs='?', help='공정 코드 (생략하면 전체)')

    def handle(self, *a, **o):
        oper_id = o.get('oper_id')
        now = datetime.now()

        with connections['analysis_db'].cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables "
                        "WHERE tablename LIKE 'cmp_analysis_%' ORDER BY 1")
            tables = [r[0] for r in cur.fetchall()]

            if oper_id:
                want = 'cmp_analysis_' + re.sub(
                    r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()
                tables = [t for t in tables if t == want]
                if not tables:
                    self.stdout.write(f'{oper_id} 적재 테이블이 없습니다')
                    return

            # 마지막 적재 기록 (없어도 그만)
            last = {}
            try:
                cur.execute("""
                    SELECT DISTINCT ON (oper_id) oper_id, finished_at
                    FROM cmp_load_job WHERE status = '완료'
                    ORDER BY oper_id, finished_at DESC
                """)
                last = {r[0].lower(): r[1] for r in cur.fetchall()}
            except Exception:
                pass

            self.stdout.write(f'\n지금 {now:%Y-%m-%d %H:%M}')
            self.stdout.write('=' * 72)
            self.stdout.write(f'{"공정":<20}{"최신 데이터":<21}'
                              f'{"지연":>8}  {"마지막 적재":<16}')
            self.stdout.write('-' * 72)

            for t in tables:
                oid = t.replace('cmp_analysis_', '')
                try:
                    cur.execute(f'SELECT MAX("DATE") FROM {t}')
                    mx = cur.fetchone()[0]
                except Exception as e:
                    self.stdout.write(f'{oid.upper():<20}'
                                      f'조회 실패: {e.__class__.__name__}')
                    continue

                if mx:
                    h = (now - mx).total_seconds() / 3600
                    gap = f'{h:.0f}시간' if h < 48 else f'{h/24:.1f}일'
                else:
                    gap = '-'

                fin = last.get(oid)
                self.stdout.write(
                    f'{oid.upper():<20}'
                    f'{str(mx)[:19] if mx else "(없음)":<21}'
                    f'{gap:>8}  '
                    f'{str(fin)[:16] if fin else "-":<16}')

            self.stdout.write('-' * 72)
            self.stdout.write(
                '지연 = 지금 - DB 최신 데이터. 마지막 적재 이후 시간만큼은\n'
                '원래 비어 있습니다 (그 뒤 데이터는 다음 적재 때 붙습니다).')
