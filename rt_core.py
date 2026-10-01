"""
국토부 실거래가 범용 수집/저장 모듈.

- 8종 데이터셋(아파트·오피스텔·연립다세대·단독다가구 × 매매·전월세)을 하나의 스키마로 정규화
- SQLite(realestate.db)에 (데이터셋, 시군구, 계약년월) 단위로 저장
- 증분 수집: 확정된 과거 월은 건너뛰고, 신고 기한/해제 반영이 남은 최근 월만 다시 받는다
"""

import calendar
import csv
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from rt_regions import CODE_TO_SGG

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "realestate.db"
API_BASE = "https://apis.data.go.kr/1613000/"

# 계약 후 30일 내 신고 + 해제 신고까지 감안해, 월말로부터 이 기간이 지나야 '확정'으로 본다.
FINAL_AFTER_DAYS = 90
# 미확정 월이라도 이 시간 안에 받은 적이 있으면 다시 받지 않는다.
REFETCH_MIN_HOURS = 12
PAGE_SIZE = 1000
REQUEST_DELAY = 0.15

DATASETS = {
    "apt_trade":  {"label": "아파트 매매",       "prop": "아파트",     "kind": "매매",
                   "path": "RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade", "name_tag": "aptNm",
                   "apply": "국토교통부_아파트 매매 실거래가 자료"},
    "apt_rent":   {"label": "아파트 전월세",     "prop": "아파트",     "kind": "전월세",
                   "path": "RTMSDataSvcAptRent/getRTMSDataSvcAptRent", "name_tag": "aptNm",
                   "apply": "국토교통부_아파트 전월세 실거래가 자료"},
    "offi_trade": {"label": "오피스텔 매매",     "prop": "오피스텔",   "kind": "매매",
                   "path": "RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade", "name_tag": "offiNm",
                   "apply": "국토교통부_오피스텔 매매 실거래가 자료"},
    "offi_rent":  {"label": "오피스텔 전월세",   "prop": "오피스텔",   "kind": "전월세",
                   "path": "RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent", "name_tag": "offiNm",
                   "apply": "국토교통부_오피스텔 전월세 실거래가 자료"},
    "rh_trade":   {"label": "연립다세대 매매",   "prop": "연립다세대", "kind": "매매",
                   "path": "RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade", "name_tag": "mhouseNm",
                   "apply": "국토교통부_연립다세대 매매 실거래가 자료"},
    "rh_rent":    {"label": "연립다세대 전월세", "prop": "연립다세대", "kind": "전월세",
                   "path": "RTMSDataSvcRHRent/getRTMSDataSvcRHRent", "name_tag": "mhouseNm",
                   "apply": "국토교통부_연립다세대 전월세 실거래가 자료"},
    "sh_trade":   {"label": "단독다가구 매매",   "prop": "단독다가구", "kind": "매매",
                   "path": "RTMSDataSvcSHTrade/getRTMSDataSvcSHTrade", "name_tag": None,
                   "apply": "국토교통부_단독/다가구 매매 실거래가 자료"},
    "sh_rent":    {"label": "단독다가구 전월세", "prop": "단독다가구", "kind": "전월세",
                   "path": "RTMSDataSvcSHRent/getRTMSDataSvcSHRent", "name_tag": None,
                   "apply": "국토교통부_단독/다가구 전월세 실거래가 자료"},
}

COLUMNS = [
    "dataset", "prop_type", "deal_kind", "sgg_cd", "sgg_nm", "umd", "jibun", "name", "apt_dong",
    "area", "deal_date", "deal_ym", "price", "deposit", "monthly_rent", "floor", "build_year",
    "dealing_gbn", "cancel_type", "cancel_date", "seller", "buyer", "agent_sgg",
    "contract_type", "contract_term", "house_type", "pre_deposit", "pre_monthly_rent", "use_rr_right",
]

# 화면/CSV 표시용 한글 컬럼명
KOREAN = {
    "prop_type": "유형", "deal_kind": "거래", "sgg_nm": "시군구", "umd": "법정동", "jibun": "지번",
    "name": "단지·건물명", "apt_dong": "동", "area": "전용면적(㎡)", "deal_date": "계약일",
    "deal_ym": "계약년월", "price": "거래금액(만원)", "deposit": "보증금(만원)",
    "monthly_rent": "월세(만원)", "floor": "층", "build_year": "건축년도", "dealing_gbn": "거래방식",
    "cancel_type": "해제여부", "cancel_date": "해제일", "seller": "매도자", "buyer": "매수자",
    "agent_sgg": "중개사소재지", "contract_type": "계약구분", "contract_term": "계약기간",
    "house_type": "주택유형", "pre_deposit": "종전보증금(만원)", "pre_monthly_rent": "종전월세(만원)",
    "use_rr_right": "갱신요구권", "sgg_cd": "시군구코드", "dataset": "데이터셋",
}


class ApiNotRegistered(Exception):
    """인증키에 해당 데이터셋 활용신청이 안 된 경우."""


class ApiError(Exception):
    pass


# ── 유틸 ─────────────────────────────────────────────

def month_list(start_ym, end_ym):
    y, m = int(start_ym[:4]), int(start_ym[4:])
    ey, em = int(end_ym[:4]), int(end_ym[4:])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def current_ym():
    return date.today().strftime("%Y%m")


def _to_int(val):
    val = (val or "").replace(",", "").strip()
    try:
        return int(float(val)) if val else None
    except ValueError:
        return None


def _to_float(val):
    try:
        return float(val) if val and val.strip() else None
    except ValueError:
        return None


def load_service_key():
    from config import SERVICE_KEY
    return SERVICE_KEY


# ── API ─────────────────────────────────────────────

def _request(dataset, sgg_cd, ym, page, key, num_rows=PAGE_SIZE):
    url = API_BASE + DATASETS[dataset]["path"]
    params = {"serviceKey": key, "LAWD_CD": sgg_cd, "DEAL_YMD": ym,
              "numOfRows": num_rows, "pageNo": page}
    last_exc = None
    for attempt in range(4):
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
            continue
        text = resp.text
        if "SERVICE_KEY_IS_NOT_REGISTERED" in text:
            raise ApiNotRegistered(f"{DATASETS[dataset]['label']}: 인증키 미등록 "
                                   f"(data.go.kr에서 '{DATASETS[dataset]['apply']}' 활용신청 필요)")
        if "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in text:
            raise ApiError("일일 호출 한도 초과 - 내일 다시 시도하세요")
        if resp.status_code >= 500:
            last_exc = ApiError(f"HTTP {resp.status_code}")
            time.sleep(1.5 * (attempt + 1))
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            last_exc = ApiError(f"응답 파싱 실패: {text[:120]}")
            time.sleep(1.5 * (attempt + 1))
            continue
        code = root.findtext(".//resultCode", "")
        if code and code not in ("00", "000"):
            raise ApiError(f"[{code}] {root.findtext('.//resultMsg', '')}")
        return root
    raise ApiError(f"요청 실패 ({sgg_cd} {ym}): {last_exc}")


def _normalize(dataset, item, sgg_cd):
    meta = DATASETS[dataset]
    g = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
    y, m, d = g("dealYear"), g("dealMonth"), g("dealDay")
    deal_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}" if y and m and d else ""
    name = g(meta["name_tag"]) if meta["name_tag"] else ""
    return {
        "dataset": dataset,
        "prop_type": meta["prop"],
        "deal_kind": meta["kind"],
        "sgg_cd": sgg_cd,
        "sgg_nm": g("sggNm") or CODE_TO_SGG.get(sgg_cd, sgg_cd),
        "umd": g("umdNm"),
        "jibun": g("jibun"),
        "name": name,
        "apt_dong": g("aptDong"),
        "area": _to_float(g("excluUseAr") or g("totalFloorAr")),
        "deal_date": deal_date,
        "deal_ym": f"{y}{m.zfill(2)}" if y and m else "",
        "price": _to_int(g("dealAmount")),
        "deposit": _to_int(g("deposit")),
        "monthly_rent": _to_int(g("monthlyRent")),
        "floor": _to_int(g("floor")),
        "build_year": _to_int(g("buildYear")),
        "dealing_gbn": g("dealingGbn"),
        "cancel_type": g("cdealType"),
        "cancel_date": g("cdealDay"),
        "seller": g("slerGbn"),
        "buyer": g("buyerGbn"),
        "agent_sgg": g("estateAgentSggNm"),
        "contract_type": g("contractType"),
        "contract_term": g("contractTerm"),
        "house_type": g("houseType"),
        "pre_deposit": _to_int(g("preDeposit")),
        "pre_monthly_rent": _to_int(g("preMonthlyRent")),
        "use_rr_right": g("useRRRight"),
    }


def fetch_month(dataset, sgg_cd, ym, key=None):
    """한 데이터셋·시군구·월의 전체 거래를 정규화된 dict 리스트로 반환."""
    key = key or load_service_key()
    rows, page = [], 1
    while True:
        root = _request(dataset, sgg_cd, ym, page, key)
        total = int(root.findtext(".//totalCount") or 0)
        items = root.findall(".//item")
        rows.extend(_normalize(dataset, it, sgg_cd) for it in items)
        if len(rows) >= total or not items:
            return rows
        page += 1
        time.sleep(REQUEST_DELAY)


def check_access(datasets=None, key=None, sgg_cd="11560"):
    """데이터셋별 인증키 사용 가능 여부 {dataset: True/False/에러문자열}."""
    key = key or load_service_key()
    ym = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y%m")
    out = {}
    for ds in datasets or DATASETS:
        try:
            _request(ds, sgg_cd, ym, 1, key, num_rows=1)
            out[ds] = True
        except ApiNotRegistered:
            out[ds] = False
        except Exception as exc:  # 네트워크 오류 등
            out[ds] = str(exc)
    return out


# ── 저장소 ───────────────────────────────────────────

def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute(f"""CREATE TABLE IF NOT EXISTS deals (
        {', '.join(COLUMNS)}
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_deals_key ON deals(dataset, sgg_cd, deal_ym)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_deals_name ON deals(name)")
    conn.execute("""CREATE TABLE IF NOT EXISTS fetch_log (
        dataset TEXT, sgg_cd TEXT, ym TEXT, fetched_at TEXT, n INTEGER,
        PRIMARY KEY (dataset, sgg_cd, ym)
    )""")
    return conn


def save_month(conn, dataset, sgg_cd, ym, rows):
    """해당 월 데이터를 통째로 교체(해제·정정 신고 반영)."""
    with conn:
        conn.execute("DELETE FROM deals WHERE dataset=? AND sgg_cd=? AND deal_ym=?",
                     (dataset, sgg_cd, ym))
        conn.executemany(
            f"INSERT INTO deals ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
            [tuple(r[c] for c in COLUMNS) for r in rows],
        )
        conn.execute("INSERT OR REPLACE INTO fetch_log VALUES (?, ?, ?, ?, ?)",
                     (dataset, sgg_cd, ym, datetime.now().isoformat(timespec="seconds"), len(rows)))


def needs_fetch(conn, dataset, sgg_cd, ym, force=False):
    if force:
        return True
    row = conn.execute("SELECT fetched_at FROM fetch_log WHERE dataset=? AND sgg_cd=? AND ym=?",
                       (dataset, sgg_cd, ym)).fetchone()
    if not row:
        return True
    fetched_at = datetime.fromisoformat(row[0])
    y, m = int(ym[:4]), int(ym[4:])
    month_end = datetime(y, m, calendar.monthrange(y, m)[1])
    if fetched_at >= month_end + timedelta(days=FINAL_AFTER_DAYS):
        return False  # 확정된 월
    return datetime.now() - fetched_at > timedelta(hours=REFETCH_MIN_HOURS)


def collect(datasets, sgg_codes, months, force=False, progress=None, db_path=DB_PATH):
    """
    증분 수집. progress(done, total, message) 콜백으로 진행 상황을 알린다.
    반환: {"fetched": n월, "skipped": n월, "rows": n건, "errors": [...], "unregistered": [...]}
    """
    key = load_service_key()
    conn = connect(db_path)
    jobs = [(ds, sgg, ym) for ds in datasets for sgg in sgg_codes for ym in months]
    summary = {"fetched": 0, "skipped": 0, "rows": 0, "errors": [], "unregistered": []}
    blocked = set()
    try:
        for i, (ds, sgg, ym) in enumerate(jobs, 1):
            label = f"{DATASETS[ds]['label']} {CODE_TO_SGG.get(sgg, sgg)} {ym[:4]}-{ym[4:]}"
            if ds in blocked:
                continue
            if not needs_fetch(conn, ds, sgg, ym, force):
                summary["skipped"] += 1
                if progress:
                    progress(i, len(jobs), f"{label} (저장됨, 건너뜀)")
                continue
            try:
                rows = fetch_month(ds, sgg, ym, key)
            except ApiNotRegistered as exc:
                blocked.add(ds)
                summary["unregistered"].append(str(exc))
                if progress:
                    progress(i, len(jobs), str(exc))
                continue
            except ApiError as exc:
                summary["errors"].append(f"{label}: {exc}")
                if "한도" in str(exc):
                    break
                continue
            save_month(conn, ds, sgg, ym, rows)
            summary["fetched"] += 1
            summary["rows"] += len(rows)
            if progress:
                progress(i, len(jobs), f"{label}: {len(rows)}건")
            time.sleep(REQUEST_DELAY)
    finally:
        conn.close()
    return summary


def coverage(db_path=DB_PATH):
    """데이터셋·시군구별 보유 기간/건수 요약 (list of dict)."""
    conn = connect(db_path)
    try:
        cur = conn.execute("""
            SELECT dataset, sgg_cd, MIN(ym), MAX(ym), COUNT(*), SUM(n), MAX(fetched_at)
            FROM fetch_log GROUP BY dataset, sgg_cd ORDER BY dataset, sgg_cd""")
        return [dict(zip(["dataset", "sgg_cd", "from", "to", "months", "rows", "last_fetch"], r))
                for r in cur.fetchall()]
    finally:
        conn.close()


def export_legacy_offi_csv(path, sgg_codes, db_path=DB_PATH):
    """combined_analysis.py가 읽는 molit_all.csv 형식으로 오피스텔 매매를 내보낸다."""
    fields = ["구", "읍면동", "지번", "오피스텔명", "전용면적(㎡)", "계약년도", "계약월", "계약일",
              "계약년월일", "거래금액(만원)", "층", "건축년도", "거래유형", "해제여부", "해제사유발생일",
              "중개사소재지", "매도자구분", "매수자구분"]
    conn = connect(db_path)
    try:
        q = f"""SELECT * FROM deals WHERE dataset='offi_trade'
                AND sgg_cd IN ({','.join('?' * len(sgg_codes))}) ORDER BY sgg_cd, deal_date"""
        cur = conn.execute(q, sgg_codes)
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            y, m, d = (r["deal_date"].split("-") + ["", "", ""])[:3]
            w.writerow({
                "구": r["sgg_nm"], "읍면동": r["umd"], "지번": r["jibun"], "오피스텔명": r["name"],
                "전용면적(㎡)": r["area"], "계약년도": y, "계약월": str(int(m)) if m else "",
                "계약일": str(int(d)) if d else "", "계약년월일": r["deal_date"],
                "거래금액(만원)": r["price"], "층": r["floor"], "건축년도": r["build_year"],
                "거래유형": r["dealing_gbn"], "해제여부": r["cancel_type"],
                "해제사유발생일": r["cancel_date"], "중개사소재지": r["agent_sgg"],
                "매도자구분": r["seller"], "매수자구분": r["buyer"],
            })
    return len(rows)
