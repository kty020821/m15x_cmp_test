# ============================================================
# import
# ============================================================
from pathlib import Path
import re
import requests
import io
import base64
import glob
import platform
import warnings
import os
import gc
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import dotenv
dotenv.load_dotenv()
import json

# 큐봇 발송: inline_notify.py 만 사용 (notify.py, notify_cmp.py 전혀 사용 안 함)
import inline_notify


# ============================================================
# 설정
# ============================================================
DAYS            = 15
YESTERDAY       = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
MEAN_DIFF_SIGMA = 2.0
BIAS_RATIO      = 0.7

try:
    from config import QBOT_USER, QBOT_CHANNEL
except ImportError:
    QBOT_USER     = os.environ.get("QBOT_USER", "")
    QBOT_CHANNEL  = "500022121"

QBOT_USER     = os.environ.get("QBOT_USER", "")
QBOT_CHANNEL  = "500022121"
# ============================================================
# 공정별 즉시 발송 헬퍼 (메모리 최소화)
# ============================================================

def _send_oper_alert(oper_desc, alerts_df):
    """공정별 이상 즉시 큐봇 발송. 전체 요약 안 함."""
    if alerts_df.empty:
        return

    MAX_ROWS = 10
    COLS = ["LOT_TYPE", "OPER_DET_DESC", "PARAM_NM", "ALERT_TYPE", "DETAIL"]

    # 메모리 절약: 필요 컬럼만, 상위 10행만
    display_df = alerts_df[COLS].head(MAX_ROWS).copy()

    # inline_notify 함수로 직접 큐봇 발송
    ok = _send_oper_table(oper_desc, display_df, len(alerts_df))

    # 나머지 건수 안내
    remaining = len(alerts_df) - len(display_df)
    if ok and remaining > 0:
        inline_notify._send(
            QBOT_USER, QBOT_CHANNEL,
            [{
                "header": {"title": f"[{oper_desc}]"},
                "body": {
                    "bodystyle": "grid",
                    "row": [{
                        "bgcolor": "#fff3e0", "border": True, "align": "",
                        "width": "",
                        "column": [inline_notify._build_text_block(
                            f"... 외 {remaining}건 생략 (상위 10건 표시)",
                            bg="#fff3e0"
                        )]
                    }]
                },
                "process": inline_notify._build_process_block()
            }]
        )

    # 즉시 메모리 해제
    del display_df


def _send_oper_table(oper_desc, display_df, total_count):
    """공정별 테이블 큐봇 발송"""
    rows = []

    # 헤더
    hdr_cols = [inline_notify._build_column(c, c, True) for c in display_df.columns]
    rows.append({
        "bgcolor": "#2e2e2e", "border": True, "align": "",
        "width": "", "column": hdr_cols
    })

    # 데이터
    for _, row in display_df.iterrows():
        data_cols = [inline_notify._build_column(row[c], c, False) for c in display_df.columns]
        rows.append({
            "bgcolor": "#ffffff", "border": True, "align": "",
            "width": "", "column": data_cols
        })

    content = [{
        "header": {"title": f"[{oper_desc}] 이상 {total_count}건"},
        "body": {"bodystyle": "grid", "row": rows},
        "process": inline_notify._build_process_block()
    }]

    return inline_notify._send(QBOT_USER, QBOT_CHANNEL, content)


# ============================================================
# goodDocsGetData (기존 그대로)
# ============================================================

def goodDocsGetData():
    col_url = "http://goodocs-vertx-api-basic-prd.api.hcp01.skhynix.com/goodocs-vertx-api/openAPI/v1/column"
    message = {
        'USER_ID': '2062272',
        'DOC_ID': '20622721781142383771',
        'TOKEN_SOURCE': 'NCNC',
        'TOKEN_KEY': 'NCNC-FEK4F4EE-472F-WDG1-BER3-9DGEWF3FDFGE'
    }
    col_res = requests.post(col_url, data=json.dumps(message))
    print("[ response code ] : ", col_res.status_code)
    rows = json.loads(col_res.json()['COLS'])
    columnDict = {}
    for row in rows:
        columnDict[row['colId']] = row['headerName']

    row_url = "http://goodocs-vertx-api-basic-prd.api.hcp01.skhynix.com/goodocs-vertx-api/openAPI/v1/row"
    row_res = requests.post(row_url, data=json.dumps(message))
    print("[ response code ] : ", row_res.status_code)

    info_df = pd.json_normalize(json.loads(row_res.json()['ROWS'].replace('\\""', "\\'")))
    for col in info_df.columns:
        changeCol = columnDict.get(col)
        info_df.rename(columns={col: changeCol}, inplace=True)

    info_df = info_df[['LOT_CD', 'OPER_ID', 'OPER_DET_DESC', 'PARAM_NM']]
    return info_df


# ============================================================
# 쿼리, 전처리, 이상 감지 함수들 (기존 그대로)
# ============================================================

def get_mt_list(days: int = DAYS) -> list:
    today  = datetime.now()
    months = set()
    for i in range(days + 1):
        months.add((today - timedelta(days=i)).strftime("%Y%m"))
    return sorted(months)


def build_inline_query(oper_id: str, param_nms: list) -> str:
    mt_tuple    = "('" + "','".join(get_mt_list()) + "')"
    param_tuple = "('" + "','".join(param_nms) + "')"
    start_dt    = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")

    return f"""
        SELECT
            LOT_CD, OPER_ID, OPER_DET_DESC,
            ALIAS_LOT_ID, WF_ID,
            PARAM_NM, MEAS_VAL,
            MAIN_EQP_ID, END_TM
        FROM lake_catalog.tas.tas_src_wf_metr_inf
        WHERE mt       IN {mt_tuple}
          AND end_tm   >= '{start_dt} 00:00:00'
          AND (LOT_CD LIKE '%E2' OR LOT_CD LIKE '%E9')
          AND OPER_ID   = '{oper_id}'
          AND PARAM_NM IN {param_tuple}
    """


def build_spec_query(lot_cds: list, oper_id: str, param_nms: list) -> str:
    lot_tuple   = "('" + "','".join(lot_cds) + "')"
    param_tuple = "('" + "','".join(param_nms) + "')"


    return f"""
        SELECT
            LOT_CD, OPER_ID, PARAM_NM,
            TARGET_VAL,
            ENGR_WF_UL_VAL AS UCL,
            ENGR_WF_LL_VAL AS LCL
        FROM lake_catalog.tas.tas_src_param_mas
        WHERE LOT_CD  IN {lot_tuple}
          AND OPER_ID  = '{oper_id}'
          AND PARAM_NM IN {param_tuple}
          AND PROD_ID = '*'
    """


def remove_outliers(s: pd.Series, k: float = 1.5) -> pd.Series:
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    if iqr == 0:
        return s
    return s[(s >= q1 - k * iqr) & (s <= q3 + k * iqr)]


def add_eqp_ch_id(df: pd.DataFrame) -> pd.DataFrame:
    def _ch(p):
        if not isinstance(p, str) or not p.upper().endswith("_TIME"):
            return ""
        u = p.upper()
        if re.search(r'_PA_|_PB_|_PA$|_PB$', u): return "_AB"
        if re.search(r'_PC_|_PD_|_PC$|_PD$', u): return "_CD"
        if re.search(r'_PL_|_PL$',            u): return "_L"
        if re.search(r'_PR_|_PR$',            u): return "_R"
        if re.search(r'_P1_|_P3_|_P1$|_P3$', u): return "_P13"
        if re.search(r'_P2_|_P4_|_P2$|_P4$', u): return "_P24"
        return ""
    df = df.copy()
    df["MAIN_EQP_CH_ID"] = df["MAIN_EQP_ID"] + df["PARAM_NM"].apply(_ch)
    return df


def expand_lot_cds(raw_list: list) -> list:
    VALID = {"5E2","6E2","5E9","6E9"}
    result = set()
    for lc in raw_list:
        u = lc.strip().upper()
        if u in VALID:
            result.add(u)
        elif u in ("E2","E9"):
            result.add(f"5{u}")
            result.add(f"6{u}")
    return sorted(result)


def detect_anomalies(df: pd.DataFrame, yesterday: str) -> pd.DataFrame:
    rows = []
    df = df.copy()
    df["DATE"] = df["END_TM"].dt.strftime("%Y-%m-%d")

    df_target = df[df["FAB"] == "5"]
    if df_target.empty:
        return pd.DataFrame()

    for (lot_type, param_nm), grp in df_target.groupby(["LOT_TYPE", "PARAM_NM"]):
        clean_idx = remove_outliers(grp["MEAS_VAL"]).index
        clean = grp.loc[clean_idx]
        if len(clean) < 5:
            continue

        all_mean = clean["MEAS_VAL"].mean()
        all_std  = clean["MEAS_VAL"].std()
        ucl = clean["UCL"].median()
        lcl = clean["LCL"].median()

        is_ran  = "_RAN" in param_nm.upper()
        is_gof  = "_GOF" in param_nm.upper()
        is_time = "_TIME" in param_nm.upper() or "POLISH" in param_nm.upper()

        if is_ran:
            lcl = 0

        yday = clean[clean["DATE"] == yesterday]
        if yday.empty:
            continue
        yday_mean = yday["MEAS_VAL"].mean()
        yday_n    = len(yday)

        base = {
            "LOT_TYPE":      lot_type,
            "OPER_ID":       grp["OPER_ID"].iloc[0],
            "OPER_DET_DESC": grp["OPER_DET_DESC"].iloc[0],
            "PARAM_NM":      param_nm,
            "ALL_MEAN":      round(all_mean, 4),
            "YDAY_MEAN":     round(yday_mean, 4),
            "N_YDAY":        yday_n,
        }

        if all_std > 0 and abs(yday_mean - all_mean) > MEAN_DIFF_SIGMA * all_std:
            if is_gof and yday_mean >= all_mean:
                pass
            elif is_ran and yday_mean <= all_mean:
                pass
            elif is_time:
                pass
            else:
                direction = "상승" if yday_mean > all_mean else "하강"
                diff_sigma = round(abs(yday_mean - all_mean) / all_std, 2)
                rows.append({**base,
                    "ALERT_TYPE": f"평균 {direction} ({diff_sigma}σ)",
                    "DETAIL":     f"15일={all_mean:.3f} → 어제={yday_mean:.3f}"})

        if not is_time:
            above = (yday["MEAS_VAL"] > all_mean).sum()
            below = yday_n - above
            if yday_n >= 3:
                if above / yday_n >= BIAS_RATIO:
                    if not is_gof:
                        rows.append({**base,
                            "ALERT_TYPE": f"상향 편측 ({above}/{yday_n})",
                            "DETAIL":     f"{above}/{yday_n}개 평균 상회"})
                elif below / yday_n >= BIAS_RATIO:
                    if not is_ran:
                        rows.append({**base,
                            "ALERT_TYPE": f"하향 편측 ({below}/{yday_n})",
                            "DETAIL":     f"{below}/{yday_n}개 평균 하회"})

        if pd.notna(ucl) and pd.notna(lcl):
            if is_ran:
                spec_out = yday[yday["MEAS_VAL"] > ucl]
            else:
                spec_out = yday[(yday["MEAS_VAL"] > ucl) | (yday["MEAS_VAL"] < lcl)]
            if not spec_out.empty:
                rows.append({**base,
                    "ALERT_TYPE": f"Spec Out ({len(spec_out)}건)",
                    "DETAIL":     f"UCL={ucl:.3f} / LCL={lcl:.3f}"})

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def save_to_postgres(df: pd.DataFrame, oper_id: str):
    try:
        from django.db import connections
        conn = connections["analysis_db"]

        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM cmp_inline_raw
                WHERE oper_id = %s
                  AND end_tm >= %s
            """, [oper_id, (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")])

        df.to_sql("cmp_inline_raw", conn.connection,
                  if_exists="append", index=False, method="multi",
                  chunksize=5000)

        print(f" → PG 적재 {len(df):,}행")

    except ImportError:
        import os
        save_dir = "data_cache"
        os.makedirs(save_dir, exist_ok=True)
        fpath = os.path.join(save_dir, f"{oper_id}.parquet")
        df.to_parquet(fpath, index=False, compression="snappy")
        print(f" → parquet 저장: {fpath}")

    except Exception as e:
        print(f" → PG 적재 실패: {e}")


def process_one_oper(lake, oper_id, oper_desc, oper_params, lot_cds, has_spec, df_info, save_db=True):
    print(f"    [1] 인라인 조회...", end="")
    lake.auto_run_sync_paragraph(code=build_inline_query(oper_id, oper_params))
    rst = lake.get_rst()
    if rst is None:
        print(f" ⚠️ 조회 실패")
        return pd.DataFrame()
    df = rst.toPandas()
    df.columns = df.columns.str.upper()
    print(f" {len(df):,}행")

    if df.empty:
        return pd.DataFrame()

    print(f"    [2] 전처리...", end="")
    df["END_TM"]   = pd.to_datetime(df["END_TM"], errors="coerce")
    df["MEAS_VAL"] = pd.to_numeric(df["MEAS_VAL"], errors="coerce")
    df["LOT_TYPE"] = df["LOT_CD"].str[-2:]
    df["FAB"]      = df["LOT_CD"].str[0]
    df = add_eqp_ch_id(df)
    print(f" 완료")

    print(f"    [3] Spec...", end="")
    if has_spec:
        spec_oper = df_info[df_info["OPER_ID"] == oper_id][
            ["LOT_CD","OPER_ID","PARAM_NM","UCL","LCL"]].copy()
        expanded = []
        for _, row in spec_oper.iterrows():
            for lc in expand_lot_cds([row["LOT_CD"]]):
                r = row.copy()
                r["LOT_CD"] = lc
                expanded.append(r)
        spec_oper = pd.DataFrame(expanded)
        print(f" df_info {len(spec_oper)}건")
    else:
        try:
            lake.auto_run_sync_paragraph(
                code=build_spec_query(lot_cds, oper_id, oper_params))
            spec_rst = lake.get_rst()
            if spec_rst is None:
                spec_oper = pd.DataFrame()
                print(f" ⚠️ 실패")
            else:
                spec_oper = spec_rst.toPandas()
                spec_oper.columns = spec_oper.columns.str.upper()
                print(f" DB {len(spec_oper)}건")
        except Exception as e:
            spec_oper = pd.DataFrame()
            print(f" ❌ {e}")

    for col in ["UCL","LCL","TARGET_VAL"]:
        if col in spec_oper.columns:
            spec_oper[col] = pd.to_numeric(spec_oper[col], errors="coerce")

    if not spec_oper.empty:
        df = pd.merge(df, spec_oper, on=["LOT_CD","OPER_ID","PARAM_NM"], how="left")
    else:
        df["UCL"] = np.nan
        df["LCL"] = np.nan
        df["TARGET_VAL"] = np.nan

    for param in df["PARAM_NM"].unique():
        mask = (df["PARAM_NM"] == param) & df["UCL"].isna()
        if mask.any():
            vals = remove_outliers(df.loc[df["PARAM_NM"] == param, "MEAS_VAL"].dropna())
            if not vals.empty:
                df.loc[mask, "UCL"] = vals.max()
                df.loc[mask, "LCL"] = vals.min()

    ran_mask = df["PARAM_NM"].str.upper().str.contains("_RAN", na=False)
    df.loc[ran_mask, "LCL"] = 0

    print(f"    [4] 이상 감지...", end="")
    alerts = detect_anomalies(df, YESTERDAY)
    print(f" {len(alerts)}건")

    if save_db:
        print(f"    [5] DB 적재...", end="")
        save_to_postgres(df, oper_id)

    del df
    gc.collect()

    return alerts


# ============================================================
# 메인 실행
# ============================================================

if __name__ == "__main__":
    import lakes

    lake      = lakes.LakeHouse(real_user_id="")
    starrocks = lake.ensure_running(cluster_type="starrocks")

    df_info = goodDocsGetData().dropna(axis=0)

    print(f"\n{'='*60}")
    print(f"CMP Inline Trend 점검  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"기준일: {YESTERDAY} / 메모리 4GB / 공정별 즉시 발송")
    print(f"{'='*60}")

    df_info.columns = df_info.columns.str.upper()
    oper_ids = df_info["OPER_ID"].unique().tolist()
    has_spec = "UCL" in df_info.columns and "LCL" in df_info.columns
    lot_cds  = expand_lot_cds(df_info["LOT_CD"].unique().tolist())

    total_alerts = 0

    for i, oper_id in enumerate(oper_ids, 1):
        oper_params = df_info[df_info["OPER_ID"] == oper_id]["PARAM_NM"].unique().tolist()
        oper_desc   = df_info[df_info["OPER_ID"] == oper_id]["OPER_DET_DESC"].iloc[0] \
                      if "OPER_DET_DESC" in df_info.columns else oper_id

        print(f"\n[{i}/{len(oper_ids)}] {oper_desc} ({oper_id}) param={len(oper_params)}")

        try:
            alerts = process_one_oper(
                lake, oper_id, oper_desc, oper_params,
                lot_cds, has_spec, df_info, save_db=True)

            if not alerts.empty:
                total_alerts += len(alerts)

                # [핵심] 공정별 즉시 큐봇 발송
                _send_oper_alert(oper_desc, alerts)

                print(f"  → ⚠️ {len(alerts)}건 (큐봇 발송 완료)")

                # 즉시 메모리 해제
                del alerts
                gc.collect()
            else:
                print(f"  → ✅ 정상")

            gc.collect()

        except Exception as e:
            print(f"  → ❌ 오류: {e}")
            # 오류도 간단히 알림
            inline_notify._send(
                QBOT_USER, QBOT_CHANNEL,
                [{
                    "header": {"title": "CMP 오류"},
                    "body": {
                        "bodystyle": "grid",
                        "row": [{
                            "bgcolor": "#ffebee", "border": True, "align": "",
                            "width": "",
                            "column": [inline_notify._build_text_block(
                                f"[{oper_desc}] 처리 오류: {str(e)[:100]}",
                                bg="#ffebee"
                            )]
                        }]
                    },
                    "process": inline_notify._build_process_block()
                }]
            )
            continue

    # 전체 완료
    status = "모두 정상" if total_alerts == 0 else f"총 이상 {total_alerts}건"
    inline_notify._send(
        QBOT_USER, QBOT_CHANNEL,
        [{
            "header": {"title": "CMP 점검 완료"},
            "body": {
                "bodystyle": "grid",
                "row": [{
                    "bgcolor": "#e8f5e9" if total_alerts == 0 else "#fff3e0",
                    "border": True, "align": "",
                    "width": "",
                    "column": [inline_notify._build_text_block(
                        f"CMP Inline Trend 점검 완료\n"
                        f"기준일: {YESTERDAY}\n"
                        f"공정 {len(oper_ids)}개 점검 완료\n"
                        f"{status}",
                        bg="#e8f5e9" if total_alerts == 0 else "#fff3e0"
                    )]
                }]
            },
            "process": inline_notify._build_process_block()
        }]
    )

    print(f"\n{'='*60}")
    print(f"✅ 점검 완료 — {status}")
    print(f"{'='*60}")
    gc.collect()
