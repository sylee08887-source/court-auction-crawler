"""
진행 중 경매 입찰 도우미.

- 입찰 예정 물건(매각기일이 오늘 이후, 아직 낙찰가 없음)을 경매 아카이브에서 뽑는다
- rt_match로 추정시세를 붙인다
- 예상 낙찰가 = 최저가 × 과거 '낙찰가/최저가' 분포(하위 25% / 중위 / 상위 25%), 구·유찰구간별
  (과거 데이터상 낙찰가는 유찰 횟수와 무관하게 최저가의 105~120%로 안정적이고,
   낙찰가/시세는 유찰 횟수와 권리관계에 따라 크게 흔들린다)
- 추정시세는 '그 가격이 시세의 몇 %인가' 판단에 쓴다
- 유찰이 많거나 최저가가 시세보다 지나치게 낮으면 권리 인수(선순위 임차인 등) 위험으로 표시

사용법:
    py rt_bid.py            # upcoming_bids.csv 저장 + 상위 후보 출력
"""

import csv
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from rt_match import MultiIndex, match_auction

ARCHIVE_CSV = "court_auction_archive.csv"
HISTORY_CSV = "combined_result.csv"
OUTPUT_CSV = "upcoming_bids.csv"
MIN_SAMPLES = 8
DEPOSIT_RATE = 0.10  # 기본 입찰보증금 (재매각 등 특별매각조건은 20%일 수 있음)
RATIO_COL = "낙찰가/최저가(%)"
RISK_YUCHAL = 5          # 유찰 이 횟수 이상이면 권리관계 경고
RISK_MIN_TO_MARKET = 30  # 최저가/시세(%)가 이보다 낮으면 권리관계 경고
CHEAP_MID_TO_MARKET = 85 # 예상 중위 낙찰가/시세(%)가 이 이하면 '시세 대비 저가 예상'

STATUS_PRIORITY = {"01": 3, "03": 2}  # 매각공고 > 유찰


def yuchal_bucket(n):
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        return "?"
    return f"{n}회" if n <= 4 else "5회+"


def load_upcoming(path=ARCHIVE_CSV, today=None):
    today = (today or date.today()).isoformat()
    best = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("매각기일", "") < today:
                continue
            if (row.get("낙찰가(만원)") or "0") not in ("", "0"):
                continue
            key = (row["사건번호"], row["물건번호"])
            rank = (row["매각기일"], STATUS_PRIORITY.get(row.get("경매상태코드", ""), 0),
                    bool(row.get("지번")), row.get("수집일시", ""))
            if key not in best or rank > best[key][0]:
                best[key] = (rank, row)
    return [r for _, r in sorted(best.values(), key=lambda x: (x[1]["매각기일"], x[1]["사건번호"]))]


class History:
    """과거 낙찰 결과의 비율 분포. ratio_col: '낙찰가/시세(%)' 또는 '낙찰가율(%)'."""

    def __init__(self, path=HISTORY_CSV):
        if Path(path).exists():
            df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
        else:
            df = pd.DataFrame(columns=["구", "유찰횟수", "낙찰가/시세(%)", "낙찰가율(%)",
                                       "낙찰가(만원)", "최저입찰가(만원)"])
        for c in ["낙찰가/시세(%)", "낙찰가율(%)", "낙찰가(만원)", "최저입찰가(만원)"]:
            df[c] = pd.to_numeric(df.get(c), errors="coerce")
        df[RATIO_COL] = (df["낙찰가(만원)"] / df["최저입찰가(만원)"] * 100).round(1)
        # 극단값(최저가 대비 3배 초과 등 특수 사례) 제거
        df.loc[~df[RATIO_COL].between(100, 300), RATIO_COL] = None
        df["유찰구간"] = df["유찰횟수"].map(yuchal_bucket)
        self.df = df

    def samples(self, ratio_col, gu, bucket):
        """(값 Series, 기준 설명) — 표본이 부족하면 구 → 유찰구간 → 전체 순으로 넓힌다."""
        d = self.df.dropna(subset=[ratio_col])
        for mask, label in (
            ((d["구"] == gu) & (d["유찰구간"] == bucket), f"{gu}·유찰{bucket}"),
            (d["유찰구간"] == bucket, f"전체 구·유찰{bucket}"),
            (d["구"] == gu, f"{gu}·전체 유찰"),
            (pd.Series(True, index=d.index), "전체"),
        ):
            s = d.loc[mask, ratio_col]
            if len(s) >= MIN_SAMPLES:
                return s, f"{label} {len(s)}건"
        return d[ratio_col], f"전체 {len(d)}건"

    def quantiles(self, ratio_col, gu, bucket):
        s, label = self.samples(ratio_col, gu, bucket)
        if s.empty:
            return None, label
        return tuple(round(float(s.quantile(q)), 1) for q in (0.25, 0.5, 0.75)), label


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate(upcoming, index, history):
    """입찰 예정 물건별 평가 dict 목록과 {(사건번호, 물건번호): 비교거래} 반환."""
    out, comps = [], {}
    for row in upcoming:
        m, used = match_auction(row, index)
        comps[(row["사건번호"], row["물건번호"])] = used
        appraisal, minimum = _num(row.get("감정가(만원)")), _num(row.get("최저입찰가(만원)"))
        market = _num(m["추정시세(만원)"])
        bucket = yuchal_bucket(row.get("유찰횟수"))
        rec = {
            "매각기일": row["매각기일"], "사건번호": row["사건번호"], "물건번호": row["물건번호"],
            "경매상태": row.get("경매상태", ""), "구": m["구"], "법정동": m["법정동"], "건물명": m["건물명"],
            "층": m["층"], "전용면적(㎡)": m["전용면적(㎡)"], "감정가(만원)": appraisal,
            "최저입찰가(만원)": minimum, "유찰횟수": row.get("유찰횟수", ""),
            "추정시세(만원)": market, "최저가/시세(%)": m["최저가/시세(%)"], "감정가/시세(%)": m["감정가/시세(%)"],
            "시세신뢰도": m["시세신뢰도"], "시세산출": m["시세산출"], "비교기간": m["비교기간"],
            "시점보정(평균)": m["시점보정(평균)"], "최근비교거래일": m["최근비교거래일"],
            "매칭방식": m["매칭방식"], "실거래유형": m["실거래유형"], "매칭건물명": m["매칭건물명"],
            "입찰보증금(만원)": round(minimum * DEPOSIT_RATE) if minimum else None,
            "소재지": row.get("소재지", ""),
        }
        q, basis = history.quantiles(RATIO_COL, m["구"], bucket) if minimum else (None, "")
        risky = (_num(row.get("유찰횟수")) or 0) >= RISK_YUCHAL or (
            market and minimum and minimum / market * 100 < RISK_MIN_TO_MARKET)
        if q:
            lo, mid, hi = (round(minimum * r / 100) for r in q)
            rec.update({"예상낙찰가_하(만원)": lo, "예상낙찰가_중(만원)": mid, "예상낙찰가_상(만원)": hi,
                        "예상중위/시세(%)": round(mid / market * 100, 1) if market else None,
                        "예상기준": f"최저가 × 과거 {basis} (낙찰가/최저가 {q[0]}~{q[2]}%)"})
        else:
            rec.update({"예상낙찰가_하(만원)": None, "예상낙찰가_중(만원)": None, "예상낙찰가_상(만원)": None,
                        "예상중위/시세(%)": None, "예상기준": ""})
        mid_ratio = rec["예상중위/시세(%)"]
        if risky:
            rec["판단"] = "⚠️ 권리관계 확인 (인수 위험)"
        elif market is None:
            rec["판단"] = "시세 없음"
        elif minimum and minimum >= market:
            rec["판단"] = "최저가 ≥ 시세 (유찰 예상)"
        elif mid_ratio is not None and mid_ratio <= CHEAP_MID_TO_MARKET:
            rec["판단"] = "시세 대비 저가 예상" if m["시세신뢰도"] != "하" else "저가 예상(시세 신뢰도 낮음)"
        else:
            rec["판단"] = ""
        out.append(rec)
    return out, comps


def win_share(history, gu, bucket, bid_to_minimum):
    """과거 같은 조건 낙찰 사례 중 낙찰가/최저가가 bid_to_minimum(%) 이하였던 비중(%).
    = '이 금액이었다면 과거 낙찰가 이상이었을 비율' (낙찰 가능성의 거친 근사)"""
    s, label = history.samples(RATIO_COL, gu, bucket)
    if s.empty:
        return None, label
    return round(float((s <= bid_to_minimum).mean() * 100), 1), label


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    upcoming = load_upcoming()
    index = MultiIndex.from_db()
    rows, _ = evaluate(upcoming, index, History())
    df = pd.DataFrame(rows)
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    has = df["추정시세(만원)"].notna()
    print(f"입찰 예정 {len(df)}건 (시세 산출 {has.sum()}건) -> {OUTPUT_CSV}")
    print("판단: " + ", ".join(f"{k or '(없음)'} {v}" for k, v in df["판단"].value_counts().items()))
    top = df[df["판단"] == "시세 대비 저가 예상"].sort_values("예상중위/시세(%)").head(15)
    print("\n[시세 대비 저가 예상 상위]")
    for _, r in top.iterrows():
        print(f"  {r['매각기일']} {r['사건번호']}({r['물건번호']}) {r['구']} {r['건물명'] or ''} {r['전용면적(㎡)']}㎡ "
              f"유찰{r['유찰횟수']} 최저 {r['최저입찰가(만원)']:,.0f} / 예상 {r['예상낙찰가_중(만원)']:,.0f} "
              f"/ 시세 {r['추정시세(만원)']:,.0f} ({r['예상중위/시세(%)']}%) [신뢰도 {r['시세신뢰도']}, {r['비교기간']}]")


if __name__ == "__main__":
    main()
