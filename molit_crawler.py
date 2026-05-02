"""
국토교통부 실거래가 공개시스템 - 오피스텔 매매 데이터 수집기
서울남부지방법원 관할 구역 (강서·양천·구로·금천·영등포·동작·관악)

사용법:
    py molit_crawler.py                         # 기본: 전년도 전체
    py molit_crawler.py 202401 202512           # 기간 지정 (YYYYMM)
    py molit_crawler.py 202401 202512 output.csv  # 출력 파일명 지정
"""

import sys
import requests
import csv
import time
import xml.etree.ElementTree as ET
from datetime import date

from config import SERVICE_KEY
BASE_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade"

# 서울남부지방법원 관할 구역 (법정동코드 앞 5자리)
DISTRICTS = {
    "강서구": "11500",
    "양천구": "11470",
    "구로구": "11530",
    "금천구": "11545",
    "영등포구": "11560",
    "동작구": "11590",
    "관악구": "11620",
}

FIELDNAMES = [
    "구", "읍면동", "지번", "오피스텔명", "전용면적(㎡)",
    "계약년도", "계약월", "계약일", "거래금액(만원)",
    "층", "건축년도", "거래유형", "해제여부", "해제사유발생일",
    "중개사소재지", "매도자구분", "매수자구분",
]


def parse_args():
    args = sys.argv[1:]
    today = date.today()
    prev_year = today.year - 1

    start_ym = args[0] if len(args) >= 1 else f"{prev_year}01"
    end_ym   = args[1] if len(args) >= 2 else f"{prev_year}12"
    out_file = args[2] if len(args) >= 3 else "court_results.csv"

    if len(start_ym) != 6 or len(end_ym) != 6:
        print("오류: 기간은 YYYYMM 형식으로 입력하세요. 예) 202401 202512")
        sys.exit(1)

    months = []
    y, m = int(start_ym[:4]), int(start_ym[4:])
    ey, em = int(end_ym[:4]), int(end_ym[4:])
    while (y, m) <= (ey, em):
        months.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1

    return months, out_file


def fetch_page(lawd_cd, deal_ymd, page_no=1, num_of_rows=1000):
    params = {
        "serviceKey": SERVICE_KEY,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ymd,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
    }
    resp = requests.get(BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_response(xml_text):
    root = ET.fromstring(xml_text)

    result_code = root.findtext(".//resultCode", "")
    result_msg  = root.findtext(".//resultMsg", "")
    if result_code and result_code not in ("00", "000"):
        raise ValueError(f"API 오류 [{result_code}]: {result_msg}")

    total = int(root.findtext(".//totalCount") or 0)

    items = []
    for item in root.findall(".//item"):
        items.append({
            "구":             item.findtext("sggNm", "").strip(),
            "읍면동":         item.findtext("umdNm", "").strip(),
            "지번":           item.findtext("jibun", "").strip(),
            "오피스텔명":     item.findtext("offiNm", "").strip(),
            "전용면적(㎡)":   item.findtext("excluUseAr", "").strip(),
            "계약년도":       item.findtext("dealYear", "").strip(),
            "계약월":         item.findtext("dealMonth", "").strip(),
            "계약일":         item.findtext("dealDay", "").strip(),
            "거래금액(만원)": item.findtext("dealAmount", "").strip().replace(",", ""),
            "층":             item.findtext("floor", "").strip(),
            "건축년도":       item.findtext("buildYear", "").strip(),
            "거래유형":       item.findtext("dealingGbn", "").strip(),
            "해제여부":       item.findtext("cdealType", "").strip(),
            "해제사유발생일": item.findtext("cdealDay", "").strip(),
            "중개사소재지":   item.findtext("estateAgentSggNm", "").strip(),
            "매도자구분":     item.findtext("slerGbn", "").strip(),
            "매수자구분":     item.findtext("buyerGbn", "").strip(),
        })

    return total, items


def fetch_all(lawd_cd, deal_ymd):
    all_items = []
    page = 1
    while True:
        xml = fetch_page(lawd_cd, deal_ymd, page_no=page)
        total, items = parse_response(xml)
        all_items.extend(items)
        if len(all_items) >= total or not items:
            break
        page += 1
        time.sleep(0.3)
    return all_items


def main():
    months, out_file = parse_args()
    start_label = f"{months[0][:4]}-{months[0][4:]}"
    end_label   = f"{months[-1][:4]}-{months[-1][4:]}"

    print(f"국토부 오피스텔 매매 실거래가 수집")
    print(f"대상: 서울남부지방법원 관할 7개 구")
    print(f"기간: {start_label} ~ {end_label}  ({len(months)}개월)\n")

    all_rows = []
    for gu_name, lawd_cd in DISTRICTS.items():
        gu_total = 0
        for deal_ymd in months:
            try:
                items = fetch_all(lawd_cd, deal_ymd)
                gu_total += len(items)
                all_rows.extend(items)
            except Exception as e:
                print(f"  [오류] {gu_name} {deal_ymd}: {e}")
            time.sleep(0.2)
        print(f"  {gu_name}: {gu_total}건")

    # 해제 건 제외 통계
    cancelled = sum(1 for r in all_rows if r["해제여부"])
    valid = len(all_rows) - cancelled

    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\n완료: 총 {len(all_rows)}건 (유효 {valid}건 / 해제 {cancelled}건) → {out_file}")


if __name__ == "__main__":
    main()
