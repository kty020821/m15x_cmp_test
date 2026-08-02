"""
equipment/issue_report.py
════════════════════════════════════════════════════════════
이슈 스캔 결과 → 한 파일 HTML 리포트

  파라미터별 판정과 차트를 한 문서로 모아 준다.
  브라우저에서 열고 Ctrl+P 로 PDF 저장까지 된다 (인쇄 CSS 포함).

────────────────────────────────────────────────────────────
왜 PDF 가 아니라 HTML 인가
  · PDF 를 만들려면 weasyprint/reportlab 같은 라이브러리가 필요한데
    사내 서버에 없을 수 있고, 렌더링이 512MiB 에서 무겁다.
  · 차트를 이미지로 굽는 과정(matplotlib)도 추가 의존성이다.
  · HTML 은 표준 라이브러리만으로 만들 수 있고, 인쇄하면 PDF 가 된다.
    파일 하나로 자체 완결이라 메일로 보내거나 공유 폴더에 둬도 그대로 열린다.

★ 차트는 SVG 를 직접 그린다 — 외부 스크립트를 안 쓰므로
  사내망에서 열어도 CDN 차단과 무관하게 보인다.
════════════════════════════════════════════════════════════
"""

import html
from datetime import datetime

from . import issue_service as iss

# 차트 크기 (인쇄 시 A4 가로폭에 맞춘 값)
W, H = 860, 190
PAD_L, PAD_R, PAD_T, PAD_B = 54, 16, 14, 26

# 상태별 색 — 화면과 같게 맞춘다
COLORS = {
    '이상': '#dc2626', '주의': '#f59e0b', '정상': '#16a34a',
    '참고': '#94a3b8', '데이터없음': '#94a3b8', '기준없음': '#94a3b8',
}
CP_COLOR = '#7c3aed'


def _e(v):
    return html.escape(str(v if v is not None else ''))


def _num(v, nd=3):
    if v is None:
        return '-'
    try:
        return f'{float(v):,.{nd}f}'.rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return str(v)


# ══════════════════════════════════════════════════════════
# SVG 차트
# ══════════════════════════════════════════════════════════
def _chart(it):
    """
    파라미터 1건의 추이 차트.
      · 지정 구간은 상태색, 나머지는 회색
      · 나머지 구간의 min/평균/max 를 가로 기준선으로
      · 변곡점은 세로 점선 + σ 라벨
    """
    ser = [p for p in (it.get('series') or []) if p.get('v') is not None]
    if len(ser) < 2:
        return '<div class="nochart">추이를 그릴 데이터가 부족합니다.</div>'

    b = it.get('base') or {}
    vals = [p['v'] for p in ser]
    lo = min(vals + [v for v in (b.get('min'),) if v is not None])
    hi = max(vals + [v for v in (b.get('max'),) if v is not None])
    if hi <= lo:
        hi = lo + 1
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad

    iw = W - PAD_L - PAD_R
    ih = H - PAD_T - PAD_B
    n = len(ser)

    def px(i):
        return PAD_L + (i * iw / (n - 1) if n > 1 else 0)

    def py(v):
        return PAD_T + ih - (v - lo) / (hi - lo) * ih

    color = COLORS.get(it.get('status'), '#64748b')
    parts = []

    # 지정 구간 배경 띠 — 어디를 지목했는지 한눈에
    spans, cur = [], None
    for i, p in enumerate(ser):
        if p.get('in_sel') and cur is None:
            cur = i
        elif not p.get('in_sel') and cur is not None:
            spans.append((cur, i - 1)); cur = None
    if cur is not None:
        spans.append((cur, n - 1))
    for a, z in spans:
        x1, x2 = px(a), px(z)
        parts.append(f'<rect x="{x1:.1f}" y="{PAD_T}" '
                     f'width="{max(x2 - x1, 2):.1f}" height="{ih}" '
                     f'fill="{color}" opacity="0.07"/>')

    # 기준선 (나머지 구간)
    for key, lab, col, dash in (('max', '나머지 max', '#ef4444', '4 3'),
                                ('avg', '나머지 평균', '#22c55e', '2 3'),
                                ('min', '나머지 min', '#ef4444', '4 3')):
        v = b.get(key)
        if v is None:
            continue
        y = py(v)
        if not (PAD_T <= y <= PAD_T + ih):
            continue
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" '
                     f'y2="{y:.1f}" stroke="{col}" stroke-width="1" '
                     f'stroke-dasharray="{dash}" opacity="0.8"/>')
        parts.append(f'<text x="{W - PAD_R - 2:.1f}" y="{y - 3:.1f}" '
                     f'text-anchor="end" class="ax" fill="{col}">'
                     f'{_e(lab)} {_num(v, 2)}</text>')

    # 점만 찍는다 (연결선 없음).
    #   측정값은 연속 신호가 아니라 웨이퍼·랏 단위의 개별 관측이라
    #   선으로 이으면 없는 추세가 있는 것처럼 보인다.
    for i, p in enumerate(ser):
        if p.get('in_sel'):
            parts.append(f'<circle cx="{px(i):.1f}" cy="{py(p["v"]):.1f}" '
                         f'r="2.6" fill="{color}" opacity="0.9"/>')
        else:
            parts.append(f'<circle cx="{px(i):.1f}" cy="{py(p["v"]):.1f}" '
                         f'r="1.9" fill="#94a3b8" opacity="0.55"/>')

    # 변곡점
    for c in (it.get('cps') or []):
        i = c.get('index')
        if i is None or not (0 <= i < n):
            continue
        x = px(i)
        parts.append(f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" '
                     f'y2="{PAD_T + ih}" stroke="{CP_COLOR}" '
                     f'stroke-width="1.4" stroke-dasharray="3 2"/>')
        sg = c.get('shift_sigma')
        lab = f'{c.get("direction", "")} {sg:+.1f}σ' if sg is not None else c.get('direction', '')
        parts.append(f'<text x="{x:.1f}" y="{PAD_T - 3:.1f}" '
                     f'text-anchor="middle" class="cp">{_e(lab)}</text>')

    # 축 (양 끝 시각과 값 범위만)
    parts.append(f'<text x="{PAD_L}" y="{H - 8}" class="ax">'
                 f'{_e(ser[0]["x"])}</text>')
    parts.append(f'<text x="{W - PAD_R}" y="{H - 8}" text-anchor="end" '
                 f'class="ax">{_e(ser[-1]["x"])}</text>')
    parts.append(f'<text x="{PAD_L - 5}" y="{PAD_T + 8}" text-anchor="end" '
                 f'class="ax">{_num(hi, 2)}</text>')
    parts.append(f'<text x="{PAD_L - 5}" y="{PAD_T + ih}" text-anchor="end" '
                 f'class="ax">{_num(lo, 2)}</text>')

    return (f'<svg class="chart" viewBox="0 0 {W} {H}" '
            f'xmlns="http://www.w3.org/2000/svg">' + ''.join(parts) + '</svg>')


# ══════════════════════════════════════════════════════════
# 리포트
# ══════════════════════════════════════════════════════════
CSS = """
:root { --tx:#1f2937; --tx2:#4b5563; --mut:#9ca3af; --bd:#e5e7eb;
        --bg:#ffffff; --bg2:#f9fafb;
        --bad:#dc2626; --warn:#f59e0b; --good:#16a34a; --cp:#7c3aed; }
* { box-sizing:border-box; }
body { margin:0; padding:26px 30px 60px; background:var(--bg); color:var(--tx);
  font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Segoe UI',
    'Noto Sans KR','Malgun Gothic',sans-serif; font-size:13px; line-height:1.6; }
.mono { font-family:'SF Mono','Consolas',monospace; }
h1 { font-size:20px; font-weight:800; margin:0 0 4px; letter-spacing:-.02em; }
h2 { font-size:15px; font-weight:800; margin:0; letter-spacing:-.01em; }
.sub { font-size:12px; color:var(--tx2); font-weight:600; margin-bottom:18px; }
.meta { display:flex; gap:18px; flex-wrap:wrap; font-size:12px; font-weight:600;
  color:var(--tx2); border:1px solid var(--bd); border-radius:12px;
  padding:12px 16px; background:var(--bg2); margin-bottom:16px; }
.meta b { color:var(--tx); font-family:'SF Mono','Consolas',monospace; }
.cards { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }
.card { border:1px solid var(--bd); border-radius:12px; padding:10px 16px;
  min-width:88px; }
.card .n { font-size:19px; font-weight:800; font-family:'SF Mono',monospace; }
.card .l { font-size:10px; font-weight:700; color:var(--mut); }
.card.bad .n{color:var(--bad);} .card.warn .n{color:var(--warn);}
.card.cp .n{color:var(--cp);}
table.idx { width:100%; border-collapse:collapse; margin-bottom:22px; }
table.idx th { font-size:10px; font-weight:800; color:var(--mut); text-align:left;
  padding:7px 8px; border-bottom:1.5px solid var(--bd); }
table.idx td { font-size:12px; padding:6px 8px; border-bottom:1px solid #f3f4f6; }
table.idx a { color:var(--tx); text-decoration:none; font-weight:700; }
.badge { display:inline-block; font-size:10px; font-weight:800; border-radius:20px;
  padding:2px 8px; }
.b-bad{color:var(--bad);background:#fef2f2;} .b-warn{color:var(--warn);background:#fffbeb;}
.b-good{color:var(--good);background:#f0fdf4;} .b-none{color:var(--mut);background:#f3f4f6;}
.tag { display:inline-block; font-size:10px; font-weight:700; color:var(--tx2);
  background:var(--bg2); border:1px solid var(--bd); border-radius:20px;
  padding:1px 7px; margin-right:3px; font-family:'SF Mono',monospace; }
.sec { border:1px solid var(--bd); border-radius:14px; padding:16px 18px;
  margin-bottom:14px; }
.sec.bad { border-color:#fecaca; } .sec.warn { border-color:#fde68a; }
.sec-h { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  margin-bottom:8px; }
.kv { display:flex; gap:16px; flex-wrap:wrap; font-size:12px; font-weight:600;
  color:var(--tx2); margin:8px 0; }
.kv b { color:var(--tx); font-family:'SF Mono',monospace; }
.rsn { font-size:12px; color:var(--tx); line-height:1.7; }
.chart { width:100%; height:auto; display:block; margin:10px 0 4px; }
.ax { font-size:9px; fill:var(--mut); font-family:'SF Mono',monospace; }
.cp { font-size:9px; fill:var(--cp); font-weight:700;
  font-family:'SF Mono',monospace; }
.cps { font-size:11px; color:var(--tx2); }
.cps b { color:var(--tx); }
.nochart { font-size:11px; color:var(--mut); padding:14px 0; }
.foot { font-size:11px; color:var(--mut); margin-top:24px;
  border-top:1px solid var(--bd); padding-top:12px; }
.top { font-size:10px; color:var(--mut); text-decoration:none; margin-left:auto; }

/* 인쇄(PDF 저장) — 파라미터 섹션이 페이지 경계에서 잘리지 않게 */
@media print {
  body { padding:0; font-size:11px; }
  .sec { page-break-inside:avoid; border-radius:0; }
  .idx-wrap { page-break-after:always; }
  .top { display:none; }
  a { color:inherit; text-decoration:none; }
}
@page { size:A4 landscape; margin:12mm; }
"""


def build_report(oper_id, lot_cd, sel, unit='lot', params=None,
                 only_issue=True, title=''):
    """
    이슈 스캔 결과를 HTML 문자열로 만든다.

      only_issue=True  이상·주의·구간내 변곡 항목만 (기본)
                       전체를 넣으면 파라미터 수만큼 차트가 생겨 파일이 커진다
      params           특정 파라미터만 지정할 수도 있다
    """
    res = iss.scan_all(oper_id, lot_cd, sel, unit=unit, params=params,
                       with_series=True)
    items = res.get('items') or []

    if only_issue:
        items = [i for i in items
                 if i['status'] in ('이상', '주의') or i.get('cp_in_sel')]

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    head = title or f'{oper_id} 이슈 구간 분석'
    lot_html = f'<span>LOT_CD <b>{_e(lot_cd)}</b></span>' if lot_cd else ''
    unit_txt = '랏 평균' if unit == 'lot' else '웨이퍼'
    scope_txt = '(이상·주의·변곡만)' if only_issue else '(전체)'

    # ── 요약 ─────────────────────────────────────────────
    cards = (
        f'<div class="card"><div class="n">{res.get("n_param", 0)}</div>'
        f'<div class="l">스캔</div></div>'
        f'<div class="card bad"><div class="n">{res.get("n_bad", 0)}</div>'
        f'<div class="l">이상</div></div>'
        f'<div class="card warn"><div class="n">{res.get("n_warn", 0)}</div>'
        f'<div class="l">주의</div></div>'
        f'<div class="card cp"><div class="n">{res.get("n_cp", 0)}</div>'
        f'<div class="l">구간 내 변곡</div></div>'
    )

    # ── 목차 ─────────────────────────────────────────────
    def badge(st):
        m = {'이상': 'b-bad', '주의': 'b-warn', '정상': 'b-good'}
        return f'<span class="badge {m.get(st, "b-none")}">{_e(st)}</span>'

    # ★ f-string 표현식 안에서 바깥과 같은 따옴표를 다시 쓰거나 백슬래시를
    #   넣는 건 Python 3.12 부터만 허용된다. 서버가 그 이전 버전이면
    #   SyntaxError 가 나므로, 값은 미리 만들어 두고 f-string 은 단순하게 쓴다.
    def _sig(v):
        return '-' if v is None else f'{v:+g}σ'

    idx_rows = []
    for n, it in enumerate(items):
        sig = _sig(it.get('sigma'))
        spread = f"{it['spread']}배" if it.get('spread') else '-'
        ncp = len(it.get('cps') or [])
        cp_mark = ' ●' if it.get('cp_in_sel') else ''
        idx_rows.append(
            '<tr>'
            f'<td><a href="#p{n}">{_e(it["param"])}</a></td>'
            f'<td><span class="tag">{_e(it.get("ptype"))}</span></td>'
            f'<td>{badge(it["status"])}</td>'
            f'<td class="mono">{sig}</td>'
            f'<td class="mono">{it.get("out_cnt", 0)}</td>'
            f'<td class="mono">{spread}</td>'
            f'<td class="mono">{ncp}곳{cp_mark}</td>'
            '</tr>')
    idx = ''.join(idx_rows)
    idx_body = idx or '<tr><td colspan="7">항목 없음</td></tr>'

    # ── 파라미터별 상세 ──────────────────────────────────
    secs = []
    for n, it in enumerate(items):
        s, b = it.get('sel') or {}, it.get('base') or {}
        cls = 'bad' if it['status'] == '이상' else ('warn' if it['status'] == '주의' else '')
        tags = ''.join(f'<span class="tag">{_e(c)}</span>'
                       for c in (it.get('checks') or []))
        cps = it.get('cps') or []
        cp_lines = []
        for c in cps:
            # 따옴표가 섞인 조각은 미리 만들어 둔다 (f-string 안에서 조립하지 않는다)
            mark = ' <b style="color:#dc2626">구간 내</b>' if c.get('in_sel') else ''
            sg = c.get('shift_sigma')
            sg_txt = f'({sg:+g}σ)' if sg is not None else ''
            cp_lines.append(
                f'<div>· {_e(c["at"])} <b>{_e(c["direction"])}</b> '
                f'{_num(c["before_avg"], 2)} → {_num(c["after_avg"], 2)} '
                f'{sg_txt}{mark}</div>')
        cp_html = ''.join(cp_lines)

        # 조건부 조각은 미리 만들어 둔다 (f-string 안에서 조립하지 않는다)
        sig_html = ''
        if it.get('sigma') is not None:
            sig_html = f"<span>차이 <b>{it['sigma']:+g}σ</b></span>"
        spread_html = ''
        if it.get('spread'):
            spread_html = f"<span>산포비 <b>{it['spread']}배</b></span>"
        cps_html = ''
        if cp_html:
            cps_html = f'<div class="cps">{cp_html}</div>'
        reason_html = '<br>'.join('· ' + _e(x) for x in (it.get('reasons') or []))

        secs.append(f'''
<div class="sec {cls}" id="p{n}">
  <div class="sec-h">
    <h2>{_e(it['param'])}</h2>
    <span class="tag">{_e(it.get('ptype'))}</span>
    {badge(it['status'])}
    {tags}
    <a class="top" href="#idx">↑ 목록</a>
  </div>
  <div class="rsn">{reason_html}</div>
  <div class="kv">
    <span>지정 구간 <b>{s.get('n', 0)}장</b> 평균 <b>{_num(s.get('avg'))}</b>
      σ <b>{_num(s.get('std'))}</b></span>
    <span>나머지 <b>{b.get('n', 0)}장</b> 평균 <b>{_num(b.get('avg'))}</b>
      σ <b>{_num(b.get('std'))}</b></span>
    {sig_html}
    {spread_html}
    <span>범위밖 <b>{it.get('out_cnt', 0)}장</b></span>
  </div>
  {_chart(it)}
  {cps_html}
</div>''')

    body_secs = ''.join(secs) or \
        '<div class="sec"><div class="rsn">해당하는 항목이 없습니다. ' \
        '지정한 구간에서 이상·주의·변곡으로 잡힌 파라미터가 없습니다.</div></div>'

    return f'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>{_e(head)} — {now}</title>
<style>{CSS}</style></head><body>
<h1>{_e(head)}</h1>
<div class="sub">이슈 구간 분석 리포트 · {now} 생성</div>

<div class="meta">
  <span>공정 <b>{_e(oper_id)}</b></span>
  {lot_html}
  <span>지정 구간 <b>{_e(res.get('sel_desc'))}</b></span>
  <span>변곡 단위 <b>{unit_txt}</b></span>
  <span>수록 <b>{len(items)}개</b>
    {scope_txt}</span>
</div>

<div class="cards">{cards}</div>

<div class="idx-wrap" id="idx">
  <table class="idx">
    <thead><tr><th>파라미터</th><th>타입</th><th>판정</th><th>σ</th>
      <th>범위밖</th><th>산포</th><th>변곡점</th></tr></thead>
    <tbody>{idx_body}</tbody>
  </table>
</div>

{body_secs}

<div class="foot">
  판정 기준 — 평균 이탈 {iss.SIGMA_WARN}σ 주의 / {iss.SIGMA_ALERT}σ 이상,
  나머지 범위 밖 웨이퍼 {iss.OUT_WARN}장 주의 / {iss.OUT_ALERT}장 이상,
  산포 {iss.SPREAD_WARN}배 주의 / {iss.SPREAD_ALERT}배 이상.
  비교 기준은 전체가 아니라 '지정 구간을 뺀 나머지' 입니다.<br>
  차트 — 옅은 띠와 진한 점이 지정 구간, 가로 점선은 나머지 구간의
  min·평균·max, 세로 점선은 변곡점입니다.<br>
  이 문서는 브라우저에서 인쇄(Ctrl+P)하면 PDF 로 저장됩니다.
</div>
</body></html>'''
