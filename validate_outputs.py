"""
CSV 산출물 기본 품질 점검.

사용법:
    py validate_outputs.py
"""

import csv
from pathlib import Path


def load_csv(path):
    p = Path(path)
    if not p.exists():
        print(f"[누락] {path}")
        return []
    with p.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def parse_int(value):
    try:
        return int(str(value).replace(",", "").strip())
    except Exception:
        return None


def has_deal_date(row):
    if row.get("계약년월일"):
        return True
    return bool(row.get("계약년도") and row.get("계약월") and row.get("계약일"))


def check_molit(rows):
    if not rows:
        return
    missing_date = [r for r in rows if not has_deal_date(r)]
    bad_price = [r for r in rows if parse_int(r.get("거래금액(만원)")) is None]
    print(f"[molit_all.csv] {len(rows)}행")
    print(f"  계약일자 조립 불가: {len(missing_date)}행")
    print(f"  거래금액 변환 실패: {len(bad_price)}행")


def check_court(rows, label):
    if not rows:
        return
    sold = [r for r in rows if parse_int(r.get("낙찰가(만원)")) and parse_int(r.get("낙찰가(만원)")) > 0]
    cases = {r.get("사건번호") for r in rows if r.get("사건번호")}
    statuses = {}
    for r in rows:
        status = r.get("경매상태") or "(미상)"
        statuses[status] = statuses.get(status, 0) + 1
    print(f"[{label}] {len(rows)}행 / 사건 {len(cases)}건")
    print(f"  낙찰가 보유: {len(sold)}행")
    print(f"  상태 분포: {statuses}")


def check_combined(rows):
    if not rows:
        return
    est = [r for r in rows if parse_int(r.get("추정시세(만원)"))]
    bad_unit = [
        r for r in rows
        if parse_int(r.get("감정가(만원)")) and parse_int(r.get("감정가(만원)")) > 1_000_000
    ]
    conf = {}
    for r in est:
        conf[r.get("시세신뢰도") or "(없음)"] = conf.get(r.get("시세신뢰도") or "(없음)", 0) + 1
    outliers = [r for r in est if r.get("낙찰가/시세(%)") and not 20 <= float(r["낙찰가/시세(%)"]) <= 200]
    print(f"[combined_result.csv] {len(rows)}행 (물건당 1행)")
    print(f"  시세 산출: {len(est)}행 ({len(est) / len(rows) * 100:.1f}%) / 신뢰도 {conf}")
    print(f"  낙찰가/시세 20~200% 밖: {len(outliers)}행")
    print(f"  감정가 단위 의심: {len(bad_unit)}행")


def check_upcoming(rows):
    if not rows:
        return
    judge = {}
    for r in rows:
        judge[r.get("판단") or "(일반)"] = judge.get(r.get("판단") or "(일반)", 0) + 1
    print(f"[upcoming_bids.csv] {len(rows)}행 / 판단 {judge}")


def main():
    check_court(load_csv("court_auction_archive.csv"), "court_auction_archive.csv")
    check_court(load_csv("court_auction_expanded.csv"), "court_auction_expanded.csv")
    check_molit(load_csv("molit_all.csv"))
    check_combined(load_csv("combined_result.csv"))
    check_upcoming(load_csv("upcoming_bids.csv"))


if __name__ == "__main__":
    main()
