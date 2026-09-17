"""MCRS 모니터링 — 이 파일 하나를 equipment/mcrs_monitor.py에 넣어 사용.

필수: pandas. Django 화면 사용 시에만 django도 필요합니다.

입력 (기존 두 쿼리를 실행한 결과 DataFrame):
    pm_df   : EQP_ID, EVENT_TM
    mcrs_df : EQP_ID, DOI_NM, HUB_LOAD_TM (그 외 컬럼은 있어도 됩니다)
    컬럼명은 대소문자 모두 허용합니다.

사용 예:
    from .mcrs_monitor import get_mcrs_summary, render_mcrs_page
    summary_df = get_mcrs_summary(pm_df, mcrs_df)

기존 Django views.py에 추가:
    def mcrs_page(request):
        pm_df = ...    # 실제 PM 조회 쿼리 결과
        mcrs_df = ...  # 실제 MCRS 조회 쿼리 결과
        return render_mcrs_page(request, pm_df, mcrs_df)

기존 equipment/urls.py의 urlpatterns에 추가:
    path('mcrs/', views.mcrs_page, name='mcrs'),

별도 HTML/CSS 파일 없이 기존 base.html을 상속해 페이지를 표시합니다.
DB 연결, 쿼리 실행, 메뉴 추가는 자동으로 수행하지 않습니다.

집계 기준:
- 입력 데이터는 실제 PM 코드 / 장비 / LOT_CD / EQP_DOWN_YN / MCRS_TYP
  조건으로 이미 필터링되어 있다고 가정합니다. 이 함수는 그 조건을 추측하지 않습니다.
- EQP_ID별 최신 PM 이후(HUB_LOAD_TM > EVENT_TM)만 집계합니다.
- EQP_ID + DOI_NM별 원본 한 행 = 1건. 중복 행도 각각 셉니다.
- PM과 같은 시각은 제외하며, 새 PM은 재조회 시 자동 반영됩니다.
- DOI가 NULL인 이벤트는 별도 그룹으로 집계합니다.
- PM 이후 이벤트가 없는 장비는 0건으로 표시합니다.
- PM 이력이 없는 장비는 기준 시각을 알 수 없어 제외합니다.
- 두 시간 컬럼은 동일한 시간대여야 합니다. HUB_LOAD_TM은 적재 시각일 수
  있으므로 실제 발생 시각 기준이 필요하면 입력 컬럼을 바꿔서 넘기세요.
- 임의의 최근 N일 제한으로 데이터를 잘라 넘기면 전체 누적이 아닐 수 있습니다.

샘플 실행:
    python mcrs_monitor.py
"""

import pandas as pd


OUTPUT_COLUMNS = [
    'EQP_ID', 'LAST_PM_TM', 'DOI_NM', 'MCRS_COUNT',
    'FIRST_MCRS_TM', 'LAST_MCRS_TM',
]


def _prepare(df, required, label):
    """입력 원본을 수정하지 않고 컬럼명과 필수 항목을 검사합니다."""
    result = df.copy()
    result.columns = [str(col).strip().upper() for col in result.columns]
    if result.columns.duplicated().any():
        raise ValueError(f'{label}: 대소문자 정리 후 중복 컬럼이 있습니다.')
    missing = set(required) - set(result.columns)
    if missing:
        raise ValueError(f'{label}: 필수 컬럼 누락: {sorted(missing)}')
    result = result[list(required)].copy()
    if result['EQP_ID'].isna().any():
        raise ValueError(f'{label}: EQP_ID가 비어 있는 행이 있습니다.')
    # 장비/챔버 ID를 임의로 자르거나 합치지 않습니다.
    result['EQP_ID'] = result['EQP_ID'].astype(str)
    return result


def _timestamps(series, label):
    """잘못된 시간을 조용히 버리면 건수가 줄어드므로 오류로 알립니다."""
    if series.isna().any():
        raise ValueError(f'{label}: 시간 값이 비어 있습니다.')
    if not series.empty and pd.api.types.is_numeric_dtype(series):
        raise ValueError(f'{label}: 숫자 시간을 실제 포맷에 맞게 datetime으로 변환해 주세요.')
    try:
        result = series.map(pd.Timestamp)
        if result.isna().any():
            raise ValueError('유효하지 않은 시간 값')
        return result
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f'{label}: 날짜/시간 포맷을 확인해 주세요.') from exc


def get_mcrs_summary(pm_df, mcrs_df):
    """두 조회 결과를 받아 장비·DOI별 PM 이후 누적 현황 DataFrame을 반환."""
    pm = _prepare(pm_df, ['EQP_ID', 'EVENT_TM'], 'PM 이력')
    events = _prepare(mcrs_df, ['EQP_ID', 'DOI_NM', 'HUB_LOAD_TM'], 'MCRS 이력')
    pm['EVENT_TM'] = _timestamps(pm['EVENT_TM'], 'EVENT_TM')
    events['HUB_LOAD_TM'] = _timestamps(events['HUB_LOAD_TM'], 'HUB_LOAD_TM')

    if pm.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    # 입력 PM 이력이 여러 건이어도 장비당 가장 최근 시각만 사용합니다.
    latest_pm = (
        pm.groupby('EQP_ID', as_index=False)['EVENT_TM'].max()
        .rename(columns={'EVENT_TM': 'LAST_PM_TM'})
    )

    if events.empty:
        result = latest_pm.assign(
            DOI_NM=None, MCRS_COUNT=0, FIRST_MCRS_TM=pd.NaT, LAST_MCRS_TM=pd.NaT,
        )
        return result[OUTPUT_COLUMNS].sort_values('EQP_ID').reset_index(drop=True)

    # many_to_one 검사로 PM 조인 때문에 이벤트가 중복 증가하는 것을 방지합니다.
    joined = events.merge(latest_pm, on='EQP_ID', how='inner', validate='many_to_one')
    try:
        after_pm = joined.loc[joined['HUB_LOAD_TM'] > joined['LAST_PM_TM']]
    except TypeError as exc:
        raise ValueError('EVENT_TM과 HUB_LOAD_TM의 타입/시간대를 동일하게 맞춰 주세요.') from exc

    if after_pm.empty:
        result = latest_pm.assign(
            DOI_NM=None, MCRS_COUNT=0, FIRST_MCRS_TM=pd.NaT, LAST_MCRS_TM=pd.NaT,
        )
    else:
        # dropna=False: DOI_NM이 NULL인 이벤트도 사라지지 않고 따로 집계합니다.
        counts = (
            after_pm.groupby(['EQP_ID', 'DOI_NM'], dropna=False, as_index=False)
            .agg(
                MCRS_COUNT=('HUB_LOAD_TM', 'size'),
                FIRST_MCRS_TM=('HUB_LOAD_TM', 'min'),
                LAST_MCRS_TM=('HUB_LOAD_TM', 'max'),
            )
        )
        # PM 이력을 기준으로 LEFT JOIN하여 이벤트 없는 장비도 남깁니다.
        result = latest_pm.merge(counts, on='EQP_ID', how='left', validate='one_to_many')
        result['MCRS_COUNT'] = result['MCRS_COUNT'].fillna(0).astype(int)

    return (
        result[OUTPUT_COLUMNS]
        .sort_values(['MCRS_COUNT', 'EQP_ID', 'DOI_NM'], ascending=[False, True, True], na_position='last')
        .reset_index(drop=True)
    )


# Django 페이지까지 한 Python 파일로 제공하기 위한 내장 템플릿입니다.
PAGE_TEMPLATE = r'''
{% extends "base.html" %}
{% block title %}MCRS 모니터링{% endblock %}
{% block extra_css %}
<style>
.mcrs {padding:32px; color:var(--tx-1,#ececec)}
.mcrs h1 {font-size:25px; margin-bottom:10px}
.mcrs p {color:var(--tx-2,#999); line-height:1.8; margin:12px 0}
.mcrs .toolbar {display:flex; gap:16px; align-items:center; flex-wrap:wrap; margin:24px 0}
.mcrs input,.mcrs button {padding:10px; border:1px solid #444; border-radius:5px; background:#222; color:#eee}
.mcrs .summary {display:flex; gap:24px; flex-wrap:wrap; margin:20px 0}
.mcrs .summary div {padding:18px; border:1px solid #333; border-radius:8px}
.mcrs .summary strong {color:#cc8b3c; font-size:24px; margin-left:12px}
.mcrs .scroll {overflow-x:auto}
.mcrs table {width:100%; border-collapse:collapse; white-space:nowrap}
.mcrs th,.mcrs td {padding:14px; text-align:center; border-bottom:1px solid #333}
.mcrs th {background:#222}
.mcrs tbody tr:nth-child(even) {background:#111}
.mcrs .count {color:#cc8b3c; font-weight:bold}
</style>
{% endblock %}
{% block content %}
<section class="mcrs">
<h1>MCRS 모니터링</h1>
<p>장비별 마지막 PM 이후 · DOI_NM별 누적 건수</p>
<div class="summary">
<div>PM 이력 보유 장비<strong>{{ equipment_count }}대</strong></div>
<div>이벤트 발생 장비<strong>{{ affected_count }}대</strong></div>
<div>전체 DOI 누적<strong>{{ total_count }}건</strong></div>
</div>
<form method="get" class="toolbar">
<label>장비 <input name="eqp" value="{{ eqp }}" placeholder="EQP_ID"></label>
<label>DOI <input name="doi" value="{{ doi }}" placeholder="DOI_NM"></label>
<label><input type="checkbox" name="positive" value="1" {% if positive %}checked{% endif %}> 발생 이력만</label>
<button type="submit">조회</button>
</form>
<div class="scroll"><table>
<thead><tr><th>EQP_ID</th><th>마지막 PM</th><th>DOI_NM</th><th>누적 건수</th><th>최초 이벤트</th><th>최근 이벤트</th></tr></thead>
<tbody>
{% for row in rows %}<tr>
<td>{{ row.EQP_ID }}</td><td>{{ row.LAST_PM_TM }}</td><td>{{ row.DOI_LABEL }}</td>
<td class="count">{{ row.MCRS_COUNT }}</td><td>{{ row.FIRST_MCRS_TM }}</td><td>{{ row.LAST_MCRS_TM }}</td>
</tr>{% empty %}<tr><td colspan="6">조회 조건에 해당하는 결과가 없습니다.</td></tr>{% endfor %}
</tbody></table></div>
<p>HUB_LOAD_TM &gt; 마지막 PM 시각 · 원본 한 행 = 1건 · 상단 합계는 검색 전 전체 결과 기준<br>
PM 이력이 없는 장비는 제외됩니다. 새로운 PM은 다음 조회부터 반영됩니다.</p>
</section>
{% endblock %}
'''


def render_mcrs_page(request, pm_df, mcrs_df):
    """기존 Django view에서 호출: return render_mcrs_page(request, pm_df, mcrs_df)."""
    from django.http import HttpResponse
    from django.template import engines

    summary = get_mcrs_summary(pm_df, mcrs_df)
    eqp = request.GET.get('eqp', '').strip()
    doi = request.GET.get('doi', '').strip()
    positive = request.GET.get('positive') == '1'
    rows = []
    for source in summary.to_dict('records'):
        row = dict(source)
        row['MCRS_COUNT'] = int(row['MCRS_COUNT'])
        row['DOI_LABEL'] = (
            '이벤트 없음' if row['MCRS_COUNT'] == 0 else
            'DOI 미지정' if pd.isna(row['DOI_NM']) else str(row['DOI_NM'])
        )
        if eqp.casefold() not in row['EQP_ID'].casefold():
            continue
        if doi.casefold() not in row['DOI_LABEL'].casefold():
            continue
        if positive and row['MCRS_COUNT'] == 0:
            continue
        for column in ['LAST_PM_TM', 'FIRST_MCRS_TM', 'LAST_MCRS_TM']:
            value = row[column]
            row[column] = '—' if pd.isna(value) else str(value)
        rows.append(row)
    context = {
        'rows': rows, 'eqp': eqp, 'doi': doi, 'positive': positive,
        'equipment_count': summary['EQP_ID'].nunique(),
        'affected_count': summary.loc[summary['MCRS_COUNT'] > 0, 'EQP_ID'].nunique(),
        'total_count': int(summary['MCRS_COUNT'].sum()),
    }
    # Django autoescape를 사용하므로 데이터 안의 HTML은 실행되지 않습니다.
    template = engines['django'].from_string(PAGE_TEMPLATE)
    return HttpResponse(template.render(context, request))


if __name__ == '__main__':
    # 실행 예시용 데이터입니다. 실제 DB를 조회하는 코드는 아닙니다.
    pm_df = pd.DataFrame([
        {'EQP_ID': 'CMP01', 'EVENT_TM': '2026-09-01 10:00:00'},
        {'EQP_ID': 'CMP01', 'EVENT_TM': '2026-09-10 10:00:00'},
        {'EQP_ID': 'CMP02', 'EVENT_TM': '2026-09-12 09:00:00'},
    ])
    mcrs_df = pd.DataFrame([
        {'EQP_ID': 'CMP01', 'DOI_NM': 'DOI_A', 'HUB_LOAD_TM': '2026-09-09 11:00:00'},
        {'EQP_ID': 'CMP01', 'DOI_NM': 'DOI_A', 'HUB_LOAD_TM': '2026-09-10 10:00:00'},
        {'EQP_ID': 'CMP01', 'DOI_NM': 'DOI_A', 'HUB_LOAD_TM': '2026-09-11 11:00:00'},
        {'EQP_ID': 'CMP01', 'DOI_NM': 'DOI_A', 'HUB_LOAD_TM': '2026-09-12 11:00:00'},
        {'EQP_ID': 'CMP01', 'DOI_NM': 'DOI_B', 'HUB_LOAD_TM': '2026-09-13 11:00:00'},
    ])
    print('샘플 결과 — 실제 DB 데이터 아님')
    print(get_mcrs_summary(pm_df, mcrs_df).to_string(index=False))
