"""
대법원 경매정보 낙찰결과 크롤러 - 빌라(다세대·연립)
서울남부지방법원 / 결과: villa_auction.csv

사용법:
    py villa_court_crawler.py               # 전체
    py villa_court_crawler.py 20260101      # 특정일 이후
    py villa_court_crawler.py 20260101 20261231
"""

import sys, time, re, requests, csv

COURT_CODE = "B000212"
DELAY_SEC  = 1.0
PAGE_SIZE  = 20
OUTPUT_CSV = "villa_auction.csv"

BASE_URL   = "https://www.courtauction.go.kr"
SEARCH_URL = f"{BASE_URL}/pgj/pgjsearch/selectDspslSchdRsltSrch.on"

HEADERS = {
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36",
    "Content-Type":     "application/json; charset=UTF-8",
    "Referer":          f"{BASE_URL}/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ158M00.xml",
    "Origin":           BASE_URL,
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

VILLA_KEYWORDS = ["다세대", "연립", "빌라"]


def parse_args():
    args = sys.argv[1:]
    return (args[0] if len(args) >= 1 else None,
            args[1] if len(args) >= 2 else None)


def in_date_range(mae_giil, start, end):
    if not mae_giil or len(mae_giil) < 8:
        return True
    d = mae_giil[:8]
    if start and d < start: return False
    if end   and d > end:   return False
    return True


def parse_int(val):
    try:
        return int(re.sub(r"[^\d]", "", str(val)) or "0")
    except Exception:
        return 0


def is_villa(row):
    usage = row.get("dspslUsgNm", "") or ""
    bigo  = row.get("mulBigo", "") or ""
    return any(kw in usage or kw in bigo for kw in VILLA_KEYWORDS)


def build_payload(page_no, total_yn="N"):
    return {
        "dma_pageInfo": {
            "pageNo": str(page_no), "pageSize": str(PAGE_SIZE),
            "bfPageNo": str(page_no - 1), "startRowNo": "",
            "totalCnt": "", "totalYn": total_yn, "groupTotalCount": ""
        },
        "dma_srchGdsDtlSrchInfo": {
            "statNum": "3", "pgmId": "PGJ158M01", "cortStDvs": "1",
            "cortOfcCd": COURT_CODE, "jdbnCd": "", "csNo": "",
            "rprsAdongSdCd": "", "rprsAdongSggCd": "", "rprsAdongEmdCd": "",
            "rdnmSdCd": "", "rdnmSggCd": "", "rdnmNo": "",
            "auctnGdsStatCd": "02",
            "lclDspslGdsLstUsgCd": "", "mclDspslGdsLstUsgCd": "",
            "sclDspslGdsLstUsgCd": "", "dspslAmtMin": "", "dspslAmtMax": "",
            "aeeEvlAmtMin": "", "aeeEvlAmtMax": "",
            "flbdNcntMin": "", "flbdNcntMax": "", "lafjOrderBy": ""
        }
    }


def make_record(row):
    mae_giil = row.get("maeGiil", "")
    if len(mae_giil) == 8:
        mae_giil = f"{mae_giil[:4]}-{mae_giil[4:6]}-{mae_giil[6:]}"
    감정가 = parse_int(row.get("gamevalAmt", 0)) // 10000
    낙찰가 = parse_int(row.get("maeAmt", 0)) // 10000
    return {
        "매각기일":        mae_giil,
        "사건번호":        row.get("srnSaNo", ""),
        "물건번호":        row.get("maemulSer", ""),
        "소재지":          row.get("printSt", "") or row.get("convAddr", ""),
        "용도":            row.get("dspslUsgNm", ""),
        "감정가(만원)":    감정가,
        "최저입찰가(만원)": parse_int(row.get("minmaePrice", 0)) // 10000,
        "낙찰가(만원)":    낙찰가,
        "낙찰가율(%)":     round(낙찰가 / 감정가 * 100, 1) if 감정가 > 0 else "",
        "유찰횟수":        parse_int(row.get("yuchalCnt", 0)),
        "법원":            "서울남부지방법원",
    }


def run():
    start_date, end_date = parse_args()
    label = f"{start_date or '전체'} ~ {end_date or '전체'}"
    print(f"[서울남부지방법원] 빌라 낙찰결과 조회")
    print(f"기간: {label}\n")

    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        sess.get(f"{BASE_URL}/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ158M00.xml", timeout=10)
    except Exception:
        pass

    payload = build_payload(1, "Y")
    resp = sess.post(SEARCH_URL, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()["data"]
    total_cnt   = int(data["dma_pageInfo"].get("totalCnt", 0))
    total_pages = (total_cnt + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"  낙찰 전체: {total_cnt}건 / {total_pages}페이지")

    all_records  = []
    villa_records = []
    usage_cnt = {}

    for page in range(1, total_pages + 1):
        if page > 1:
            payload = build_payload(page, "N")
            time.sleep(DELAY_SEC)
            resp = sess.post(SEARCH_URL, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()["data"]

        results = data.get("dlt_srchResult", [])
        if not results:
            break

        page_villa = 0
        for row in results:
            if not in_date_range(row.get("maeGiil", ""), start_date, end_date):
                continue
            rec = make_record(row)
            all_records.append(rec)
            k = row.get("dspslUsgNm", "(미분류)")
            usage_cnt[k] = usage_cnt.get(k, 0) + 1
            if is_villa(row):
                villa_records.append(rec)
                page_villa += 1

        print(f"  p{page}/{total_pages}: {len(results)}건 수신, 빌라 {page_villa}건")

    print(f"\n용도별 분포:")
    for k, v in sorted(usage_cnt.items(), key=lambda x: -x[1]):
        mark = " <-- 해당" if any(kw in k for kw in VILLA_KEYWORDS) else ""
        print(f"  {k}: {v}건{mark}")

    if not villa_records:
        print("\n빌라 낙찰 건이 없습니다.")
        return

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(villa_records[0].keys()))
        writer.writeheader()
        writer.writerows(villa_records)
    print(f"\n빌라 낙찰 {len(villa_records)}건 -> {OUTPUT_CSV}")


if __name__ == "__main__":
    run()
