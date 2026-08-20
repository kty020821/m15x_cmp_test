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
    """
    분석용 DataFrame 을 만든다.

    각 행에 __span 열이 붙는다 — 0,1,2… 는 이슈 구간, -1 은 나머지.
    (views_analysis2 의 구간 판정과 같은 규칙을 쓴다)
    """
    from . import views_analysis2 as v2

    table = _table(oper_id)
    with _conn().cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
        if not cur.fetchone()[0]:
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

        sel = ", ".join(f'"{c}"' for c in dict.fromkeys(keep))
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
    """
    Benjamini-Hochberg FDR 보정.
    ★ 파라미터를 수백 개 훑으면 우연히 유의한 게 반드시 나온다.
      보정 없이 'p<0.05 인 것 20개' 를 보고하면 대부분 거짓이다.
    """
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(n) + 1)
    # 단조 감소 보정
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(ranked, 0, 1)
    return out


def _confidence(n_in, n_out):
    """표본 크기로 신뢰도를 매긴다 — LLM 이 단정하지 않도록"""
    small = min(n_in, n_out)
    if small < MIN_SAMPLE:
        return 'low', f'표본이 적습니다 (구간 {n_in}장 / 나머지 {n_out}장)'
    if small < WEAK_SAMPLE:
        return 'medium', f'표본이 다소 적습니다 (구간 {n_in}장)'
    return 'high', ''


def _welch_p_normal(x, y):
    """
    scipy 없이 Welch 검정의 p 값 — 정규근사.

    ★ 표본이 30 이상이면 t분포와 정규분포가 거의 같아
      실용적으로 충분하다. 표본이 아주 작으면 p 값이 조금 낙관적이지만,
      그런 경우는 신뢰도(_confidence)가 이미 'low' 로 표시된다.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    nx, ny = len(x), len(y)
    if nx < 2 or ny < 2:
        return 1.0
    se = np.sqrt(x.var(ddof=1) / nx + y.var(ddof=1) / ny)
    if se == 0:
        return 1.0
    z = abs(x.mean() - y.mean()) / se
    # 양측 p = 2 * (1 - Φ(z)) — 오차함수로 계산
    return float(2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))))


# ══════════════════════════════════════════════════════════
# 도구 1. 구간 비교
# ══════════════════════════════════════════════════════════
def compare_groups(oper_id, spans, lot_cd=None, span_a=0, span_b=None,
                   top=20):
    """
    구간 A 와 B(없으면 나머지 전체)를 전 파라미터에 대해 비교한다.

    반환: 효과크기 순 상위 목록 + 표본 정보
      diff        평균 차이
      cohens_d    효과크기 (0.2 작음 / 0.5 중간 / 0.8 큼)
      p, q        t검정 p값과 FDR 보정값
    """
    # ★ scipy 가 없어도 이 도구는 돌아야 한다 — 가장 기본이 되는 분석이다.
    #   없으면 정규근사로 p 값을 구한다(표본이 크면 t검정과 거의 같다).
    try:
        from scipy import stats
        _ttest = lambda x, y: stats.ttest_ind(x, y, equal_var=False)[1]
    except ImportError:
        _ttest = _welch_p_normal

    df, num, cat = load_frame(oper_id, spans, lot_cd)
    a = df[df['__span'] == span_a]
    b = df[df['__span'] == span_b] if span_b is not None else df[df['__span'] != span_a]

    if a.empty or b.empty:
        return {'ok': False,
                'error': f'비교할 데이터가 없습니다 (A {len(a)}행 / B {len(b)}행)'}

    rows, pvals = [], []
    for c in num:
        x, y = a[c].dropna(), b[c].dropna()
        if len(x) < 3 or len(y) < 3:
            continue
        mx, my = float(x.mean()), float(y.mean())
        sx, sy = float(x.std(ddof=1)), float(y.std(ddof=1))
        # 합동 표준편차 (Cohen's d)
        pooled = np.sqrt(((len(x) - 1) * sx**2 + (len(y) - 1) * sy**2) /
                         max(1, len(x) + len(y) - 2))
        d = (mx - my) / pooled if pooled > 0 else 0.0
        try:
            p = _ttest(x, y)
        except Exception:
            p = 1.0
        rows.append({'param': c, 'n_a': len(x), 'n_b': len(y),
                     'mean_a': round(mx, 4), 'mean_b': round(my, 4),
                     'std_a': round(sx, 4), 'std_b': round(sy, 4),
                     'diff': round(mx - my, 4),
                     'cohens_d': round(float(d), 3)})
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
        'n_a': len(a), 'n_b': len(b),
        'n_param': len(rows),
        'n_significant': sum(1 for r in rows if r['significant']),
        'confidence': conf, 'note': note,
        'items': rows[:top],
        'method': 'Welch t검정 + BH FDR 보정, 효과크기(Cohen\'s d) 순 정렬',
    }


# ══════════════════════════════════════════════════════════
# 도구 2. 요인 중요도
# ══════════════════════════════════════════════════════════
def feature_importance(oper_id, spans, lot_cd=None, span_a=0, top=20):
    """
    '이슈 구간인가' 를 맞히는 모델을 세워 변수 중요도를 본다.

    ★ 단변량 비교(도구 1)는 변수를 하나씩만 본다. 트리 모델은
      변수들이 함께 작용하는 경우를 잡아낸다.
    ★ permutation importance 를 쓴다 — 트리의 기본 중요도는
      값 종류가 많은 변수를 과대평가한다.
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.inspection import permutation_importance
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import roc_auc_score
    except ImportError:
        # ★ scikit-learn 이 없는 서버가 있다. 그렇다고 이 도구가
        #   통째로 죽으면 안 되므로 단변량 대체 방식으로 답한다.
        #   변수 간 상호작용은 못 잡지만 순위는 낼 수 있다.
        return _importance_fallback(oper_id, spans, lot_cd, span_a, top)

    df, num, cat = load_frame(oper_id, spans, lot_cd)
    y = (df['__span'] == span_a).astype(int)
    if y.sum() < 10 or (1 - y).sum() < 10:
        return {'ok': False,
                'error': f'표본이 부족합니다 (구간 {int(y.sum())}장 / '
                         f'나머지 {int((1-y).sum())}장 · 각각 10장 이상 필요)'}

    # 범주형은 원-핫으로 (값 종류가 많으면 상위만)
    X = df[num].copy()
    for c in cat:
        vc = df[c].astype(str).value_counts()
        for v in vc.index[:8]:
            X[f'{c}={v}'] = (df[c].astype(str) == v).astype(int)

    # 결측은 중앙값으로 — 트리는 결측을 못 받는다
    X = X.fillna(X.median(numeric_only=True)).fillna(0)
    X = X.loc[:, X.std() > 0]           # 값이 하나뿐인 열은 제외
    if X.empty:
        return {'ok': False, 'error': '변동이 있는 변수가 없습니다'}

    Xtr, Xte, ytr, yte = train_test_split(
        X, y, test_size=0.3, random_state=0,
        stratify=y if min(y.sum(), (1 - y).sum()) >= 2 else None)

    m = RandomForestClassifier(n_estimators=200, max_depth=8,
                               min_samples_leaf=5, random_state=0,
                               class_weight='balanced', n_jobs=-1)
    m.fit(Xtr, ytr)

    try:
        auc = float(roc_auc_score(yte, m.predict_proba(Xte)[:, 1]))
    except Exception:
        auc = float('nan')

    imp = permutation_importance(m, Xte, yte, n_repeats=10,
                                 random_state=0, n_jobs=-1)
    items = sorted(
        [{'feature': c, 'importance': round(float(v), 5),
          'std': round(float(s), 5)}
         for c, v, s in zip(X.columns, imp.importances_mean,
                            imp.importances_std) if v > 0],
        key=lambda r: r['importance'], reverse=True)[:top]

    conf, note = _confidence(int(y.sum()), int((1 - y).sum()))
    # 모델이 못 맞히면 중요도도 의미가 없다
    if not np.isnan(auc) and auc < 0.6:
        conf = 'low'
        note = (note + ' · ' if note else '') + \
               f'모델이 구간을 잘 구분하지 못합니다 (AUC {auc:.2f}) — ' \
               f'요인 순위를 신뢰하기 어렵습니다'

    return {
        'ok': True, 'tool': 'feature_importance',
        'n_in': int(y.sum()), 'n_out': int((1 - y).sum()),
        'auc': None if np.isnan(auc) else round(auc, 3),
        'confidence': conf, 'note': note,
        'items': items,
        'method': 'RandomForest + permutation importance (검증셋 30%)',
    }


def _importance_fallback(oper_id, spans, lot_cd=None, span_a=0, top=20):
    """
    scikit-learn 이 없을 때의 요인 순위.

    각 변수가 '구간 여부' 를 얼마나 가르는지를 AUC 로 잰다.
    한 변수만 쓰는 분류기의 성능과 같아서, 트리 모델의 중요도와
    비슷한 순위를 준다.

    ★ 한계: 변수를 하나씩만 보므로 '둘이 함께일 때만 문제' 인 경우를
      못 잡는다. 그 사실을 note 에 적어 LLM 이 오해하지 않게 한다.
    """
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    y = (df['__span'] == span_a).astype(int).to_numpy()
    if y.sum() < 10 or (1 - y).sum() < 10:
        return {'ok': False,
                'error': f'표본이 부족합니다 (구간 {int(y.sum())}장 / '
                         f'나머지 {int((1 - y).sum())}장)'}

    def auc_of(v):
        """순위 기반 AUC — 정규분포를 가정하지 않는다"""
        m = ~pd.isna(v)
        if m.sum() < 20:
            return None
        vv, yy = np.asarray(v)[m], y[m]
        n1, n0 = yy.sum(), (1 - yy).sum()
        if n1 < 5 or n0 < 5:
            return None
        r = pd.Series(vv).rank().to_numpy()
        a = (r[yy == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
        return float(a)

    items = []
    for c in num:
        a = auc_of(df[c].to_numpy())
        if a is None:
            continue
        # 0.5 에서 얼마나 벗어났나 — 방향은 무관
        items.append({'feature': c, 'importance': round(abs(a - 0.5) * 2, 4),
                      'auc': round(a, 3)})

    # 범주형은 수준별 구간 비율의 퍼짐으로 본다
    for c in cat:
        g = pd.DataFrame({'v': df[c].astype(str), 'y': y})
        rate = g.groupby('v')['y'].agg(['mean', 'count'])
        rate = rate[rate['count'] >= MIN_SAMPLE]
        if len(rate) < 2:
            continue
        items.append({'feature': c,
                      'importance': round(float(rate['mean'].max()
                                                - rate['mean'].min()), 4),
                      'levels': int(len(rate))})

    if not items:
        return {'ok': False, 'error': '평가 가능한 변수가 없습니다'}

    items.sort(key=lambda r: r['importance'], reverse=True)
    conf, note = _confidence(int(y.sum()), int((1 - y).sum()))
    note = ((note + ' · ') if note else '') + \
           ('scikit-learn 이 없어 단변량 방식으로 계산했습니다 — '
            '변수 하나씩만 보므로 여러 변수가 함께 작용하는 경우는 '
            '잡지 못합니다')

    return {
        'ok': True, 'tool': 'feature_importance',
        'n_in': int(y.sum()), 'n_out': int((1 - y).sum()),
        'auc': None, 'confidence': conf, 'note': note,
        'items': items[:top],
        'method': '변수별 단변량 AUC (sklearn 미설치 대체)',
    }


# ══════════════════════════════════════════════════════════
# 도구 3. 교호작용
# ══════════════════════════════════════════════════════════
def interaction(oper_id, spans, lot_cd=None, span_a=0, target=None, top=12):
    """
    범주형(장비·챔버) × 연속형 조합에서 기울기가 달라지는지 본다.

    ★ CMP 에서 실제로 의미 있는 교호작용은 대체로 이 형태다 —
      '장비마다 압력의 영향이 다르다' 같은 것.
    ★ 연속×연속은 조합이 폭발하므로 여기서 다루지 않는다.
      필요하면 요인 중요도 상위끼리 따로 본다.

    target 을 주면 그 값을 종속변수로, 없으면 '구간 여부' 를 쓴다.
    """
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    if not cat:
        return {'ok': False,
                'error': '장비·챔버 같은 범주형 컬럼이 없어 교호작용을 볼 수 없습니다'}

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
        if len(levels) < 2:
            continue

        for nc in num:
            xv = pd.to_numeric(df[nc], errors='coerce')
            slopes = []
            for lv in levels:
                m = (groups == lv) & xv.notna() & yv.notna()
                if m.sum() < MIN_SAMPLE:
                    continue
                x, y = xv[m].to_numpy(), yv[m].to_numpy()
                if np.std(x) == 0:
                    continue
                # 단순 회귀 기울기
                b = float(np.polyfit(x, y, 1)[0])
                slopes.append({'level': lv, 'n': int(m.sum()),
                               'slope': round(b, 6)})
            if len(slopes) < 2:
                continue

            vals = [s['slope'] for s in slopes]
            spread = max(vals) - min(vals)

            # ★ 기울기 차이를 '표준화' 한다.
            #   비율(차이/최대기울기)만 쓰면 기울기가 0에 가까울 때 비율이
            #   폭발해 잡음이 상위로 올라온다. 실제로 그 현상을 확인했다.
            #   y 의 산포 대비 x 한 단위가 y 를 얼마나 움직이는지로 바꾼다.
            sx = float(np.nanstd(xv)) or 1e-12
            sy = float(np.nanstd(yv)) or 1e-12
            std_spread = spread * sx / sy      # 무차원 — 변수 간 비교 가능

            flip = (max(vals) > 0 > min(vals))
            # 부호가 갈려도 크기가 미미하면 의미 없다 — 표준화 값에만 가중
            score = std_spread * (1.5 if flip else 1.0)

            rows.append({
                'category': cc, 'param': nc,
                'spread': round(spread, 6),
                'std_spread': round(float(std_spread), 4),
                'sign_flip': bool(flip),
                'levels': sorted(slopes, key=lambda s: s['slope']),
                'score': round(float(score), 4),
            })

    # 표준화 기울기 차이가 0.1 미만이면 실질적 의미가 없다
    rows = [r for r in rows if r['std_spread'] >= 0.1]
    rows.sort(key=lambda r: r['score'], reverse=True)
    n_in = int((df['__span'] == span_a).sum())
    conf, note = _confidence(n_in, len(df) - n_in)

    return {
        'ok': True, 'tool': 'interaction',
        'target': ylabel, 'n_pair': len(rows),
        'confidence': conf, 'note': note,
        'items': rows[:top],
        'method': '범주 수준별 회귀 기울기 비교 — 기울기 차이를 x·y 산포로 '
                  '표준화(무차원), 부호가 갈리면 1.5배 가중, 0.1 미만 제외',
    }


# ══════════════════════════════════════════════════════════
# 도구 4. 범주별 분포
# ══════════════════════════════════════════════════════════
def distribution(oper_id, spans, param, lot_cd=None, by=None, span_a=0):
    """특정 파라미터를 장비·챔버별로 쪼개 본다"""
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    p = str(param).upper()
    if p not in df.columns:
        return {'ok': False, 'error': f'{p} 컬럼이 없습니다'}

    keys = [by.upper()] if by and by.upper() in df.columns else cat
    if not keys:
        return {'ok': False, 'error': '구분할 범주형 컬럼이 없습니다'}

    out = []
    for k in keys[:3]:
        grp = []
        for lv, g in df.groupby(df[k].astype(str)):
            v = pd.to_numeric(g[p], errors='coerce').dropna()
            if len(v) < 5:
                continue
            inn = g['__span'] == span_a
            grp.append({
                'level': lv, 'n': len(v),
                'mean': round(float(v.mean()), 4),
                'std': round(float(v.std(ddof=1)), 4) if len(v) > 1 else 0.0,
                'median': round(float(v.median()), 4),
                'n_in_span': int(inn.sum()),
                'span_ratio': round(float(inn.mean()), 3),
            })
        if grp:
            grp.sort(key=lambda r: r['mean'], reverse=True)
            out.append({'by': k, 'levels': grp})

    return {'ok': True, 'tool': 'distribution', 'param': p, 'groups': out,
            'method': '범주별 평균·표준편차와 구간 포함 비율'}


# ══════════════════════════════════════════════════════════
# 도구 5. 시계열 변화
# ══════════════════════════════════════════════════════════
def timeline(oper_id, spans, param, lot_cd=None, bins=40):
    """파라미터가 시간에 따라 어떻게 변했는지 — 구간이 어디에 있는지"""
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    p = str(param).upper()
    if p not in df.columns:
        return {'ok': False, 'error': f'{p} 컬럼이 없습니다'}

    d = df[['DATE', p, '__span']].dropna(subset=[p]).copy()
    if d.empty:
        return {'ok': False, 'error': f'{p} 에 값이 없습니다'}

    d['DATE'] = pd.to_datetime(d['DATE'], errors='coerce')
    d = d.dropna(subset=['DATE']).sort_values('DATE')
    if len(d) < 4:
        return {'ok': False, 'error': '점이 너무 적습니다'}

    # 시간을 균등 구간으로 나눠 요약 — 원시 점을 다 넘기지 않는다
    d['__bin'] = pd.cut(d['DATE'].astype('int64'), bins=min(bins, len(d)),
                        labels=False)
    out = []
    for b, g in d.groupby('__bin'):
        v = g[p]
        out.append({
            'from': str(g['DATE'].min())[:19], 'to': str(g['DATE'].max())[:19],
            'n': len(v), 'mean': round(float(v.mean()), 4),
            'std': round(float(v.std(ddof=1)), 4) if len(v) > 1 else 0.0,
            'in_span': int((g['__span'] >= 0).sum()),
        })

    return {'ok': True, 'tool': 'timeline', 'param': p,
            'n': len(d), 'bins': out,
            'method': f'시간을 {len(out)}구간으로 나눈 요약'}


# ══════════════════════════════════════════════════════════
# 도구 목록 (LLM function calling 스펙)
# ══════════════════════════════════════════════════════════
TOOLS = {
    'compare_groups': compare_groups,
    'feature_importance': feature_importance,
    'interaction': interaction,
    'distribution': distribution,
    'timeline': timeline,
}


def tool_specs():
    """OpenAI function calling 형식의 도구 정의"""
    common = {
        'oper_id': {'type': 'string', 'description': '데이터 소스 키'},
        'lot_cd': {'type': 'string', 'description': 'LOT_CD 로 좁힐 때'},
        'span_a': {'type': 'integer',
                   'description': '분석할 이슈 구간 번호 (0부터)'},
    }
    return [
        {'type': 'function', 'function': {
            'name': 'compare_groups',
            'description': '이슈 구간과 나머지(또는 다른 구간)를 전 파라미터에 '
                           '대해 비교한다. 효과크기 순으로 후보를 좁힐 때 먼저 쓴다.',
            'parameters': {'type': 'object', 'properties': {
                **common,
                'span_b': {'type': 'integer',
                           'description': '비교 대상 구간. 생략하면 나머지 전체'},
                'top': {'type': 'integer', 'description': '상위 몇 개 (기본 20)'},
            }, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'feature_importance',
            'description': '구간 여부를 맞히는 모델을 세워 변수 중요도를 매긴다. '
                           '변수들이 함께 작용하는 경우를 잡는다.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'top': {'type': 'integer'}}, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'interaction',
            'description': '장비·챔버 같은 범주형과 연속 파라미터의 교호작용을 '
                           '찾는다. 범주마다 기울기가 다른지 본다.',
            'parameters': {'type': 'object', 'properties': {
                **common,
                'target': {'type': 'string',
                           'description': '종속변수. 생략하면 구간 여부'},
                'top': {'type': 'integer'}}, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'distribution',
            'description': '특정 파라미터를 장비·챔버별로 쪼개 평균·산포를 본다.',
            'parameters': {'type': 'object', 'properties': {
                **common,
                'param': {'type': 'string', 'description': '볼 파라미터'},
                'by': {'type': 'string', 'description': '구분 기준 컬럼'}},
                'required': ['oper_id', 'param']}}},
        {'type': 'function', 'function': {
            'name': 'timeline',
            'description': '파라미터의 시간 변화를 요약한다. 언제부터 달라졌는지 볼 때.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'param': {'type': 'string'}},
                'required': ['oper_id', 'param']}}},
    ]


def run_tool(name, args, spans):
    """
    도구 실행. spans 는 화면이 정의한 이슈 구간이라 LLM 이 못 바꾼다.

    ★ LLM 이 넘긴 인자를 그대로 믿지 않는다 — 컬럼 이름이나 공정 ID 를
      지어내는 경우가 있어 형식을 검사한다.
    """
    fn = TOOLS.get(name)
    if not fn:
        return {'ok': False, 'error': f'모르는 도구입니다: {name}'}

    kw = dict(args or {})
    oper_id = kw.pop('oper_id', None)
    if not _safe(oper_id):
        return {'ok': False, 'error': 'oper_id 형식 오류'}
    for k in ('param', 'by', 'target', 'lot_cd'):
        if k in kw and kw[k] is not None and not _safe(str(kw[k])):
            return {'ok': False, 'error': f'{k} 형식 오류'}

    try:
        return fn(oper_id, spans, **kw)
    except TypeError as e:
        return {'ok': False, 'error': f'인자 오류: {e}'}
    except Exception as e:
        traceback.print_exc()
        return {'ok': False, 'error': f'{e.__class__.__name__}: {e}'}
