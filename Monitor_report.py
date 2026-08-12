"""
equipment/monitor_report.py
════════════════════════════════════════════════════════════
인라인 점검 결과 → 한 파일 HTML 리포트

  아침 점검 결과를 그대로 공유·보관할 수 있게 만든다.
  브라우저에서 열고 Ctrl+P 로 PDF 저장까지 된다.

  ★ 이슈 리포트(issue_report)와 같은 방식 —
    외부 스크립트 없이 SVG 를 직접 그리므로 사내망에서도 열리고,
    파일 하나로 자체 완결이라 메일·공유폴더로 그대로 전달된다.

  ★ 이슈 리포트와 다른 점: 대상이 '사용자가 지목한 구간' 이 아니라
    '어제(최근일) 대 30일 기준선' 이다. 그래서 차트도 구간 비교가 아니라
    일자별 추이가 중심이고, 점검일 구간을 배경 띠로 표시한다.
════════════════════════════════════════════════════════════
"""

import html
from datetime import datetime

from . import monitor_service as ms

# 차트 크기 — 한 화면·한 페이지에 여러 개가 들어가게
W, H = 560, 170
PAD_L, PAD_R, PAD_T, PAD_B = 50, 14, 18, 24

COLORS = {
    '이상': '#dc2626', '주의': '#f59e0b', '정상': '#16a34a',
    '참고': '#94a3b8', '데이터없음': '#94a3b8', '기준없음': '#94a3b8',
}
C_BASE = '#94a3b8'


def _e(v):
    return html.escape(str(v if v is not None else ''))


def _num(v, nd=3):
    if v is None:
        return '-'
    try:
        return f'{float(v):,.{nd}f}'.rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return str(v)


def _sig(v):
    return '-' if v is None else f'{v:+g}σ'


# ══════════════════════════════════════════════════════════
# 일자별 추이 차트
# ══════════════════════════════════════════════════════════
def _chart(r):
    """
    30일 일자별 평균 추이.
      · 점검일은 상태색, 나머지는 회색
      · 기준선(30일 평균·min·max)을 가로 점선으로
    """
    ser = [p for p in (r.get('series') or []) if p.get('avg') is not None]
    if len(ser) < 2:
        return '<div class="nochart">추이를 그릴 데이터가 부족합니다.</div>'

    b = r.get('base_stat') or {}
    day = str(r.get('day') or '')[:10]
    d_from = str(r.get('day_from') or '')[:10]

    vals = [p['avg'] for p in ser]
    cand = [v for v in (b.get('min'), b.get('max')) if v is not None]
    lo, hi = min(vals + cand), max(vals + cand)
    if hi <= lo:
        hi = lo + 1
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad

    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    n = len(ser)
    px = lambda i: PAD_L + (i * iw / (n - 1) if n > 1 else 0)
    py = lambda v: PAD_T + ih - (v - lo) / (hi - lo) * ih

    color = COLORS.get(r.get('status'), '#64748b')
    parts = []

    # 점검 구간 배경 띠 — 어느 날을 판정했는지
    idx = [i for i, p in enumerate(ser)
           if d_from <= str(p['d'])[:10] <= day] if day else []
    if idx:
        x1, x2 = px(idx[0]), px(idx[-1])
        parts.append(f'<rect x="{x1:.1f}" y="{PAD_T}" '
                     f'width="{max(x2 - x1, 3):.1f}" height="{ih}" '
                     f'fill="{color}" opacity="0.09"/>')

    # 기준선
    for key, lab, dash in (('max', '30일 max', '4 3'),
                           ('avg', '30일 평균', '2 3'),
                           ('min', '30일 min', '4 3')):
        v = b.get(key)
        if v is None:
            continue
        y = py(v)
        if not (PAD_T <= y <= PAD_T + ih):
            continue
        col = '#22c55e' if key == 'avg' else '#ef4444'
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" '
                     f'y2="{y:.1f}" stroke="{col}" stroke-width="1" '
                     f'stroke-dasharray="{dash}" opacity="0.8"/>')
        parts.append(f'<text x="{W - PAD_R - 2:.1f}" y="{y - 3:.1f}" '
                     f'text-anchor="end" class="ax" fill="{col}">'
                     f'{_e(lab)} {_num(v, 2)}</text>')

    # 일자별 평균 — 선으로 잇는다 (하루 1점이라 추이가 의미 있다)
    d = ' '.join(f'{"M" if i == 0 else "L"}{px(i):.1f},{py(p["avg"]):.1f}'
                 for i, p in enumerate(ser))
    parts.append(f'<path d="{d}" fill="none" stroke="{C_BASE}" '
                 f'stroke-width="1.2" opacity="0.7"/>')
    for i, p in enumerate(ser):
        on = i in idx
        parts.append(f'<circle cx="{px(i):.1f}" cy="{py(p["avg"]):.1f}" '
                     f'r="{3.2 if on else 2}" '
                     f'fill="{color if on else C_BASE}" '
                     f'fill-opacity="{0.95 if on else 0.55}"/>')

    parts.append(f'<text x="{PAD_L}" y="{H - 6}" class="ax">'
                 f'{_e(str(ser[0]["d"])[:10])}</text>')
    parts.append(f'<text x="{W - PAD_R}" y="{H - 6}" text-anchor="end" '
                 f'class="ax">{_e(str(ser[-1]["d"])[:10])}</text>')

    return (f'<svg class="chart" viewBox="0 0 {W} {H}" '
            f'xmlns="http://www.w3.org/2000/svg">' + ''.join(parts) + '</svg>')


# ══════════════════════════════════════════════════════════
CSS = """
:root { --tx:#1f2937; --tx2:#4b5563; --mut:#9ca3af; --bd:#e5e7eb;
        --bg:#fff; --bg2:#f9fafb;
        --bad:#dc2626; --warn:#f59e0b; --good:#16a34a; }
* { box-sizing:border-box; }
body { margin:0; padding:26px 30px 60px; background:var(--bg); color:var(--tx);
  font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Segoe UI',
    'Noto Sans KR','Malgun Gothic',sans-serif; font-size:13px; line-height:1.6; }
.mono { font-family:'SF Mono','Consolas',monospace; }
h1 { font-size:20px; font-weight:800; margin:0 0 4px; letter-spacing:-.02em; }
h2 { font-size:15px; font-weight:800; margin:0; }
h3 { font-size:13px; font-weight:800; margin:22px 0 8px;
     padding-bottom:5px; border-bottom:1.5px solid var(--bd); }
.sub { font-size:12px; color:var(--tx2); font-weight:600; margin-bottom:16px; }
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
.card.good .n{color:var(--good);}
table.idx { width:100%; border-collapse:collapse; margin-bottom:6px; }
table.idx th { font-size:10px; font-weight:800; color:var(--mut); text-align:left;
  padding:7px 8px; border-bottom:1.5px solid var(--bd); white-space:nowrap; }
table.idx td { font-size:12px; padding:6px 8px; border-bottom:1px solid #f3f4f6;
  vertical-align:top; }
table.idx a { color:var(--tx); text-decoration:none; font-weight:700; }
.badge { display:inline-block; font-size:10px; font-weight:800; border-radius:20px;
  padding:2px 8px; white-space:nowrap; }
.b-bad{color:var(--bad);background:#fef2f2;} .b-warn{color:var(--warn);background:#fffbeb;}
.b-good{color:var(--good);background:#f0fdf4;} .b-none{color:var(--mut);background:#f3f4f6;}
.tag { display:inline-block; font-size:10px; font-weight:700; color:var(--tx2);
  background:var(--bg2); border:1px solid var(--bd); border-radius:20px;
  padding:1px 7px; margin-right:3px; font-family:'SF Mono',monospace; }
.secs { display:grid; grid-template-columns:repeat(auto-fit,minmax(520px,1fr));
  gap:12px; align-items:start; }
.sec { border:1px solid var(--bd); border-radius:14px; padding:13px 15px; }
.sec.bad { border-color:#fecaca; } .sec.warn { border-color:#fde68a; }
.sec-h { display:flex; align-items:center; gap:8px; flex-wrap:wrap;
  margin-bottom:6px; }
.kv { display:flex; gap:12px; flex-wrap:wrap; font-size:11px; font-weight:600;
  color:var(--tx2); margin:6px 0; }
.kv b { color:var(--tx); font-family:'SF Mono',monospace; }
.rsn { font-size:11px; color:var(--tx); line-height:1.6; }
.chart { width:100%; height:auto; display:block; margin:6px 0 2px; }
.ax { font-size:9px; fill:var(--mut); font-family:'SF Mono',monospace; }
.eqp { display:flex; gap:6px; flex-wrap:wrap; margin-top:4px; }
.nochart { font-size:11px; color:var(--mut); padding:12px 0; }
.foot { font-size:11px; color:var(--mut); margin-top:24px;
  border-top:1px solid var(--bd); padding-top:12px; }
.top { font-size:10px; color:var(--mut); text-decoration:none; margin-left:auto; }

@media print {
  body { padding:0; font-size:10px; }
  .secs { grid-template-columns:1fr 1fr; gap:8px; }
  .sec { page-break-inside:avoid; border-radius:0; padding:10px 12px; }
  .idx-wrap { page-break-after:always; }
  .top { display:none; }
  a { color:inherit; text-decoration:none; }
}
@page { size:A4 landscape; margin:12mm; }
"""


def _badge(st):
    m = {'이상': 'b-bad', '주의': 'b-warn', '정상': 'b-good'}
    return f'<span class="badge {m.get(st, "b-none")}">{_e(st)}</span>'


def build_report(only_issue=True, oper_id=None, title=''):
    """
    저장된 최근 점검 결과로 리포트를 만든다.

      only_issue=True  이상·주의만 (기본)
      oper_id          특정 공정만

    ★ 점검을 다시 돌리지 않는다 — 화면에서 본 그 결과를 그대로 문서로
      옮기는 것이 목적이다. 다시 돌리면 화면과 내용이 달라진다.
    """
    data = ms.load_results()
    rows = data.get('results') or []

    if oper_id:
        rows = [r for r in rows if str(r.get('oper_id') or '').upper()
                == str(oper_id).upper()]
    total = len(rows)

    n_bad = sum(1 for r in rows if r.get('status') == '이상')
    n_warn = sum(1 for r in rows if r.get('status') == '주의')
    n_ok = sum(1 for r in rows if r.get('status') == '정상')
    n_skip = total - n_bad - n_warn - n_ok

    items = [r for r in rows if r.get('status') in ('이상', '주의')] \
        if only_issue else rows

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    head = title or '인라인 점검 리포트'
    run_ts = str(data.get('run_ts') or '')[:19]
    days = sorted({str(r.get('day'))[:10] for r in rows if r.get('day')})
    day_txt = f'{days[-1]}' if days else '-'

    # ── 목차 (공정별로 묶는다) ───────────────────────────
    by_oper = {}
    for i, r in enumerate(items):
        by_oper.setdefault(r.get('oper_label') or r.get('oper_id') or '-',
                           []).append((i, r))

    idx_parts = []
    for label, group in by_oper.items():
        body = ''.join(
            f'<tr><td><a href="#p{i}">{_e(r.get("param"))}</a></td>'
            f'<td class="mono">{_e(r.get("lot_cd"))}</td>'
            f'<td><span class="tag">{_e(r.get("ptype"))}</span></td>'
            f'<td>{_badge(r.get("status"))}</td>'
            f'<td class="mono">{_sig(r.get("sigma"))}</td>'
            f'<td class="mono">{r.get("out_cnt", 0)}</td>'
            f'<td class="mono">{"".join(_e(c)[0] for c in (r.get("checks") or []))}</td>'
            f'<td style="font-size:11px;color:var(--tx2);">'
            f'{"<br>".join(_e(x) for x in (r.get("reasons") or [])[:2])}</td></tr>'
            for i, r in group)
        idx_parts.append(
            f'<h3>{_e(label)} <span style="font-weight:600;color:var(--mut);">'
            f'· {len(group)}건</span></h3>'
            f'<table class="idx"><thead><tr>'
            f'<th>파라미터</th><th>LOT_CD</th><th>타입</th><th>판정</th>'
            f'<th>σ</th><th>범위밖</th><th>검사</th><th>근거</th>'
            f'</tr></thead><tbody>{body}</tbody></table>')

    # ── 파라미터별 상세 ──────────────────────────────────
    secs = []
    for i, r in enumerate(items):
        d, b = r.get('day_stat') or {}, r.get('base_stat') or {}
        cls = ('bad' if r.get('status') == '이상'
               else 'warn' if r.get('status') == '주의' else '')
        tags = ''.join(f'<span class="tag">{_e(c)}</span>'
                       for c in (r.get('checks') or []))
        reason_html = '<br>'.join('· ' + _e(x)
                                  for x in (r.get('reasons') or []))
        eqp = r.get('eqp') or []
        eqp_html = ''
        if eqp:
            chip_list = []
            for e in eqp:
                sg = e.get('sigma')
                sg_txt = '' if sg is None else f' ({sg:+g}σ)'
                chip_list.append(
                    f'<span class="tag">{_e(e.get("eqp"))} · '
                    f'{_num(e.get("avg"), 2)}{sg_txt} · '
                    f'{e.get("n", 0)}장</span>')
            eqp_html = '<div class="eqp">' + ''.join(chip_list) + '</div>'

        sigma_html = ('' if r.get('sigma') is None
                      else f'<span>차이 <b>{r["sigma"]:+g}σ</b></span>')
        spread_html = (f'<span>산포비 <b>{r["spread"]}배</b></span>'
                       if r.get('spread') else '')
        drift_html = (f'<span>드리프트 <b>{r["drift"]}</b></span>'
                      if r.get('drift') else '')

        secs.append(f'''
<div class="sec {cls}" id="p{i}">
  <div class="sec-h">
    <h2>{_e(r.get('param'))}</h2>
    <span class="tag">{_e(r.get('ptype'))}</span>
    {_badge(r.get('status'))}
    <span class="mono" style="font-size:11px;color:var(--mut);">
      {_e(r.get('oper_label') or r.get('oper_id'))} · {_e(r.get('lot_cd'))}</span>
    {tags}
    <a class="top" href="#idx">↑ 목록</a>
  </div>
  <div class="rsn">{reason_html}</div>
  <div class="kv">
    <span>점검일 <b>{d.get('n', 0)}장</b> 평균 <b>{_num(d.get('avg'))}</b>
      σ <b>{_num(d.get('std'))}</b></span>
    <span>30일 <b>{b.get('n', 0)}장</b> 평균 <b>{_num(b.get('avg'))}</b>
      σ <b>{_num(b.get('std'))}</b></span>
    {sigma_html}{spread_html}{drift_html}
    <span>범위밖 <b>{r.get('out_cnt', 0)}장</b></span>
  </div>
  {_chart(r)}
  {eqp_html}
</div>''')

    body_secs = ('<div class="secs">' + ''.join(secs) + '</div>') if secs else \
        '<div class="sec"><div class="rsn">해당하는 항목이 없습니다.</div></div>'

    scope = '(이상·주의만)' if only_issue else '(전체)'
    if oper_id:
        oper_html = f'<span>공정 <b>{_e(oper_id)}</b></span>'
    else:
        n_oper = len({r.get('oper_id') for r in rows})
        oper_html = f'<span>공정 <b>{n_oper}개</b></span>'

    return f'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>{_e(head)} — {day_txt}</title>
<style>{CSS}</style></head><body>
<h1>{_e(head)} — {_e(day_txt)}</h1>
<div class="sub">{now} 생성 · 점검 실행 {_e(run_ts or "-")}</div>

<div class="meta">
  {oper_html}
  <span>점검일 <b>{_e(day_txt)}</b></span>
  <span>점검 항목 <b>{total}건</b></span>
  <span>수록 <b>{len(items)}건</b> {scope}</span>
</div>

<div class="cards">
  <div class="card"><div class="n">{total}</div><div class="l">점검</div></div>
  <div class="card bad"><div class="n">{n_bad}</div><div class="l">이상</div></div>
  <div class="card warn"><div class="n">{n_warn}</div><div class="l">주의</div></div>
  <div class="card good"><div class="n">{n_ok}</div><div class="l">정상</div></div>
  <div class="card"><div class="n">{n_skip}</div><div class="l">판정제외</div></div>
</div>

<div class="idx-wrap" id="idx">
  {''.join(idx_parts) or '<div class="rsn">항목이 없습니다.</div>'}
</div>

{body_secs}

<div class="foot">
  판정 기준 — 점검일 평균이 30일 기준선 대비 {ms.SIGMA_WARN}σ 주의 /
  {ms.SIGMA_ALERT}σ 이상, 30일 범위 밖 웨이퍼 {ms.OUT_WARN}장 주의 /
  {ms.OUT_ALERT}장 이상, 산포 {ms.SPREAD_WARN}배 주의 / {ms.SPREAD_ALERT}배 이상.
  검사 약어 — L 수준이탈 · R 범위이탈 · E 단독이탈 · S 산포확대 · D 드리프트.<br>
  차트 — 일자별 평균 추이이며 색이 진한 구간이 점검일, 가로 점선은 30일
  평균·min·max 입니다. 소모품(PART) 계열은 판정에서 제외됩니다.<br>
  이 문서는 브라우저에서 인쇄(Ctrl+P)하면 PDF 로 저장됩니다.
</div>
</body></html>'''
