"""
equipment/insight_service.py
════════════════════════════════════════════════════════════
분석 엔진 — 이슈 구간이 왜 다른지 찾는다

  LLM 이 호출할 '도구' 들이다. 각 함수는 압축된 근거표를 돌려주고,
  LLM 은 그 표를 읽어 우선순위를 매기고 공정 맥락으로 해석한다.

────────────────────────────────────────────────────────────
설계 원칙

★ 계산은 여기서, 해석은 LLM 이.
  원시 데이터를 LLM 에 넘기지 않는다 — 웨이퍼 수천 장 × 컬럼 수백 개는
  컨텍스트에 안 들어가고, 들어가도 LLM 은 숫자 계산을 틀린다.

★ 다중비교 보정을 반드시 한다.
  파라미터 200개를 훑으면 우연히 유의한 게 10개는 나온다.
  FDR(Benjamini-Hochberg) 보정 없이는 '유의한 변수 15개 발견' 같은
  헛소리가 나온다.

★ 표본 크기를 함께 돌려준다.
  구간이 웨이퍼 20장이면 통계가 못 미덥다. LLM 은 그걸 무시하고
  단정하기 쉬우므로 신뢰도를 출력에 넣어 함께 읽게 한다.

★ 효과크기를 우선한다.
  p값은 표본이 크면 사소한 차이도 유의해진다. 실제로 중요한 것은
  '얼마나 다른가'(Cohen's d)이므로 그걸 기준으로 정렬한다.
════════════════════════════════════════════════════════════
"""

import re
import math
import traceback

import numpy as np
import pandas as pd
from django.db import connections

# 값이 아니라 식별·구분용인 컬럼
META_COLS = {
    'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
    'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
    'WF_ID', 'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY',
    'REWORK_N', 'MEAS_N',
}

# 범주형으로 다룰 컬럼 (장비·챔버 등) — 이름 규칙으로 추가 판정
CAT_HINTS = ('_EQP', '_CH', 'EQP_ID', 'EQP_CH_ID', 'RECIPE_ID',
             'PRE_EQP_ID', 'PRE_EQP_CH', 'PRE_LAYER', 'IDLE')

NUMERIC_TYPES = {
    'smallint', 'integer', 'bigint', 'decimal', 'numeric',
    'real', 'double precision',
}

# 표본이 이보다 적으면 결과를 믿기 어렵다고 표시한다
MIN_SAMPLE = 30
WEAK_SAMPLE = 100


def _conn():
    return connections['analysis_db']


def _table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def _safe(v):
    return bool(v) and bool(re.match(r'^[0-9A-Za-z_\-]+$', str(v)))


# ══════════════════════════════════════════════════════════
# 데이터 적재
# ══════════════════════════════════════════════════════════
def load_frame(oper_id, spans, lot_cd=None, max_cols=400):
    from . import views_analysis2 as v2

    table = _table(oper_id)
    with _conn().cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
        if not cur.fetchone()[0]:
            table_cfg2 = f"{table}_cfg2"
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", [table_cfg2])
            if cur.fetchone()[0]:
                table = table_cfg2
            else:
                raise ValueError(f'{table} 이 없습니다 — 먼저 적재하세요')

        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = %s ORDER BY ordinal_position
        """, [table])
        cols = [(c.upper(), d.lower()) for c, d in cur.fetchall()]
        names = {c for c, _ in cols}

        num = [c for c, d in cols if d in NUMERIC_TYPES and c not in META_COLS
               and not c.endswith(('_OFFSET', '_FORMULA'))][:max_cols]
        cat = [c for c, _ in cols
               if c in names and (c in CAT_HINTS or c.endswith(('_EQP', '_CH')))]
        keep = ['ID', 'DATE'] + \
               [c for c in ('LOT_ID', 'WF_ID', 'LOT_CD') if c in names] + \
               cat + num

        args = []
        span_sql = v2._span_case(spans, names, args)
        where, wargs = '1=1', []
        if lot_cd and 'LOT_CD' in names:
            where, wargs = '"LOT_CD" = %s', [lot_cd]

        sel = ", ".join('id' if c == 'ID' else f'"{c}"' for c in dict.fromkeys(keep))
        cur.execute(f'''
            SELECT {sel}, COALESCE({span_sql}, -1) AS __span
            FROM {table} WHERE {where} ORDER BY "DATE"
        ''', args + wargs)
        rows = cur.fetchall()
        colnames = [d[0].upper() for d in cur.description]

    df = pd.DataFrame(rows, columns=colnames)
    df.rename(columns={'__SPAN': '__span'}, inplace=True)
    for c in num:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df, num, [c for c in cat if c in df.columns]


def _fdr(pvals):
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0: return p
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(n) + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(ranked, 0, 1)
    return out


def _confidence(n_in, n_out):
    small = min(n_in, n_out)
    if small < MIN_SAMPLE:
        return 'low', f'표본이 적습니다 (구간 {n_in}장 / 나머지 {n_out}장)'
    if small < WEAK_SAMPLE:
        return 'medium', f'표본이 다소 적습니다 (구간 {n_in}장)'
    return 'high', ''


def _welch_p_normal(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    nx, ny = len(x), len(y)
    if nx < 2 or ny < 2: return 1.0
    se = np.sqrt(x.var(ddof=1) / nx + y.var(ddof=1) / ny)
    if se == 0: return 1.0
    z = abs(x.mean() - y.mean()) / se
    return float(2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))))


# ══════════════════════════════════════════════════════════
# 도구 1. 구간 비교
# ══════════════════════════════════════════════════════════
def compare_groups(oper_id, spans, lot_cd=None, span_a=0, span_b=None, top=20):
    try:
        from scipy import stats
        _ttest = lambda x, y: stats.ttest_ind(x, y, equal_var=False)[1]
    except ImportError:
        _ttest = _welch_p_normal

    df, num, cat = load_frame(oper_id, spans, lot_cd)
    a = df[df['__span'] == span_a]
    b = df[df['__span'] == span_b] if span_b is not None else df[df['__span'] != span_a]

    if a.empty or b.empty:
        return {'ok': False, 'error': f'비교할 데이터가 없습니다 (A {len(a)}행 / B {len(b)}행)'}

    rows, pvals = [], []
    for c in num:
        x, y = a[c].dropna(), b[c].dropna()
        if len(x) < 3 or len(y) < 3: continue
        mx, my = float(x.mean()), float(y.mean())
        sx, sy = float(x.std(ddof=1)), float(y.std(ddof=1))
        pooled = np.sqrt(((len(x) - 1) * sx**2 + (len(y) - 1) * sy**2) / max(1, len(x) + len(y) - 2))
        d = (mx - my) / pooled if pooled > 0 else 0.0
        try: p = _ttest(x, y)
        except Exception: p = 1.0
        rows.append({'param': c, 'n_a': len(x), 'n_b': len(y),
                     'mean_a': round(mx, 4), 'mean_b': round(my, 4),
                     'std_a': round(sx, 4), 'std_b': round(sy, 4),
                     'diff': round(mx - my, 4), 'cohens_d': round(float(d), 3)})
        pvals.append(float(p) if np.isfinite(p) else 1.0)

    if not rows:
        return {'ok': False, 'error': '비교 가능한 숫자 파라미터가 없습니다'}

    q = _fdr(pvals)
    for r, p, qq in zip(rows, pvals, q):
        r['p'] = round(float(p), 6)
        r['q'] = round(float(qq), 6)
        r['significant'] = bool(qq < 0.05 and abs(r['cohens_d']) >= 0.2)

    rows.sort(key=lambda r: abs(r['cohens_d']), reverse=True)
    conf, note = _confidence(len(a), len(b))

    return {
        'ok': True, 'tool': 'compare_groups',
        'n_a': len(a), 'n_b': len(b), 'n_param': len(rows),
        'n_significant': sum(1 for r in rows if r['significant']),
        'confidence': conf, 'note': note, 'items': rows[:top],
        'method': 'Welch t검정 + BH FDR 보정, 효과크기(Cohen\'s d) 순 정렬',
    }


# ══════════════════════════════════════════════════════════
# 도구 2. 요인 중요도
# ══════════════════════════════════════════════════════════
def feature_importance(oper_id, spans, lot_cd=None, span_a=0, top=20):
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.inspection import permutation_importance
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import roc_auc_score
    except ImportError:
        return _importance_fallback(oper_id, spans, lot_cd, span_a, top)

    df, num, cat = load_frame(oper_id, spans, lot_cd)
    y = (df['__span'] == span_a).astype(int)
    if y.sum() < 10 or (1 - y).sum() < 10:
        return {'ok': False, 'error': f'표본이 부족합니다 (구간 {int(y.sum())}장 / 나머지 {int((1-y).sum())}장 · 각각 10장 이상 필요)'}

    X = df[num].copy()
    for c in cat:
        vc = df[c].astype(str).value_counts()
        for v in vc.index[:8]:
            X[f'{c}={v}'] = (df[c].astype(str) == v).astype(int)

    X = X.fillna(X.median(numeric_only=True)).fillna(0)
    X = X.loc[:, X.std() > 0]
    if X.empty: return {'ok': False, 'error': '변동이 있는 변수가 없습니다'}

    Xtr, Xte, ytr, yte = train_test_split(
        X, y, test_size=0.3, random_state=0,
        stratify=y if min(y.sum(), (1 - y).sum()) >= 2 else None)

    m = RandomForestClassifier(n_estimators=200, max_depth=8, min_samples_leaf=5, 
                               random_state=0, class_weight='balanced', n_jobs=-1)
    m.fit(Xtr, ytr)

    try: auc = float(roc_auc_score(yte, m.predict_proba(Xte)[:, 1]))
    except Exception: auc = float('nan')

    imp = permutation_importance(m, Xte, yte, n_repeats=10, random_state=0, n_jobs=-1)
    items = sorted(
        [{'feature': c, 'importance': round(float(v), 5), 'std': round(float(s), 5)}
         for c, v, s in zip(X.columns, imp.importances_mean, imp.importances_std) if v > 0],
        key=lambda r: r['importance'], reverse=True)[:top]

    conf, note = _confidence(int(y.sum()), int((1 - y).sum()))
    if not np.isnan(auc) and auc < 0.6:
        conf = 'low'
        note = (note + ' · ' if note else '') + f'모델 구분력 저조 (AUC {auc:.2f})'

    return {
        'ok': True, 'tool': 'feature_importance',
        'n_in': int(y.sum()), 'n_out': int((1 - y).sum()),
        'auc': None if np.isnan(auc) else round(auc, 3),
        'confidence': conf, 'note': note, 'items': items,
        'method': 'RandomForest + permutation importance',
    }

def _importance_fallback(oper_id, spans, lot_cd=None, span_a=0, top=20):
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    y = (df['__span'] == span_a).astype(int).to_numpy()
    if y.sum() < 10 or (1 - y).sum() < 10:
        return {'ok': False, 'error': '표본이 부족합니다.'}

    def auc_of(v):
        m = ~pd.isna(v)
        if m.sum() < 20: return None
        vv, yy = np.asarray(v)[m], y[m]
        n1, n0 = yy.sum(), (1 - yy).sum()
        if n1 < 5 or n0 < 5: return None
        r = pd.Series(vv).rank().to_numpy()
        a = (r[yy == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
        return float(a)

    items = []
    for c in num:
        a = auc_of(df[c].to_numpy())
        if a is None: continue
        items.append({'feature': c, 'importance': round(abs(a - 0.5) * 2, 4), 'auc': round(a, 3)})

    for c in cat:
        g = pd.DataFrame({'v': df[c].astype(str), 'y': y})
        rate = g.groupby('v')['y'].agg(['mean', 'count'])
        rate = rate[rate['count'] >= MIN_SAMPLE]
        if len(rate) < 2: continue
        items.append({'feature': c, 'importance': round(float(rate['mean'].max() - rate['mean'].min()), 4), 'levels': int(len(rate))})

    if not items: return {'ok': False, 'error': '평가 가능한 변수가 없습니다'}

    items.sort(key=lambda r: r['importance'], reverse=True)
    conf, note = _confidence(int(y.sum()), int((1 - y).sum()))
    return {
        'ok': True, 'tool': 'feature_importance',
        'n_in': int(y.sum()), 'n_out': int((1 - y).sum()),
        'auc': None, 'confidence': conf, 'note': note, 'items': items[:top],
        'method': '변수별 단변량 AUC',
    }


# ══════════════════════════════════════════════════════════
# 도구 2-B. 소규모 이상 자재 분석 (Anomaly Features) ★ 신규 추가
# ══════════════════════════════════════════════════════════
def anomaly_features(oper_id, spans, lot_cd=None, span_a=0, top=15):
    """
    극소수(예: 30일치 중 단 몇 장)의 불량 웨이퍼(Anomaly)를 정상군과 비교하여
    장비/챔버 쏠림 현상(Lift)과 특정 파라미터의 편차(Robust Z-Score)를 추출합니다.
    """
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    a = df[df['__span'] == span_a]  # 타겟(불량) 구간
    b = df[df['__span'] != span_a]  # 나머지(정상) 구간

    if len(a) == 0 or len(b) == 0:
        return {'ok': False, 'error': f'비교할 데이터가 부족합니다 (구간 {len(a)}장 / 나머지 {len(b)}장)'}

    items = []
    
    # 1. 수치형 데이터: MAD 기반 Robust Z-Score (극소수 이상 탐지에 유리)
    for c in num:
        b_med = float(b[c].median())
        b_mad = float((b[c] - b_med).abs().median())
        
        # MAD가 0이면 일반 표준편차 사용 (이마저도 0이면 패스)
        if b_mad == 0:
            b_mad = float(b[c].std(ddof=1))
            if pd.isna(b_mad) or b_mad == 0:
                continue
                
        a_med = float(a[c].median())
        z_score = abs(a_med - b_med) / b_mad
        
        # 정상군 대비 뚜렷하게 튀는 값(Z-score 3 이상)만 수집
        if z_score > 3.0:
            items.append({
                'feature': c, 
                'type': 'numeric',
                'score': round(z_score, 2),
                'detail': f"구간 중앙값 {a_med:.3f} vs 나머지 {b_med:.3f} (Z: {z_score:.1f})"
            })

    # 2. 범주형 데이터 (장비, 챔버 등): Lift(편중도) 계산
    for c in cat:
        for val in a[c].dropna().unique():
            a_pct = (a[c] == val).mean()
            b_pct = (b[c] == val).mean()
            
            if b_pct > 0:
                lift = a_pct / b_pct
                if lift > 2.0 and (a[c] == val).sum() >= 2:
                    items.append({
                        'feature': f"{c} = {val}", 
                        'type': 'category',
                        'score': round(lift, 2),
                        'detail': f"구간 내 {a_pct*100:.1f}% 점유 vs 나머지 {b_pct*100:.1f}% (Lift: {lift:.1f}배)"
                    })

    items.sort(key=lambda x: x['score'], reverse=True)
    
    conf, note = _confidence(len(a), len(b))
    if len(a) < 10:
        conf = 'mid'
        note = f'소규모 타겟 분석 모드 작동 (구간 {len(a)}장)'

    return {
        'ok': True, 'tool': 'anomaly_features',
        'n_in': len(a), 'n_out': len(b),
        'confidence': conf, 'note': note,
        'items': items[:top],
        'method': 'Robust Z-score(수치형) 및 Lift(범주형) 기반 극소수 이상 자재 탐지',
    }


# ══════════════════════════════════════════════════════════
# 도구 3. 교호작용
# ══════════════════════════════════════════════════════════
def interaction(oper_id, spans, lot_cd=None, span_a=0, target=None, top=12):
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    if not cat: return {'ok': False, 'error': '장비·챔버 같은 범주형 컬럼이 없습니다'}

    if target and target.upper() in df.columns:
        yv = pd.to_numeric(df[target.upper()], errors='coerce')
        ylabel = target.upper()
    else:
        yv = (df['__span'] == span_a).astype(float)
        ylabel = '구간 여부'

    rows = []
    for cc in cat:
        groups = df[cc].astype(str)
        vc = groups.value_counts()
        levels = [v for v in vc.index[:6] if vc[v] >= MIN_SAMPLE]
        if len(levels) < 2: continue

        for nc in num:
            xv = pd.to_numeric(df[nc], errors='coerce')
            slopes = []
            for lv in levels:
                m = (groups == lv) & xv.notna() & yv.notna()
                if m.sum() < MIN_SAMPLE: continue
                x, y = xv[m].to_numpy(), yv[m].to_numpy()
                if np.std(x) == 0: continue
                b = float(np.polyfit(x, y, 1)[0])
                slopes.append({'level': lv, 'n': int(m.sum()), 'slope': round(b, 6)})
            if len(slopes) < 2: continue

            vals = [s['slope'] for s in slopes]
            spread = max(vals) - min(vals)
            sx = float(np.nanstd(xv)) or 1e-12
            sy = float(np.nanstd(yv)) or 1e-12
            std_spread = spread * sx / sy      
            flip = (max(vals) > 0 > min(vals))
            score = std_spread * (1.5 if flip else 1.0)

            rows.append({
                'category': cc, 'param': nc, 'spread': round(spread, 6),
                'std_spread': round(float(std_spread), 4), 'sign_flip': bool(flip),
                'levels': sorted(slopes, key=lambda s: s['slope']), 'score': round(float(score), 4),
            })

    rows = [r for r in rows if r['std_spread'] >= 0.1]
    rows.sort(key=lambda r: r['score'], reverse=True)
    n_in = int((df['__span'] == span_a).sum())
    conf, note = _confidence(n_in, len(df) - n_in)

    return {
        'ok': True, 'tool': 'interaction', 'target': ylabel, 'n_pair': len(rows),
        'confidence': conf, 'note': note, 'items': rows[:top],
        'method': '범주 수준별 회귀 기울기 비교',
    }


# ══════════════════════════════════════════════════════════
# 도구 4. 범주별 분포
# ══════════════════════════════════════════════════════════
def distribution(oper_id, spans, param, lot_cd=None, by=None, span_a=0):
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    p = str(param).upper()
    if p not in df.columns: return {'ok': False, 'error': f'{p} 컬럼이 없습니다'}

    keys = [by.upper()] if by and by.upper() in df.columns else cat
    if not keys: return {'ok': False, 'error': '구분할 범주형 컬럼이 없습니다'}

    out = []
    for k in keys[:3]:
        grp = []
        for lv, g in df.groupby(df[k].astype(str)):
            v = pd.to_numeric(g[p], errors='coerce').dropna()
            if len(v) < 5: continue
            inn = g['__span'] == span_a
            grp.append({
                'level': lv, 'n': len(v), 'mean': round(float(v.mean()), 4),
                'std': round(float(v.std(ddof=1)), 4) if len(v) > 1 else 0.0,
                'median': round(float(v.median()), 4),
                'n_in_span': int(inn.sum()), 'span_ratio': round(float(inn.mean()), 3),
            })
        if grp:
            grp.sort(key=lambda r: r['mean'], reverse=True)
            out.append({'by': k, 'levels': grp})

    return {'ok': True, 'tool': 'distribution', 'param': p, 'groups': out, 'method': '범주별 평균·표준편차'}


# ══════════════════════════════════════════════════════════
# 도구 5. 시계열 변화
# ══════════════════════════════════════════════════════════
def timeline(oper_id, spans, param, lot_cd=None, bins=40):
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    p = str(param).upper()
    if p not in df.columns: return {'ok': False, 'error': f'{p} 컬럼이 없습니다'}

    d = df[['DATE', p, '__span']].dropna(subset=[p]).copy()
    if d.empty: return {'ok': False, 'error': f'{p} 에 값이 없습니다'}

    d['DATE'] = pd.to_datetime(d['DATE'], errors='coerce')
    d = d.dropna(subset=['DATE']).sort_values('DATE')
    if len(d) < 4: return {'ok': False, 'error': '점이 너무 적습니다'}

    d['__bin'] = pd.cut(d['DATE'].astype('int64'), bins=min(bins, len(d)), labels=False)
    out = []
    for b, g in d.groupby('__bin'):
        v = g[p]
        out.append({
            'from': str(g['DATE'].min())[:19], 'to': str(g['DATE'].max())[:19],
            'n': len(v), 'mean': round(float(v.mean()), 4),
            'std': round(float(v.std(ddof=1)), 4) if len(v) > 1 else 0.0,
            'in_span': int((g['__span'] >= 0).sum()),
        })

    return {'ok': True, 'tool': 'timeline', 'param': p, 'n': len(d), 'bins': out, 'method': '시간 추이 요약'}


TOOLS = {
    'compare_groups': compare_groups,
    'feature_importance': feature_importance,
    'anomaly_features': anomaly_features,
    'interaction': interaction,
    'distribution': distribution,
    'timeline': timeline,
}

def tool_specs():
    common = {
        'oper_id': {'type': 'string', 'description': '데이터 소스 키'},
        'lot_cd': {'type': 'string', 'description': 'LOT_CD 로 좁힐 때'},
        'span_a': {'type': 'integer', 'description': '분석할 이슈 구간 번호 (0부터)'},
    }
    return [
        {'type': 'function', 'function': {
            'name': 'compare_groups',
            'description': '이슈 구간과 나머지(또는 다른 구간)를 전 파라미터에 대해 비교한다. 효과크기 순으로 후보를 좁힐 때.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'span_b': {'type': 'integer'}, 'top': {'type': 'integer'}
            }, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'feature_importance',
            'description': '이슈 구간이 전반적으로 넓게 퍼져있을 때, 구간 여부를 맞히는 트리 모델을 세워 변수 중요도를 매긴다.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'top': {'type': 'integer'}}, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'anomaly_features',
            'description': '이슈 구간이 극소수일 때(예: 간헐적 불량 5장), 정상군과 비교하여 특정 설비 쏠림(Lift)과 파라미터 편차(Robust Z-Score)를 추출한다.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'top': {'type': 'integer'}}, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'interaction',
            'description': '장비·챔버 같은 범주형과 연속 파라미터의 교호작용을 찾는다. 범주마다 기울기가 다른지 본다.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'target': {'type': 'string'}, 'top': {'type': 'integer'}}, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'distribution',
            'description': '특정 파라미터를 장비·챔버별로 쪼개 평균·산포를 본다.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'param': {'type': 'string'}, 'by': {'type': 'string'}}, 'required': ['oper_id', 'param']}}},
        {'type': 'function', 'function': {
            'name': 'timeline',
            'description': '파라미터의 시간 변화를 요약한다.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'param': {'type': 'string'}}, 'required': ['oper_id', 'param']}}},
    ]

def run_tool(name, args, spans):
    fn = TOOLS.get(name)
    if not fn: return {'ok': False, 'error': f'모르는 도구입니다: {name}'}
    kw = dict(args or {})
    oper_id = kw.pop('oper_id', None)
    if not _safe(oper_id): return {'ok': False, 'error': 'oper_id 형식 오류'}
    for k in ('param', 'by', 'target', 'lot_cd'):
        if k in kw and kw[k] is not None and not _safe(str(kw[k])):
            return {'ok': False, 'error': f'{k} 형식 오류'}
    try: return fn(oper_id, spans, **kw)
    except Exception as e:
        traceback.print_exc()
        return {'ok': False, 'error': f'{e.__class__.__name__}: {e}'}
