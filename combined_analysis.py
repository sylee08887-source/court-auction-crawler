"""
법원경매 낙찰가 vs 국토부 실거래가 비교 분석
────────────────────────────────────────────
1. court_auction_archive.csv(court_crawler_expanded.py 누적본)에서 낙찰 물건 로드
2. realestate.db의 오피스텔 매매 실거래와 매칭 (rt_match: 지번 → 동+건물명 → 구+건물명)
3. 같은 면적대 거래로 추정시세를 계산해 낙찰가/최저가/감정가와 비교
4. rt_rent로 매각기일 기준 연 월세회전율(최근 12개월 월세계약 / 전체 호수)을 붙인다

출력:
    combined_result.csv   경매 물건당 1행 (추정시세, 낙찰가/시세 등)
    combined_matches.csv  시세 산출에 쓴 실거래 상세

사용법:
    py combined_analysis.py                  # 전체
    py combined_analysis.py 20260101         # 특정일 이후 매각기일만
    py combined_analysis.py 20260101 20261231
"""

import csv
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median

import rt_rent
from rt_match import MarketIndex, MultiIndex, match_auction

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

COURT_ARCHIVE_CSV = "court_auction_archive.csv"
COURT_CACHE_CSV = "court_auction_expanded.csv"
LEGACY_MOLIT_CSV = "molit_all.csv"
OUTPUT_CSV = "combined_result.csv"
MATCHES_CSV = "combined_matches.csv"

COURT_STATUS_PRIORITY = {
    "06": 5,  # 대금납부
    "04": 4,  # 매각허가결정
    "02": 3,  # 매각
    "": 2,
}

AUCTION_FIELDS = [
    "매각기일", "사건번호", "물건번호", "경매상태", "소재지", "용도",
    "감정가(만원)", "최저입찰가(만원)", "낙찰가(만원)", "낙찰가율(%)", "유찰횟수",
]
MATCH_FIELDS = [
    "구", "법정동", "지번", "건물명", "층", "전용면적(㎡)",
    "매칭방식", "실거래유형", "매칭건물명", "매칭지번", "건물명유사도", "후보거래수",
    "비교거래수", "비교기간", "시세산출", "시세신뢰도", "시점보정(평균)", "추정시세(만원)", "최근비교거래일",
    "낙찰가/시세(%)", "최저가/시세(%)", "감정가/시세(%)", "매칭비고",
]
DETAIL_FIELDS = ["사건번호", "물건번호", "매각기일", "매칭_건물명", "매칭_법정동", "매칭_지번",
                 "매칭_계약일", "매칭_거래금액(만원)", "매칭_전용면적(㎡)", "매칭_층", "매칭_㎡당가(만원)", "매칭_시점보정"]


def parse_int(val):
    try:
        return int(re.sub(r"[^\d]", "", str(val)) or "0")
    except Exception:
        return 0


def load_auctions(start_date, end_date):
    path = next((p for p in (COURT_ARCHIVE_CSV, COURT_CACHE_CSV) if Path(p).exists()), None)
    if not path:
        print("경매 CSV가 없습니다. 먼저 py court_crawler_expanded.py 를 실행하세요.")
        sys.exit(1)

    best = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            d = row.get("매각기일", "").replace("-", "")
            if (start_date and d < start_date) or (end_date and d > end_date):
                continue
            if parse_int(row.get("낙찰가(만원)")) <= 0:
                continue
            key = (row["사건번호"], row["물건번호"], row["소재지"], row["매각기일"])
            old = best.get(key)
            pri = COURT_STATUS_PRIORITY.get(row.get("경매상태코드", ""), 0)
            # 상태 우선순위가 같으면 구조화 필드(지번)가 있는 행을 쓴다
            if old is None or (pri, bool(row.get("지번"))) > (
                    COURT_STATUS_PRIORITY.get(old.get("경매상태코드", ""), 0), bool(old.get("지번"))):
                best[key] = row
    rows = sorted(best.values(), key=lambda r: (r["매각기일"], r["사건번호"], r["물건번호"]))
    print(f"  경매 로드: {path} (낙찰 물건 {len(rows)}건)")
    return rows


def load_market():
    index = MultiIndex.from_db()
    if index.size:
        parts = ", ".join(f"{label} {idx.size:,}건" for label, idx in index.parts)
        print(f"  실거래 로드: realestate.db 매매 {parts} (해제 제외, 오피스텔 우선 매칭)")
        return index
    if Path(LEGACY_MOLIT_CSV).exists():
        index = MarketIndex.from_legacy_csv(LEGACY_MOLIT_CSV)
        print(f"  실거래 로드: {LEGACY_MOLIT_CSV} {index.size:,}건 (DB가 비어 있어 CSV 사용)")
        return index
    print("실거래 데이터가 없습니다. 먼저 py collect.py -t offi_trade 로 수집하세요.")
    sys.exit(1)


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    args = sys.argv[1:]
    start_date = args[0] if len(args) >= 1 else None
    end_date = args[1] if len(args) >= 2 else None
    print("=== 법원경매 낙찰가 vs 국토부 실거래가 비교 ===")
    print(f"기간: {start_date or '전체'} ~ {end_date or '전체'}\n")

    auctions = load_auctions(start_date, end_date)
    index = load_market()

    rent = rt_rent.RentIndex.from_db()
    print(f"  전월세 로드: {rent.size:,}건 (" + ", ".join(f"{p} {i.size:,}" for p, i in rent.parts.items()) + ")")

    def sale_date(row):
        try:
            return datetime.strptime(row.get("매각기일", "")[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    n = rt_rent.prefetch(auctions, rent, sale_date)  # 건축물대장 병렬 선조회 (캐시돼 있으면 건너뜀)
    print(f"  건축물대장: 새로 조회 {n}곳")
    rent_conn = rt_rent.connect()

    results, details = [], []
    for row in auctions:
        m, used = match_auction(row, index)
        try:
            base = datetime.strptime(row.get("매각기일", "")[:10], "%Y-%m-%d").date()
            turn = rt_rent.calc_rent_turnover(row, rent, base, conn=rent_conn)
        except ValueError:
            turn = {**dict.fromkeys(rt_rent.COLUMNS, ""), "회전율비고": "매각기일 없음"}
        results.append({**{k: row.get(k, "") for k in AUCTION_FIELDS}, **m, **turn})
        for d in sorted(used, key=lambda d: d["deal_date"], reverse=True):
            details.append({
                "사건번호": row["사건번호"], "물건번호": row["물건번호"], "매각기일": row["매각기일"],
                "매칭_건물명": d["name"], "매칭_법정동": d["umd"], "매칭_지번": d["jibun"],
                "매칭_계약일": d["deal_date"], "매칭_거래금액(만원)": d["price"],
                "매칭_전용면적(㎡)": d["area"], "매칭_층": d.get("floor", ""),
                "매칭_㎡당가(만원)": round(d["price"] / d["area"], 1) if d["area"] else "",
                "매칭_시점보정": d.get("adj", ""),
            })

    rent_conn.close()
    write_csv(OUTPUT_CSV, AUCTION_FIELDS + MATCH_FIELDS + rt_rent.COLUMNS, results)
    write_csv(MATCHES_CSV, DETAIL_FIELDS, details)

    matched = [r for r in results if r["추정시세(만원)"] != ""]
    print(f"\n=== 요약 ===")
    print(f"  낙찰 물건:   {len(results):,}건")
    print(f"  시세 산출:   {len(matched):,}건 ({len(matched) / max(len(results), 1) * 100:.1f}%)")
    print("  매칭방식:   " + ", ".join(f"{k} {v}" for k, v in Counter(r["매칭방식"] for r in results).most_common()))
    print("  실거래유형: " + ", ".join(f"{k} {v}" for k, v in Counter(r["실거래유형"] for r in matched).most_common()))
    print("  시세신뢰도: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(r["시세신뢰도"] for r in matched).items())))
    how = Counter(r["시세산출"].split(" ")[0] for r in matched)
    print("  시세산출:   " + ", ".join(f"{k} {v}" for k, v in how.most_common()))
    fails = Counter(r["매칭비고"] for r in results if r["추정시세(만원)"] == "")
    if fails:
        print("  미산출 사유: " + ", ".join(f"{k} {v}" for k, v in fails.most_common()))
    ratios = [r["낙찰가/시세(%)"] for r in matched if r["낙찰가/시세(%)"] != ""]
    if ratios:
        print(f"  낙찰가/시세 중위: {median(ratios):.1f}%")
    turns = [r["연 월세회전율(%)"] for r in results if r["연 월세회전율(%)"] != ""]
    print(f"  연 월세회전율: {len(turns):,}건 산출" + (f" (중위 {median(turns):.1f}%)" if turns else ""))
    for msg in rt_rent._blocked.values():
        print(f"    [건축물대장] {msg}")
    print(f"\n저장: {OUTPUT_CSV} ({len(results)}행), {MATCHES_CSV} ({len(details)}행)")


if __name__ == "__main__":
    main()
