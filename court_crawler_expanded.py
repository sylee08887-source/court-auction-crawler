"""
서울남부지방법원 오피스텔 경매 데이터 확장 수집기.

기존 낙찰결과(status=02)만 보지 않고, 매각결과 화면에서 조회 가능한
여러 상태/통계 구분을 순회해 가능한 많은 오피스텔 경매 행을 확보한다.

사용법:
    py court_crawler_expanded.py
    py court_crawler_expanded.py 20250101 20261231
"""

import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests


COURT_CODE = "B000212"
COURT_NAME = "서울남부지방법원"
PAGE_SIZE = 20
DELAY_SEC = 0.4
OUTPUT_CSV = "court_auction_expanded.csv"
ARCHIVE_CSV = "court_auction_archive.csv"

BASE_URL = "https://www.courtauction.go.kr"
SEARCH_URL = f"{BASE_URL}/pgj/pgjsearch/selectDspslSchdRsltSrch.on"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36",
    "Content-Type": "application/json; charset=UTF-8",
    "Referer": f"{BASE_URL}/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ158M00.xml",
    "Origin": BASE_URL,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

STATUS_NAMES = {
    "": "전체",
    "01": "매각공고",
    "02": "매각",
    "03": "유찰",
    "04": "매각허가결정",
    "05": "매각불허가결정",
    "06": "대금납부",
    "07": "대금미납",
    "08": "매각허가취소결정",
}

# statNum은 화면 내부 검색 구분값이다. 사이트가 공식 문서화하지 않아,
# 실제 응답이 확인된 구분을 순회하고 사건번호 기준으로 중복 제거한다.
SEARCH_MATRIX = [
    ("3", ""),
    ("3", "02"),
    ("3", "03"),
    ("3", "04"),
    ("3", "05"),
    ("5", "01"),
    ("5", "03"),
    ("5", "04"),
    ("5", "05"),
    ("5", "06"),
    ("5", "07"),
    ("5", "08"),
]

FIELDNAMES = [
    "수집일시", "검색구분", "경매상태코드", "경매상태", "매각기일",
    "사건번호", "물건번호", "소재지", "용도", "감정가(만원)",
    "최저입찰가(만원)", "낙찰가(만원)", "낙찰가율(%)", "유찰횟수", "법원",
    # 시세 매칭용 구조화 필드 (도로명 주소 물건도 법정동·지번이 따로 내려온다)
    "구", "법정동", "지번", "건물명", "층호", "전용면적(㎡)",
]


def parse_args():
    args = sys.argv[1:]
    if args and args[0] == "--archive-existing":
        return None, None, True
    return args[0] if len(args) >= 1 else None, args[1] if len(args) >= 2 else None, False


def parse_int(val):
    try:
        return int(re.sub(r"[^\d]", "", str(val)) or "0")
    except Exception:
        return 0


def in_date_range(mae_giil, start, end):
    if not mae_giil or len(mae_giil) < 8:
        return True
    d = mae_giil[:8]
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def is_officetel(row):
    haystack = " ".join([
        row.get("dspslUsgNm", "") or "",
        row.get("mulBigo", "") or "",
        row.get("printSt", "") or "",
        row.get("convAddr", "") or "",
    ])
    return "오피스텔" in haystack


def build_payload(stat_num, status_code, page_no, total_yn="N"):
    return {
        "dma_pageInfo": {
            "pageNo": str(page_no),
            "pageSize": str(PAGE_SIZE),
            "bfPageNo": str(page_no - 1),
            "startRowNo": "",
            "totalCnt": "",
            "totalYn": total_yn,
            "groupTotalCount": "",
        },
        "dma_srchGdsDtlSrchInfo": {
            "statNum": stat_num,
            "pgmId": "PGJ158M01",
            "cortStDvs": "1",
            "cortOfcCd": COURT_CODE,
            "jdbnCd": "",
            "csNo": "",
            "rprsAdongSdCd": "",
            "rprsAdongSggCd": "",
            "rprsAdongEmdCd": "",
            "rdnmSdCd": "",
            "rdnmSggCd": "",
            "rdnmNo": "",
            "auctnGdsStatCd": status_code,
            "lclDspslGdsLstUsgCd": "",
            "mclDspslGdsLstUsgCd": "",
            "sclDspslGdsLstUsgCd": "",
            "dspslAmtMin": "",
            "dspslAmtMax": "",
            "aeeEvlAmtMin": "",
            "aeeEvlAmtMax": "",
            "flbdNcntMin": "",
            "flbdNcntMax": "",
            "lafjOrderBy": "",
        },
    }


def make_record(row, stat_num, status_code, collected_at):
    appraisal = parse_int(row.get("gamevalAmt", 0)) // 10000
    sold_price = parse_int(row.get("maeAmt", 0)) // 10000
    mae_giil = row.get("maeGiil", "")
    if len(mae_giil) >= 8:
        mae_giil = f"{mae_giil[:4]}-{mae_giil[4:6]}-{mae_giil[6:8]}"
    return {
        "수집일시": collected_at,
        "검색구분": stat_num,
        "경매상태코드": status_code,
        "경매상태": STATUS_NAMES.get(status_code, status_code),
        "매각기일": mae_giil,
        "사건번호": row.get("srnSaNo", ""),
        "물건번호": row.get("maemulSer", ""),
        "소재지": row.get("printSt", "") or row.get("convAddr", ""),
        "용도": row.get("dspslUsgNm", ""),
        "감정가(만원)": appraisal,
        "최저입찰가(만원)": parse_int(row.get("minmaePrice", 0)) // 10000,
        "낙찰가(만원)": sold_price,
        "낙찰가율(%)": round(sold_price / appraisal * 100, 1) if appraisal and sold_price else "",
        "유찰횟수": parse_int(row.get("yuchalCnt", 0)),
        "법원": COURT_NAME,
        "구": row.get("hjguSigu", ""),
        "법정동": row.get("hjguDong", ""),
        "지번": row.get("daepyoLotno", ""),
        "건물명": row.get("buldNm", ""),
        "층호": row.get("buldList", ""),
        "전용면적(㎡)": parse_area(row.get("pjbBuldList", "")),
    }


def parse_area(text):
    """'철근콘크리트구조 44.16㎡' -> 44.16. 면적이 여러 개(복수 호실)면 빈 값."""
    areas = re.findall(r"(\d+(?:\.\d+)?)\s*㎡", text or "")
    return float(areas[0]) if len(areas) == 1 else ""


def fetch_search(session, stat_num, status_code):
    payload = build_payload(stat_num, status_code, 1, "Y")
    resp = session.post(SEARCH_URL, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    total_cnt = int(data.get("dma_pageInfo", {}).get("totalCnt") or 0)
    total_pages = (total_cnt + PAGE_SIZE - 1) // PAGE_SIZE
    rows = list(data.get("dlt_srchResult", []))

    for page in range(2, total_pages + 1):
        time.sleep(DELAY_SEC)
        payload = build_payload(stat_num, status_code, page, "N")
        resp = session.post(SEARCH_URL, json=payload, timeout=20)
        resp.raise_for_status()
        rows.extend(resp.json().get("data", {}).get("dlt_srchResult", []))
    return total_cnt, rows


def dedupe(records):
    best = {}
    for rec in records:
        key = (
            rec["사건번호"],
            rec["물건번호"],
            rec["소재지"],
            rec["매각기일"],
            rec["경매상태코드"],
        )
        old = best.get(key)
        if old is None or parse_int(rec["낙찰가(만원)"]) > parse_int(old["낙찰가(만원)"]):
            best[key] = rec
    return list(best.values())


def load_existing_csv(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def archive_key(record):
    return (
        record.get("사건번호", ""),
        record.get("물건번호", ""),
        record.get("소재지", ""),
        record.get("매각기일", ""),
        record.get("경매상태코드", ""),
    )


def merge_archive(existing, current):
    merged = {archive_key(row): row for row in existing if archive_key(row) != ("", "", "", "", "")}
    for row in current:
        merged[archive_key(row)] = row
    return sorted(merged.values(), key=lambda r: (
        r.get("매각기일", ""),
        r.get("사건번호", ""),
        r.get("물건번호", ""),
        r.get("경매상태코드", ""),
    ))


def save_csv(path, records):
    actual_path = path
    try:
        f = open(actual_path, "w", newline="", encoding="utf-8-sig")
    except PermissionError:
        stem = Path(path).stem
        suffix = Path(path).suffix
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        actual_path = f"{stem}_{timestamp}{suffix}"
        f = open(actual_path, "w", newline="", encoding="utf-8-sig")
        print(f"  [경고] {path} 파일이 잠겨 있어 {actual_path}로 저장합니다.")

    with f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, restval="", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    return actual_path


def main():
    start_date, end_date, archive_existing = parse_args()
    if archive_existing:
        records = load_existing_csv(OUTPUT_CSV)
        if not records:
            print(f"기존 스냅샷이 없습니다: {OUTPUT_CSV}")
            return
        existing = load_existing_csv(ARCHIVE_CSV)
        archive = merge_archive(existing, records)
        actual_archive = save_csv(ARCHIVE_CSV, archive)
        archived_sold = [r for r in archive if parse_int(r["낙찰가(만원)"]) > 0]
        print(f"기존 스냅샷 병합 완료: {len(records)}건")
        print(f"누적 아카이브: {len(archive)}건 -> {actual_archive}")
        print(f"누적 낙찰가 보유: {len(archived_sold)}건")
        return

    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get(f"{BASE_URL}/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ158M00.xml", timeout=10)
    except Exception:
        pass

    all_records = []
    print(f"[{COURT_NAME}] 오피스텔 경매 확장 수집")
    print(f"기간: {start_date or '전체'} ~ {end_date or '전체'}")
    for stat_num, status_code in SEARCH_MATRIX:
        try:
            total, rows = fetch_search(session, stat_num, status_code)
        except Exception as exc:
            print(f"  [오류] stat={stat_num} status={status_code or 'ALL'}: {exc}")
            continue

        records = [
            make_record(row, stat_num, status_code, collected_at)
            for row in rows
            if is_officetel(row) and in_date_range(row.get("maeGiil", ""), start_date, end_date)
        ]
        all_records.extend(records)
        print(
            f"  stat={stat_num} status={status_code or 'ALL'} "
            f"전체 {total}건 / 오피스텔 {len(records)}건"
        )
        time.sleep(DELAY_SEC)

    records = sorted(dedupe(all_records), key=lambda r: (r["매각기일"], r["사건번호"], r["물건번호"]))
    actual_output = save_csv(OUTPUT_CSV, records)

    existing = load_existing_csv(ARCHIVE_CSV)
    archive = merge_archive(existing, records)
    actual_archive = save_csv(ARCHIVE_CSV, archive)

    sold = [r for r in records if parse_int(r["낙찰가(만원)"]) > 0]
    archived_sold = [r for r in archive if parse_int(r["낙찰가(만원)"]) > 0]
    print(f"\n저장 완료: {len(records)}건 -> {actual_output}")
    print(f"낙찰가 보유: {len(sold)}건")
    print(f"누적 아카이브: {len(archive)}건 -> {actual_archive}")
    print(f"누적 낙찰가 보유: {len(archived_sold)}건")


if __name__ == "__main__":
    main()
