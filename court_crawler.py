"""
대법원 경매정보 낙찰결과 크롤러 (requests 기반)
- API: https://www.courtauction.go.kr/pgj/pgjsearch/selectDspslSchdRsltSrch.on
- 서울남부지방법원 오피스텔 낙찰결과
- 결과: court_auction.csv 저장

사용법:
    py court_crawler.py               # 전체 낙찰결과 (날짜 무관)
    py court_crawler.py 20250101      # 특정 시작일 이후
    py court_crawler.py 20250101 20251231  # 기간 지정
"""

import sys
import time
import re
import requests
import csv
from datetime import datetime

# ── 설정 ──────────────────────────────────────────────
COURT_CODE = "B000212"   # 서울남부지방법원
DELAY_SEC  = 1.0         # 요청 간 딜레이
PAGE_SIZE  = 20
OUTPUT_CSV = "court_auction.csv"
# ──────────────────────────────────────────────────────

BASE_URL   = "https://www.courtauction.go.kr"
SEARCH_URL = f"{BASE_URL}/pgj/pgjsearch/selectDspslSchdRsltSrch.on"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36",
    "Content-Type":    "application/json; charset=UTF-8",
    "Referer":         f"{BASE_URL}/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ158M00.xml",
    "Origin":          BASE_URL,
    "Accept":          "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With":"XMLHttpRequest",
}

OFFICETEL_KEYWORDS = ["오피스텔"]


def parse_args():
    args = sys.argv[1:]
    start = args[0] if len(args) >= 1 else None
    end   = args[1] if len(args) >= 2 else None
    return start, end


def in_date_range(mae_giil: str, start: str, end: str) -> bool:
    if not mae_giil or len(mae_giil) < 8:
        return True  # 날짜 없으면 통과
    d = mae_giil[:8]
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def is_officetel(row: dict) -> bool:
    usage_nm = row.get("dspslUsgNm", "") or ""
    mul_bigo = row.get("mulBigo", "") or ""
    for kw in OFFICETEL_KEYWORDS:
        if kw in usage_nm or kw in mul_bigo:
            return True
    return False


def parse_int(val) -> int:
    try:
        return int(re.sub(r"[^\d]", "", str(val)) or "0")
    except Exception:
        return 0


def build_payload(page_no: int, total_yn: str = "N") -> dict:
    return {
        "dma_pageInfo": {
            "pageNo": str(page_no),
            "pageSize": str(PAGE_SIZE),
            "bfPageNo": str(page_no - 1),
            "startRowNo": "",
            "totalCnt": "",
            "totalYn": total_yn,
            "groupTotalCount": ""
        },
        "dma_srchGdsDtlSrchInfo": {
            "statNum": "3",
            "pgmId": "PGJ158M01",
            "cortStDvs": "1",
            "cortOfcCd": COURT_CODE,
            "jdbnCd": "", "csNo": "",
            "rprsAdongSdCd": "", "rprsAdongSggCd": "", "rprsAdongEmdCd": "",
            "rdnmSdCd": "", "rdnmSggCd": "", "rdnmNo": "",
            "auctnGdsStatCd": "02",  # 02=낙찰
            "lclDspslGdsLstUsgCd": "",
            "mclDspslGdsLstUsgCd": "",
            "sclDspslGdsLstUsgCd": "",
            "dspslAmtMin": "", "dspslAmtMax": "",
            "aeeEvlAmtMin": "", "aeeEvlAmtMax": "",
            "flbdNcntMin": "", "flbdNcntMax": "",
            "lafjOrderBy": ""
        }
    }


def make_record(row: dict) -> dict:
    감정가 = parse_int(row.get("gamevalAmt", 0)) // 10000  # 원 → 만원
    낙찰가 = parse_int(row.get("maeAmt", 0)) // 10000        # 원 → 만원
    mae_giil = row.get("maeGiil", "")
    # 매각기일 포맷: yyyyMMdd → yyyy-MM-dd
    if len(mae_giil) == 8:
        mae_giil = f"{mae_giil[:4]}-{mae_giil[4:6]}-{mae_giil[6:]}"

    return {
        "매각기일":   mae_giil,
        "사건번호":   row.get("srnSaNo", ""),
        "물건번호":   row.get("maemulSer", ""),
        "소재지":     row.get("printSt", "") or row.get("convAddr", ""),
        "용도":       row.get("dspslUsgNm", ""),
        "감정가(만원)":   감정가,
        "최저입찰가(만원)": parse_int(row.get("minmaePrice", 0)) // 10000,
        "낙찰가(만원)":   낙찰가,
        "낙찰가율(%)": round(낙찰가 / 감정가 * 100, 1) if 감정가 > 0 else "",
        "유찰횟수":   parse_int(row.get("yuchalCnt", 0)),
        "법원":       "서울남부지방법원",
    }


def run():
    start_date, end_date = parse_args()

    if start_date or end_date:
        period_label = f"{start_date or '전체'} ~ {end_date or '전체'}"
    else:
        period_label = "전체 (날짜 무관)"

    print(f"[서울남부지방법원] 낙찰결과 조회")
    print(f"기간: {period_label}\n")

    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        sess.get(f"{BASE_URL}/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ158M00.xml", timeout=10)
    except Exception:
        pass

    # 1페이지로 전체 건수 파악
    payload = build_payload(1, total_yn="Y")
    resp = sess.post(SEARCH_URL, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()["data"]

    total_cnt   = int(data["dma_pageInfo"].get("totalCnt", 0))
    total_pages = (total_cnt + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"  낙찰 전체 건수: {total_cnt}건 / {total_pages}페이지")

    all_records   = []
    offi_records  = []
    skipped_date  = 0

    for page in range(1, total_pages + 1):
        if page > 1:
            payload = build_payload(page, total_yn="N")
            time.sleep(DELAY_SEC)
            resp = sess.post(SEARCH_URL, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()["data"]

        results = data.get("dlt_srchResult", [])
        if not results:
            break

        page_offi = 0
        for row in results:
            mae_giil_raw = row.get("maeGiil", "")
            if not in_date_range(mae_giil_raw, start_date, end_date):
                skipped_date += 1
                continue

            rec = make_record(row)
            all_records.append(rec)
            if is_officetel(row):
                offi_records.append(rec)
                page_offi += 1

        print(f"  p{page}/{total_pages}: {len(results)}건 수신, 오피스텔 {page_offi}건")

    if skipped_date:
        print(f"\n  (날짜 범위 외 제외: {skipped_date}건)")

    # 오피스텔 CSV 저장
    save_csv(offi_records, OUTPUT_CSV)

    # 용도별 분포 출력
    print("\n─── 용도별 분포 (전체 낙찰) ───")
    usage_cnt: dict = {}
    for r in all_records:
        k = r["용도"] or "(미분류)"
        usage_cnt[k] = usage_cnt.get(k, 0) + 1
    for k, v in sorted(usage_cnt.items(), key=lambda x: -x[1]):
        marker = " ◀ 오피스텔" if "오피스텔" in k else ""
        print(f"  {k}: {v}건{marker}")


def save_csv(records: list, filepath: str):
    if not records:
        print(f"\n⚠ 오피스텔 낙찰 결과가 없습니다.")
        return

    fieldnames = list(records[0].keys())
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    print(f"\n오피스텔 낙찰: {len(records)}건 → {filepath}")


if __name__ == "__main__":
    run()
