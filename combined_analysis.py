"""
법원경매 낙찰가 vs 국토부 실거래가 비교 분석
────────────────────────────────────────────
1. 법원경매 사이트에서 서울남부지방법원 오피스텔 낙찰 결과 수집
2. 낙찰 물건의 주소(지번)로 국토부 실거래가 DB에서 동일 단지 거래 조회
3. 낙찰가 vs 시세 비교 CSV 출력

사용법:
    py combined_analysis.py                  # 최근 낙찰 전체
    py combined_analysis.py 20260101         # 특정일 이후 낙찰만
    py combined_analysis.py 20260101 20261231
"""

import sys
import re
import time
import requests
import csv
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ── 설정 ──────────────────────────────────────────────
COURT_CODE   = "B000212"
from config import SERVICE_KEY
MOLIT_URL    = "https://apis.data.go.kr/1613000/RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade"
COURT_URL    = "https://www.courtauction.go.kr/pgj/pgjsearch/selectDspslSchdRsltSrch.on"
BASE_URL     = "https://www.courtauction.go.kr"
PAGE_SIZE    = 20
DELAY        = 0.8
OUTPUT_CSV   = "combined_result.csv"
# 낙찰일 기준 앞뒤 몇 개월의 실거래가를 시세로 볼 것인지
MONTHS_BEFORE = 6
MONTHS_AFTER  = 3
# ──────────────────────────────────────────────────────

DISTRICTS = {
    "강서구": "11500",
    "양천구": "11470",
    "구로구": "11530",
    "금천구": "11545",
    "영등포구": "11560",
    "동작구": "11590",
    "관악구": "11620",
}

COURT_HEADERS = {
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36",
    "Content-Type":     "application/json; charset=UTF-8",
    "Referer":          f"{BASE_URL}/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ158M00.xml",
    "Origin":           BASE_URL,
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


# ══════════════════════════════════════════
# 1단계: 법원경매 낙찰 데이터 수집
# ══════════════════════════════════════════

def parse_args():
    args = sys.argv[1:]
    return (args[0] if len(args) >= 1 else None,
            args[1] if len(args) >= 2 else None)


def in_date_range(mae_giil, start, end):
    if not mae_giil or len(mae_giil) < 8:
        return True
    d = mae_giil[:8]
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def parse_int(val):
    try:
        return int(re.sub(r"[^\d]", "", str(val)) or "0")
    except Exception:
        return 0


def build_court_payload(page_no, total_yn="N"):
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


def parse_address(addr):
    """'서울 강서구 마곡동 795-2' → (구, 동, 지번)"""
    addr = re.sub(r"서울특별시|서울시|서울\s*", "", addr).strip()
    m = re.search(r"(\S+구)\s+(\S+동)\s+([\d\-]+)", addr)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None, None, None


def collect_court_auctions(start_date, end_date):
    print("── 1단계: 법원경매 낙찰 데이터 수집 ──")
    sess = requests.Session()
    sess.headers.update(COURT_HEADERS)
    try:
        sess.get(f"{BASE_URL}/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ158M00.xml", timeout=10)
    except Exception:
        pass

    payload = build_court_payload(1, "Y")
    resp = sess.post(COURT_URL, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()["data"]
    total_cnt = int(data["dma_pageInfo"].get("totalCnt", 0))
    total_pages = (total_cnt + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"  낙찰 전체: {total_cnt}건 / {total_pages}페이지")

    auctions = []
    for page in range(1, total_pages + 1):
        if page > 1:
            payload = build_court_payload(page, "N")
            time.sleep(DELAY)
            resp = sess.post(COURT_URL, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()["data"]

        results = data.get("dlt_srchResult", [])
        for row in results:
            mae_giil_raw = row.get("maeGiil", "")
            if not in_date_range(mae_giil_raw, start_date, end_date):
                continue
            usage = row.get("dspslUsgNm", "") or ""
            bigo  = row.get("mulBigo", "") or ""
            if "오피스텔" not in usage and "오피스텔" not in bigo:
                continue

            addr = row.get("printSt", "") or row.get("convAddr", "")
            gu, dong, jibun = parse_address(addr)
            감정가 = parse_int(row.get("gamevalAmt", 0)) // 10000  # 원 → 만원
            낙찰가 = parse_int(row.get("maeAmt", 0)) // 10000        # 원 → 만원

            # 매각기일 포맷: yyyyMMdd → datetime
            try:
                mae_dt = datetime.strptime(mae_giil_raw[:8], "%Y%m%d")
            except Exception:
                mae_dt = None

            auctions.append({
                "매각기일":      mae_giil_raw[:4] + "-" + mae_giil_raw[4:6] + "-" + mae_giil_raw[6:8] if len(mae_giil_raw) >= 8 else mae_giil_raw,
                "매각기일_dt":   mae_dt,
                "사건번호":      row.get("srnSaNo", ""),
                "소재지":        addr,
                "구":            gu,
                "동":            dong,
                "지번":          jibun,
                "용도":          usage,
                "감정가(만원)":  감정가,
                "최저입찰가(만원)": parse_int(row.get("minmaePrice", 0)) // 10000,
                "낙찰가(만원)":  낙찰가,
                "낙찰가율(%)":   round(낙찰가 / 감정가 * 100, 1) if 감정가 > 0 else "",
                "유찰횟수":      parse_int(row.get("yuchalCnt", 0)),
            })

    print(f"  오피스텔 낙찰: {len(auctions)}건\n")
    return auctions


# ══════════════════════════════════════════
# 2단계: 국토부 실거래가 조회
# ══════════════════════════════════════════

def month_range(dt, before, after):
    """기준일로부터 before개월 전 ~ after개월 후 YYYYMM 목록"""
    months = []
    start = dt - timedelta(days=before * 30)
    end   = dt + timedelta(days=after * 30)
    cur = datetime(start.year, start.month, 1)
    while cur <= end:
        months.append(f"{cur.year}{cur.month:02d}")
        if cur.month == 12:
            cur = datetime(cur.year + 1, 1, 1)
        else:
            cur = datetime(cur.year, cur.month + 1, 1)
    return months


def fetch_molit(lawd_cd, deal_ymd):
    params = {
        "serviceKey": SERVICE_KEY,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ymd,
        "numOfRows": 1000,
        "pageNo": 1,
    }
    r = requests.get(MOLIT_URL, params=params, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    items = []
    for item in root.findall(".//item"):
        items.append({
            "오피스텔명":     item.findtext("offiNm", "").strip(),
            "읍면동":         item.findtext("umdNm", "").strip(),
            "지번":           item.findtext("jibun", "").strip(),
            "전용면적(㎡)":   item.findtext("excluUseAr", "").strip(),
            "계약년월일":     f"{item.findtext('dealYear','')}-{item.findtext('dealMonth','').zfill(2)}-{item.findtext('dealDay','').zfill(2)}",
            "거래금액(만원)": item.findtext("dealAmount", "").strip().replace(",", ""),
            "층":             item.findtext("floor", "").strip(),
            "해제여부":       item.findtext("cdealType", "").strip(),
        })
    return items


def load_molit_csv(csv_path="molit_all.csv"):
    """로컬 국토부 CSV 로드 → {(구, 지번): [rows]} 인덱스 생성"""
    import os
    if not os.path.exists(csv_path):
        return None
    db = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (row.get("구", "").strip(), row.get("지번", "").strip())
            db.setdefault(key, []).append(row)
    print(f"  로컬 캐시 로드: {csv_path} ({sum(len(v) for v in db.values())}건)")
    return db


def collect_market_prices(auctions, molit_db=None):
    """낙찰 물건별 주변 실거래가 수집 (구+지번 매칭)"""
    print("── 2단계: 국토부 실거래가 조회 ──")

    # 로컬 DB 없으면 API 실시간 조회
    use_local = molit_db is not None
    api_cache = {}

    results = []
    for auction in auctions:
        gu     = auction.get("구")
        dong   = auction.get("동")
        jibun  = auction.get("지번")
        mae_dt = auction.get("매각기일_dt")

        if not gu or not jibun or not mae_dt:
            results.append({**auction, "매칭_오피스텔명": "", "매칭_계약년월일": "",
                             "매칭_거래금액(만원)": "", "매칭_전용면적(㎡)": "",
                             "매칭_층": "", "매칭수": 0, "시세평균(만원)": "",
                             "낙찰가/시세(%)": ""})
            continue

        if use_local:
            # 로컬 CSV에서 지번 매칭
            all_items_raw = molit_db.get((gu, jibun), [])
            # 날짜 필터: 매각기일 기준 전후 기간
            start_dt = mae_dt - timedelta(days=MONTHS_BEFORE * 30)
            end_dt   = mae_dt + timedelta(days=MONTHS_AFTER * 30)
            all_items = []
            for row in all_items_raw:
                try:
                    deal_dt = datetime.strptime(row["계약년월일"], "%Y-%m-%d")
                    if start_dt <= deal_dt <= end_dt:
                        all_items.append({
                            "오피스텔명":     row.get("오피스텔명", ""),
                            "계약년월일":     row.get("계약년월일", ""),
                            "거래금액(만원)": row.get("거래금액(만원)", ""),
                            "전용면적(㎡)":   row.get("전용면적(㎡)", ""),
                            "층":             row.get("층", ""),
                            "해제여부":       row.get("해제여부", ""),
                        })
                except Exception:
                    continue
            all_items = [it for it in all_items if not it["해제여부"]]
        else:
            lawd_cd = DISTRICTS.get(gu)
            if not lawd_cd:
                results.append({**auction, "매칭_오피스텔명": "(관할외)", "매칭_계약년월일": "",
                                 "매칭_거래금액(만원)": "", "매칭_전용면적(㎡)": "",
                                 "매칭_층": "", "매칭수": 0, "시세평균(만원)": "",
                                 "낙찰가/시세(%)": ""})
                continue
            months = month_range(mae_dt, MONTHS_BEFORE, MONTHS_AFTER)
            all_items = []
            for ym in months:
                key = (lawd_cd, ym)
                if key not in api_cache:
                    try:
                        api_cache[key] = fetch_molit(lawd_cd, ym)
                        time.sleep(0.2)
                    except Exception as e:
                        print(f"  [오류] {gu} {ym}: {e}")
                        api_cache[key] = []
                all_items.extend([it for it in api_cache[key]
                                  if it["지번"] == jibun and not it["해제여부"]])

        matched = all_items
        if matched:
            prices = []
            for it in matched:
                try:
                    prices.append(int(it["거래금액(만원)"].replace(",", "")))
                except Exception:
                    pass
            avg_price = round(sum(prices) / len(prices)) if prices else ""
            낙찰가 = auction.get("낙찰가(만원)", 0)
            비율 = round(낙찰가 / avg_price * 100, 1) if avg_price and 낙찰가 else ""

            for it in matched:
                results.append({
                    **auction,
                    "매칭_오피스텔명":     it["오피스텔명"],
                    "매칭_계약년월일":     it["계약년월일"],
                    "매칭_거래금액(만원)": it["거래금액(만원)"],
                    "매칭_전용면적(㎡)":   it["전용면적(㎡)"],
                    "매칭_층":            it["층"],
                    "매칭수":             len(matched),
                    "시세평균(만원)":      avg_price,
                    "낙찰가/시세(%)":     비율,
                })
        else:
            results.append({
                **auction,
                "매칭_오피스텔명": "(미매칭)", "매칭_계약년월일": "",
                "매칭_거래금액(만원)": "", "매칭_전용면적(㎡)": "",
                "매칭_층": "", "매칭수": 0, "시세평균(만원)": "",
                "낙찰가/시세(%)": ""
            })
            print(f"  [미매칭] {auction['소재지']} ({dong} {jibun})")

    src = "로컬 CSV" if use_local else f"API 캐시 {len(api_cache)}건"
    print(f"\n  조회 완료 ({src})\n")
    return results


# ══════════════════════════════════════════
# 3단계: CSV 저장
# ══════════════════════════════════════════

FIELDNAMES = [
    "매각기일", "사건번호", "소재지", "용도",
    "감정가(만원)", "최저입찰가(만원)", "낙찰가(만원)", "낙찰가율(%)", "유찰횟수",
    "매칭_오피스텔명", "매칭_계약년월일", "매칭_거래금액(만원)", "매칭_전용면적(㎡)", "매칭_층",
    "매칭수", "시세평균(만원)", "낙찰가/시세(%)",
]


def save_csv(records):
    # 내부용 필드 제거
    clean = []
    for r in records:
        clean.append({k: v for k, v in r.items() if k in FIELDNAMES})

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(clean)
    print(f"── 저장 완료: {len(clean)}행 → {OUTPUT_CSV}")


def main():
    start_date, end_date = parse_args()
    label = f"{start_date or '전체'} ~ {end_date or '전체'}"
    print(f"=== 법원경매 낙찰가 vs 국토부 실거래가 비교 ===")
    print(f"기간: {label}\n")

    auctions = collect_court_auctions(start_date, end_date)
    if not auctions:
        print("오피스텔 낙찰 건이 없습니다.")
        return

    molit_db = load_molit_csv("molit_all.csv")
    results = collect_market_prices(auctions, molit_db)
    save_csv(results)

    # 요약 출력
    matched = [r for r in results if r.get("매칭수", 0) > 0]
    print(f"\n=== 요약 ===")
    print(f"  낙찰 건수:      {len(auctions)}건")
    print(f"  시세 매칭 성공: {len(set(r['사건번호'] for r in matched))}건")
    seen = set()
    for r in results:
        key = r['사건번호']
        if r.get("낙찰가/시세(%)") and key not in seen:
            seen.add(key)
            print(f"  [{key}] {r['소재지']}")
            print(f"    낙찰가 {r['낙찰가(만원)']:,}만원 / 시세평균 {r['시세평균(만원)']:,}만원 → {r['낙찰가/시세(%)']}%")


if __name__ == "__main__":
    main()
