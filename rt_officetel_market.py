"""
서울 오피스텔 시장 패널: 지역 방어력 / 유동성 / 수익성 (종합점수 없음, raw 지표 + 분포 + Pareto 후보만).

분석 단위
  - 단지 = 시군구코드 + 법정동 + 지번 (매매·전월세 모두 지번이 있어 이름 매칭 없이 정확히 묶는다)
  - 단지 × 면적군(10~20 / 20~30 / 30~40 / 40~60 / 60+ ㎡) 행과 단지 전체(area_bin='전체') 행
  - 법정동 → 구 집계는 단지 지표를 stock_units 가중평균 + 단지 중위값으로 (단순평균 금지)

재고(분모)
  - rt_rent의 건축물대장 캐시: 전유부 오피스텔 호수(면적군별까지) → stock_quality
      A 전유부·면적별 / B 전유부·단지전체(면적군 행도 단지 전체 호수로 나눔) / C 표제부 호수(상가 포함 가능) / D 없음
  - py rt_officetel_market.py --build-stock 으로 서울 전 단지를 병렬 선조회 (호출 한도에 걸리면 다음 날 이어서)

유동성 (절대 거래건수는 점수로 쓰지 않고 sample_n으로만 보존)
  - trade_turnover_12m = 최근 12개월 정상 매매(해제·중복 제외) / stock_units × 100, 24·36개월은 연환산
  - active_trade_month_ratio = 최근 12개월 중 거래 있는 달 / 12, max_trade_month_share = 최대 월 거래 / 12개월 거래
  - rent_turnover_12m(전월세 전체), monthly_rent_turnover_12m(월세>0), new_contract_turnover_12m(계약구분 기재 70%↑일 때만)
    → 공실률이 아니라 '임대유동성' 지표
  - 회전율 > 100% (= 재고보다 거래가 많음)는 자동 오류 처리: 값 비우고 error 컬럼에 사유

가격방어 (단지 × 면적군 ㎡당가 중위, 지역 중위가격 변화율 쓰지 않음)
  - 연간 창(기준일부터 12개월 단위) 중위 ㎡당가 → price_cagr_3y/5y, positive_year_ratio
  - 분기 중위(3건 이상) → mdd_3y/5y
  - 분양·입주 초기 대량거래(initial_supply_event) 달은 가격 산정에서 제외
  - mature_price_cagr: 건물연차 ≥ T 거래만. T는 --maturity auto(연차별 가격곡선에서 추정) 또는 숫자

신축 초기 가격조정 (officetel_age_curve.csv)
  - building_age_at_trade = 거래연도 - 건축연도
  - 시장 사이클 제거: 거래 ㎡당가 ÷ 서울 '성숙 재고(연차 5년+)' 분기 ㎡당가 지수
  - 단지 × 면적군마다 연차별 중위를 구해, 같은 그룹의 연차 a-1 → a 변화(로그)의 서울 중위값을 연결(chain)
    → 같은 건물끼리만 비교하므로 단지·면적 구성 차이가 섞이지 않는다
  - 시장지수 대비로는 오래된 건물도 노후화로 매년 조금씩 빠진다(정상 감가, 6~10년차 변화의 중위).
    T = 이후 두 해 연속 연간 변화가 정상 감가 - 1%p보다 덜 빠지는(=신축 프리미엄이 다 빠진) 첫 연차

임대수익률 (시장수익률만, 경매 낙찰가 기준은 아직 섞지 않음)
  - gross_yield_cash  = 중위(월세/㎡) × 12 / 중위(매매 ㎡당가)
  - gross_yield_equiv = 중위((월세 + 보증금 × conversion_rate / 12)/㎡) × 12 / 중위(매매 ㎡당가)

사용법:
    py rt_officetel_market.py --collect --from 201501 --to 202609   # 서울 오피스텔 매매·전월세 수집 (증분)
    py rt_officetel_market.py --build-stock                          # 서울 전 단지 건축물대장 선조회 (오래 걸림, 이어받기)
    py rt_officetel_market.py                                        # 지표 산출 → CSV 5종 + 분포 요약
    py rt_officetel_market.py --maturity 4 --conv-rate 0.055
    py rt_officetel_market.py --verify                               # 10개 단지 수동검증 표
"""

import argparse
import csv
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import median

import rt_core
import rt_rent
from rt_match import norm_jibun, quarter_of
from rt_regions import CODE_TO_SGG, PRESETS, resolve

OUT_COMPLEX = "officetel_complex_metrics.csv"
OUT_DONG = "officetel_dong_metrics.csv"
OUT_GU = "officetel_gu_metrics.csv"
OUT_AGE = "officetel_age_curve.csv"
OUT_DIST = "officetel_distribution.csv"
OUT_PARETO = "pareto_candidates.csv"

CONV_RATE = 0.05            # 보증금 → 월세 환산율 (연)
REPORT_LAG_DAYS = 30        # 기준일: 이 기간이 지난 마지막 월말 (신고 기한 30일)
MIN_YEAR_N = 3              # 연간 가격창 최소 거래수
MIN_QUARTER_N = 3           # 분기 중위 최소 거래수 (MDD)
MIN_YIELD_N = 3             # 수익률 산출 최소 월세·매매 건수
MAX_TURNOVER = 100          # 회전율 상한 (초과면 오류)
CONCENTRATION_SHARE = 0.5   # 12개월 거래 중 한 달 비중이 이 이상이면 flag (거래 4건 이상일 때)
MATURE_INDEX_AGE = 5        # 시장지수는 이 연차 이상 거래로 만든다
AGE_MAX = 15
AGE_MIN_PAIRS = 15          # 연차 변화 추정에 필요한 최소 단지×면적군 수
STABLE_CHANGE = -0.01       # 정상 감가 속도보다 1%p 넘게 더 빠지지 않으면 안정
DEFAULT_MATURITY = 4        # auto 추정 실패 시
SUPPLY_MIN_MONTH = 10       # 초기 대량거래: 한 달 거래 최소 건수
SUPPLY_STOCK_SHARE = 0.1    # … 그리고 재고의 이 비율 이상
SUPPLY_SAME_PRICE = 5       # 같은 달 같은 가격 반복 건수
BULK_SAME_DAY = 5           # 한 단지 같은 날 거래가 이 이상이면 일괄거래 → 가격 산정에서 제외 (회전율에는 포함)
PARETO_MIN_STOCK = {"complex": 50, "dong": 300}

AREA_LABELS = [label for _, label in rt_rent.AREA_BINS]
ALL = "전체"

COMPLEX_FIELDS = [
    "시군구코드", "시군구", "법정동", "단지명", "지번", "건축년도", "연식", "area_bin",
    "stock_units", "stock_quality", "stock_note",
    "trade_n_12m", "trade_n_24m", "trade_n_36m", "trade_turnover_12m", "trade_turnover_24m_ann",
    "trade_turnover_36m_ann", "active_trade_month_ratio", "max_trade_month_share", "trade_concentration_flag",
    "rent_n_12m", "monthly_rent_n_12m", "new_contract_n_12m", "rent_turnover_12m", "monthly_rent_turnover_12m",
    "new_contract_turnover_12m", "active_rent_month_ratio",
    "price_ppa_12m", "price_cagr_3y", "price_cagr_5y", "mature_price_cagr", "mature_years", "mdd_3y", "mdd_5y",
    "positive_year_ratio", "price_confidence",
    "gross_yield", "gross_yield_equiv", "conversion_rate", "rent_ppa_12m",
    "maturity_age", "initial_supply_event", "bulk_trades_12m", "dup_trades",
    "sample_n_price", "sample_n_rent", "confidence", "error", "asof",
]
RATE_METRICS = [
    "trade_turnover_12m", "trade_turnover_24m_ann", "trade_turnover_36m_ann", "active_trade_month_ratio",
    "max_trade_month_share", "rent_turnover_12m", "monthly_rent_turnover_12m", "new_contract_turnover_12m",
    "active_rent_month_ratio", "price_cagr_3y", "price_cagr_5y", "mature_price_cagr", "mdd_3y", "mdd_5y",
    "positive_year_ratio", "gross_yield", "gross_yield_equiv",
]


# ── 날짜 ────────────────────────────────────────────

def default_asof(today=None):
    """신고 기한이 지난 마지막 월말."""
    d = (today or date.today()) - timedelta(days=REPORT_LAG_DAYS)
    return d if (d + timedelta(days=1)).day == 1 else d.replace(day=1) - timedelta(days=1)


def window(asof, months_back, months_len=12):
    """(시작, 끝] 문자열 — 기준일에서 months_back개월 전까지 months_len개월."""
    end = rt_rent.months_before(asof, months_back) if months_back else asof
    start = rt_rent.months_before(end, months_len)
    return start.isoformat(), end.isoformat()


def in_win(d, w):
    return w[0] < d <= w[1]


def ym(d):
    return d[:4] + d[5:7]


# ── 데이터 ──────────────────────────────────────────

def region_codes(tokens):
    tokens = ["서울 전체" if t in ("서울", "서울시", "서울특별시") else t for t in tokens]
    return resolve(tokens)


def load(codes, start_ym=None, end_ym=None, db_path=rt_core.DB_PATH):
    """-> (매매 목록, 전월세 목록, 중복 매매 수 {단지키: n}, 수집 범위 {(dataset, sgg): {ym}})"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = f"""SELECT dataset, sgg_cd, sgg_nm, umd, jibun, name, area, floor, deal_date, deal_ym, price, deposit,
                   monthly_rent, contract_type, build_year, cancel_type FROM deals
            WHERE dataset IN ('offi_trade', 'offi_rent') AND sgg_cd IN ({','.join('?' * len(codes))})
              AND deal_ym BETWEEN ? AND ?"""
    try:
        rows = conn.execute(q, [*codes, start_ym or "190001", end_ym or "999912"]).fetchall()
        cov = defaultdict(set)
        for ds, sgg, m in conn.execute("SELECT dataset, sgg_cd, ym FROM fetch_log"):
            cov[(ds, sgg)].add(m)
    finally:
        conn.close()
    trades, rents, seen, dups = [], [], set(), Counter()
    for r in rows:
        d = dict(r)
        if d["cancel_type"] or not d["area"] or not d["deal_date"]:
            continue
        d["key"] = (d["sgg_cd"], d["umd"], norm_jibun(d["jibun"]))
        if not d["key"][2]:
            continue
        sig = (d["dataset"], d["key"], d["name"], d["area"], d["floor"], d["deal_date"], d["price"],
               d["deposit"], d["monthly_rent"])
        # 같은 단지·면적·층·날짜·금액 계약이 또 있으면 중복 의심으로 세기만 한다.
        # API에 호수가 없어 일괄매각(같은 날 같은 층 여러 호를 같은 값에)과 구분이 안 되므로 지우지 않는다.
        if sig in seen and d["dataset"] == "offi_trade":
            dups[d["key"]] += 1
        seen.add(sig)
        d["bin"] = rt_rent.area_bin(d["area"])
        d["ppa"] = (d["price"] / d["area"]) if d["price"] else None
        if d["dataset"] == "offi_trade":
            if d["price"]:
                trades.append(d)
        else:
            rents.append(d)
    return trades, rents, dups, cov


def covered(cov, dataset, sgg_cd, w):
    """창 (시작, 끝]의 모든 달이 수집돼 있는가."""
    have = cov.get((dataset, sgg_cd), set())
    s = (datetime.strptime(w[0], "%Y-%m-%d").date() + timedelta(days=1)).strftime("%Y%m")
    return all(m in have for m in rt_core.month_list(s, ym(w[1])))


# ── 재고 ────────────────────────────────────────────

def parcel_of(conn, key):
    sgg_cd, umd, jibun = key
    pj = rt_rent.parse_parcel(jibun)
    if not pj:
        return None
    try:
        bj = rt_rent.bjdong_code(conn, sgg_cd, umd)
    except Exception:  # code.go.kr 장애 등
        return None
    return (sgg_cd, bj, *pj) if bj else None


def complex_stock(conn, key, name):
    """-> {"total", "bins": {면적군: 호수}, "quality", "note", "units_title", "units_expos"} (캐시만 사용)"""
    sgg_cd, umd, jibun = key
    st = rt_rent.get_building_stock(conn, sgg_cd, umd, jibun, "오피스텔", name, fetch=False)
    out = {"total": st["stock"], "bins": {}, "quality": "D 없음", "note": st["note"],
           "units_title": st["units"], "units_expos": st["units_expos"]}
    if not st["stock"]:
        return out
    if st["stock_src"] != "전유부 오피스텔 호수":
        out["quality"] = "C 표제부 호수(상가 포함 가능)"
        return out
    out["quality"] = "B 전유부·단지전체"
    pc = parcel_of(conn, key)
    if pc:
        names = set(filter(None, st["building_name"].split(" / ")))
        rows = conn.execute(f"""SELECT building_name, unit_use, area_bin, n FROM building_unit_areas
                                WHERE {rt_rent._parcel_where()}""", pc).fetchall()
        multi = len({b for b, *_ in rows}) > 1
        bins = Counter()
        for b, use, ab, n in rows:
            if "오피스텔" in use and ab and (not multi or not names or b in names):
                bins[ab] += n
        if bins and sum(bins.values()) == st["stock"]:
            out["bins"], out["quality"] = dict(bins), "A 전유부·면적별"
    return out


def build_stock(codes, limit=None, workers=6, start_ym="201501"):
    """서울(지정 지역) 오피스텔 단지 전체의 건축물대장을 병렬 선조회. 거래 많은 단지부터."""
    trades, rents, _, _ = load(codes, start_ym)
    count = Counter(d["key"] for d in trades) + Counter(d["key"] for d in rents)
    conn = rt_rent.connect()
    parcels, force = {}, set()
    try:
        for key, _ in count.most_common(limit):
            pc = parcel_of(conn, key)
            if not pc:
                continue
            parcels[pc] = key[2]
            has_units = conn.execute(f"SELECT n FROM building_fetch WHERE {rt_rent._parcel_where()} AND kind='units'",
                                     pc).fetchone()
            has_area = conn.execute(f"SELECT 1 FROM building_unit_areas WHERE {rt_rent._parcel_where()} LIMIT 1",
                                    pc).fetchone()
            if has_units and has_units[0] and not has_area:
                force.add(pc)  # 면적별 호수가 없는 예전 캐시 → 전유부 다시
    finally:
        conn.close()
    print(f"단지 {len(count):,}곳 중 지번 확인 {len(parcels):,}곳 (면적별 재조회 대상 {len(force):,}곳)")
    n = rt_rent.prefetch_parcels(
        parcels, dict.fromkeys(parcels, "오피스텔"), workers, units=True, force_units=force,
        progress=lambda d, t, m: print(f"  [{d}/{t}] {m}", flush=True) if d % 50 == 0 or d == t else None)
    print(f"새로 조회 {n:,}곳")
    for msg in rt_rent._blocked.values():
        print(f"[건축물대장] {msg} → 내일 같은 명령으로 이어서 받으세요")


# ── 신축 연차 가격곡선 ───────────────────────────────

def market_index(trades):
    """서울 성숙 재고(연차 5+) 분기 ㎡당가 중위 (전후 1분기 포함)."""
    raw = defaultdict(list)
    for d in trades:
        if d["age"] is not None and d["age"] >= MATURE_INDEX_AGE:
            raw[quarter_of(d["deal_date"])].append(d["ppa"])
    idx = {}
    for q in raw:
        vals = raw.get(q - 1, []) + raw[q] + raw.get(q + 1, [])
        if len(vals) >= 30:
            idx[q] = median(vals)
    return idx


def age_curve(trades, idx):
    """-> (곡선 행 목록, 추정 T 또는 None)"""
    by_group_age = defaultdict(list)
    for d in trades:
        q = quarter_of(d["deal_date"])
        if d["age"] is None or d["age"] > AGE_MAX or q not in idx or excluded(d):
            continue
        by_group_age[(d["key"], d["bin"])].append((d["age"], d["ppa"] / idx[q]))
    med = defaultdict(dict)  # group -> {age: 중위 조정㎡당가}
    for g, vals in by_group_age.items():
        ages = defaultdict(list)
        for a, v in vals:
            ages[a].append(v)
        med[g] = {a: median(v) for a, v in ages.items() if len(v) >= 2}
    rows, level = [], 1.0
    changes = {}
    for a in range(0, AGE_MAX + 1):
        pairs = [math.log(m[a] / m[a - 1]) for m in med.values() if a in m and a - 1 in m] if a else []
        base = [m[a] / m[min(m)] for m in med.values() if a in m and min(m) <= 1]
        d = median(pairs) if len(pairs) >= AGE_MIN_PAIRS else None
        if d is not None:
            level *= math.exp(d)
            changes[a] = d
        rows.append({
            "age": a, "n_groups": sum(1 for m in med.values() if a in m),
            "n_trades": sum(1 for vals in by_group_age.values() for x in vals if x[0] == a),
            "n_pairs": len(pairs), "yoy_change_median": round(math.exp(d) - 1, 4) if d is not None else "",
            "chain_index": round(level, 4) if (a == 0 or d is not None) else "",
            "level_vs_initial_median": round(median(base), 4) if len(base) >= AGE_MIN_PAIRS else "",
            "level_p25": round(_pct(base, 25), 4) if len(base) >= AGE_MIN_PAIRS else "",
            "level_p75": round(_pct(base, 75), 4) if len(base) >= AGE_MIN_PAIRS else "",
        })
    # 정상 감가 속도 = 6~10년차 연간 변화의 중위. T = 다음 두 해 변화가 모두 (정상 감가 - 1%p) 이상인 첫 연차
    late = [changes[a] for a in range(6, 11) if a in changes]
    steady = median(late) if len(late) >= 3 else 0.0
    t = None
    for a in range(1, AGE_MAX - 1):
        if a + 1 in changes and a + 2 in changes and min(changes[a + 1], changes[a + 2]) >= steady + STABLE_CHANGE:
            t = a
            break
    for r in rows:
        r["is_T"] = 1 if r["age"] == t else ""
        r["steady_yoy"] = round(math.exp(steady) - 1, 4)
    return rows, t


def excluded(d):
    """가격·연차곡선·수익률 산정에서 뺄 거래: 분양·입주 초기 대량거래 달, 같은 날 일괄거래."""
    return d.get("supply") or d.get("bulk")


def mark_bulk(trades):
    by_day = defaultdict(list)
    for d in trades:
        by_day[(d["key"], d["deal_date"])].append(d)
    n = 0
    for ds in by_day.values():
        if len(ds) >= BULK_SAME_DAY:
            for d in ds:
                d["bulk"] = True
            n += len(ds)
    return n


def mark_supply_events(trades, stocks):
    """준공 직후(연차 0~1) 대량거래 달 표시. -> {단지키: [달,...]}"""
    early = defaultdict(lambda: defaultdict(list))
    for d in trades:
        if d["age"] is not None and d["age"] <= 1:
            early[d["key"]][ym(d["deal_date"])].append(d)
    events = {}
    for key, months in early.items():
        total = sum(len(v) for v in months.values())
        stock = stocks.get(key) or 0
        bad = []
        for m, ds in months.items():
            big = len(ds) >= max(SUPPLY_MIN_MONTH, SUPPLY_STOCK_SHARE * stock) and len(ds) / total >= 0.3
            same = max(Counter(x["price"] for x in ds).values()) >= SUPPLY_SAME_PRICE
            if big or same:
                bad.append(m)
        if bad:
            events[key] = sorted(bad)
            for m in bad:
                for d in months[m]:
                    d["supply"] = True
    return events


# ── 지표 ────────────────────────────────────────────

def _pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _r(v, nd=2):
    return "" if v is None else round(v, nd)


def year_medians(trades, asof, years=6, field="ppa"):
    """[(k, 중위, n, 중위면적)] k=0이 최근 12개월."""
    out = []
    for k in range(years):
        w = window(asof, 12 * k)
        vals = [d for d in trades if in_win(d["deal_date"], w)]
        if len(vals) >= MIN_YEAR_N:
            out.append((k, median(d[field] for d in vals), len(vals), median(d["area"] for d in vals)))
    return out


def cagr(p_new, p_old, years):
    return ((p_new / p_old) ** (1 / years) - 1) * 100 if p_new and p_old and years > 0 else None


def mdd(trades, asof, n_quarters):
    """분기마다 직전 분기와 합친 2분기 창의 ㎡당가 중위(3건 이상)로 고점 대비 최대 낙폭(%)."""
    end_q = quarter_of(asof.isoformat())
    by_q = defaultdict(list)
    for d in trades:
        q = quarter_of(d["deal_date"])
        if end_q - n_quarters - 1 < q <= end_q:
            by_q[q].append(d["ppa"])
    series = []
    for q in range(end_q - n_quarters + 1, end_q + 1):
        vals = by_q.get(q - 1, []) + by_q.get(q, [])
        if len(vals) >= MIN_QUARTER_N:
            series.append(median(vals))
    if len(series) < max(4, n_quarters // 2):
        return None
    peak, worst = series[0], 0.0
    for v in series:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst * 100


def price_metrics(trades, asof, maturity):
    clean = [d for d in trades if not excluded(d)]
    ym_ = {k: (p, n, a) for k, p, n, a in year_medians(clean, asof)}
    out = {"price_ppa_12m": _r(ym_[0][0], 1) if 0 in ym_ else "",
           "sample_n_price": sum(n for _, n, _ in ym_.values())}
    for yrs in (3, 5):
        out[f"price_cagr_{yrs}y"] = _r(cagr(ym_[0][0], ym_[yrs][0], yrs)) if 0 in ym_ and yrs in ym_ else ""
    ks = sorted(ym_)
    pairs = [(ym_[k][0], ym_[k + 1][0]) for k in ks if k + 1 in ym_ and k + 1 <= 5]
    out["positive_year_ratio"] = _r(sum(a > b for a, b in pairs) / len(pairs)) if len(pairs) >= 3 else ""
    out["mdd_3y"], out["mdd_5y"] = _r(mdd(clean, asof, 12)), _r(mdd(clean, asof, 20))
    mature = [d for d in clean if d["age"] is not None and d["age"] >= maturity]
    mm = {k: p for k, p, _, _ in year_medians(mature, asof)}
    if 0 in mm and max(mm) >= 2:
        k0 = max(mm)
        out["mature_price_cagr"], out["mature_years"] = _r(cagr(mm[0], mm[k0], k0)), k0
    else:
        out["mature_price_cagr"], out["mature_years"] = "", ""
    # 신뢰도: 표본수 + 면적 구성 변화
    old = ym_.get(5) or ym_.get(3)
    if 0 in ym_ and old:
        n_min = min(ym_[0][1], old[1])
        mix = abs(ym_[0][2] - old[2]) / old[2]
        out["price_confidence"] = "상" if n_min >= 8 and mix < 0.1 else ("하" if n_min < 5 or mix > 0.2 else "중")
    else:
        out["price_confidence"] = "하"
    return out


def turnover(n, stock):
    if stock is None or not stock or n is None:
        return None
    return n / stock * 100


def metrics_row(info, trades, rents, stock, stock_quality, asof, maturity, conv_rate, cov, dup_n):
    """단지 또는 단지×면적군 1행."""
    w12, w24, w36 = window(asof, 0, 12), window(asof, 0, 24), window(asof, 0, 36)
    sgg = info["시군구코드"]
    errors = []
    row = {**info, "stock_units": stock or "", "stock_quality": stock_quality,
           "conversion_rate": conv_rate, "maturity_age": maturity, "asof": asof.isoformat(), "dup_trades": dup_n}

    t12 = [d for d in trades if in_win(d["deal_date"], w12)]
    n = {12: len(t12), 24: sum(in_win(d["deal_date"], w24) for d in trades),
         36: sum(in_win(d["deal_date"], w36) for d in trades)}
    row["trade_n_12m"] = n[12]
    row["bulk_trades_12m"] = sum(1 for d in t12 if d.get("bulk"))
    for m in (24, 36):
        row[f"trade_n_{m}m"] = n[m] if covered(cov, "offi_trade", sgg, window(asof, 0, m)) else ""
    if not covered(cov, "offi_trade", sgg, w12):
        errors.append("매매 12개월 수집 안 됨")
        row["trade_n_12m"] = ""
    tt = {12: turnover(n[12], stock) if row["trade_n_12m"] != "" else None,
          24: turnover(n[24] / 2, stock) if row["trade_n_24m"] != "" else None,
          36: turnover(n[36] / 3, stock) if row["trade_n_36m"] != "" else None}
    if tt[12] is not None and tt[12] > MAX_TURNOVER:
        errors.append(f"매매회전율 {tt[12]:.0f}% > {MAX_TURNOVER}% (재고 {stock} < 거래 {n[12]})")
        tt = {k: None for k in tt}
    row["trade_turnover_12m"], row["trade_turnover_24m_ann"], row["trade_turnover_36m_ann"] = (
        _r(tt[12]), _r(tt[24]), _r(tt[36]))
    months = Counter(ym(d["deal_date"]) for d in t12)
    row["active_trade_month_ratio"] = _r(len(months) / 12) if row["trade_n_12m"] != "" else ""
    row["max_trade_month_share"] = _r(max(months.values()) / n[12]) if n[12] else ""
    row["trade_concentration_flag"] = 1 if n[12] >= 4 and max(months.values()) / n[12] >= CONCENTRATION_SHARE else ""

    r12 = [d for d in rents if in_win(d["deal_date"], w12)]
    monthly = [d for d in r12 if (d["monthly_rent"] or 0) > 0]
    if covered(cov, "offi_rent", sgg, w12):
        typed = [d for d in r12 if (d["contract_type"] or "").strip() in ("신규", "갱신")]
        new_n = sum(1 for d in typed if d["contract_type"].strip() == "신규") \
            if r12 and len(typed) / len(r12) >= rt_rent.CONTRACT_TYPE_MIN_SHARE else None
        rt = {"rent": turnover(len(r12), stock), "monthly": turnover(len(monthly), stock),
              "new": turnover(new_n, stock) if new_n is not None else None}
        if rt["rent"] is not None and rt["rent"] > MAX_TURNOVER:
            errors.append(f"임대회전율 {rt['rent']:.0f}% > {MAX_TURNOVER}%")
            rt = dict.fromkeys(rt)
        row.update({"rent_n_12m": len(r12), "monthly_rent_n_12m": len(monthly),
                    "new_contract_n_12m": "" if new_n is None else new_n,
                    "rent_turnover_12m": _r(rt["rent"]), "monthly_rent_turnover_12m": _r(rt["monthly"]),
                    "new_contract_turnover_12m": _r(rt["new"]),
                    "active_rent_month_ratio": _r(len({ym(d["deal_date"]) for d in r12}) / 12)})
    else:
        errors.append("전월세 12개월 수집 안 됨")

    row.update(price_metrics(trades, asof, maturity))

    # 수익률: ㎡당 기준으로 맞춰 면적 차이를 없앤다
    sale = [d for d in t12 if not excluded(d)]
    if len(sale) < MIN_YIELD_N:
        sale = [d for d in trades if in_win(d["deal_date"], w24) and not excluded(d)]
    row["sample_n_rent"] = len(monthly)
    if len(sale) >= MIN_YIELD_N and len(monthly) >= MIN_YIELD_N:
        ppa = median(d["ppa"] for d in sale)
        rent_ppa = median(d["monthly_rent"] / d["area"] for d in monthly)
        equiv_ppa = median((d["monthly_rent"] + (d["deposit"] or 0) * conv_rate / 12) / d["area"] for d in monthly)
        row.update({"gross_yield": _r(rent_ppa * 12 / ppa * 100), "gross_yield_equiv": _r(equiv_ppa * 12 / ppa * 100),
                    "rent_ppa_12m": _r(rent_ppa, 3)})

    # 종합 신뢰도 = 분모 품질 · 가격 신뢰도(가격지표가 있을 때) · 월세 표본 중 가장 낮은 것
    grades = [{"A": 2, "B": 2, "C": 1, "D": 0}[stock_quality[0]],
              2 if len(monthly) >= 10 else (1 if len(monthly) >= MIN_YIELD_N else 0)]
    if row.get("price_cagr_3y") != "":
        grades.append({"상": 2, "중": 1, "하": 0}[row["price_confidence"]])
    row["confidence"] = "하중상"[min(grades)]
    row["error"] = "; ".join(errors)
    return row


def build_panel(codes, asof, maturity="auto", conv_rate=CONV_RATE, start_ym=None, end_ym=None, log=print):
    trades, rents, dups, cov = load(codes, start_ym, end_ym or asof.strftime("%Y%m"))
    trades = [d for d in trades if d["deal_date"] <= asof.isoformat()]
    rents = [d for d in rents if d["deal_date"] <= asof.isoformat()]
    log(f"매매 {len(trades):,}건 (중복 의심 {sum(dups.values()):,}건은 표시만), 전월세 {len(rents):,}건, 기준일 {asof}")

    by_key = defaultdict(lambda: {"t": [], "r": []})
    for d in trades:
        by_key[d["key"]]["t"].append(d)
    for d in rents:
        by_key[d["key"]]["r"].append(d)

    conn = rt_rent.connect()
    try:
        stocks, info = {}, {}
        for key, g in by_key.items():
            names = Counter(d["name"] for d in g["t"] + g["r"] if d["name"])
            years = Counter(d["build_year"] for d in g["t"] + g["r"] if d["build_year"])
            by = years.most_common(1)[0][0] if years else None
            name = names.most_common(1)[0][0] if names else ""
            info[key] = {"시군구코드": key[0], "시군구": CODE_TO_SGG.get(key[0], key[0]), "법정동": key[1],
                         "단지명": name, "지번": key[2], "건축년도": by or "",
                         "연식": asof.year - by if by else ""}
            stocks[key] = complex_stock(conn, key, name)
    finally:
        conn.close()
    q = Counter(s["quality"][0] for s in stocks.values())
    log(f"단지 {len(by_key):,}곳 · 재고 품질 " + ", ".join(f"{k} {v:,}" for k, v in sorted(q.items())))

    for d in trades:
        by = info[d["key"]]["건축년도"]
        d["age"] = max(int(d["deal_date"][:4]) - by, 0) if by else None
    events = mark_supply_events(trades, {k: s["total"] for k, s in stocks.items()})
    log(f"분양·입주 초기 대량거래 단지 {len(events):,}곳, 같은 날 일괄거래 {mark_bulk(trades):,}건 → 가격 산정 제외")
    idx = market_index(trades)
    curve, t_auto = age_curve(trades, idx)
    if maturity == "auto":
        mat = t_auto if t_auto is not None else DEFAULT_MATURITY
        log(f"성숙 연차 T = {mat} " + ("(가격곡선에서 추정)" if t_auto is not None else
                                       f"(추정 실패 → 기본값 {DEFAULT_MATURITY})"))
    else:
        mat = int(maturity)
        log(f"성숙 연차 T = {mat} (지정, auto 추정치 {t_auto})")

    rows = []
    for key, g in by_key.items():
        st = stocks[key]
        base = {**info[key], "initial_supply_event": 1 if key in events else 0,
                "stock_note": st["note"] if st["quality"][0] in "CD" else ""}
        rows.append({"area_bin": ALL, **metrics_row({**base}, g["t"], g["r"], st["total"], st["quality"], asof,
                                                    mat, conv_rate, cov, dups.get(key, 0))})
        for ab in AREA_LABELS:
            tb = [d for d in g["t"] if d["bin"] == ab]
            rb = [d for d in g["r"] if d["bin"] == ab]
            if not tb and not rb:
                continue
            if st["bins"]:
                stock, quality = st["bins"].get(ab), st["quality"]
            else:  # 면적별 호수 없음 → 단지 전체 호수로 나누고 품질에 표시
                stock, quality = st["total"], (st["quality"] if st["quality"][0] in "CD"
                                               else "B 전유부·단지전체")
            r = metrics_row({**base, "area_bin": ab}, tb, rb, stock, quality, asof, mat, conv_rate, cov, 0)
            r["area_bin"] = ab
            rows.append(r)
    for r in rows:
        r["area_bin"] = r.get("area_bin") or ALL
    return rows, curve, mat, t_auto


# ── 집계 · 분포 · Pareto ─────────────────────────────

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def aggregate(rows, level):
    """level: '법정동' 또는 '시군구'. 단지 행 → stock 가중평균 + 단지 중위값."""
    keys = ["시군구코드", "시군구", "법정동"] if level == "법정동" else ["시군구코드", "시군구"]
    groups = defaultdict(list)
    for r in rows:
        groups[(*[r[k] for k in keys], r["area_bin"])].append(r)
    out = []
    for gk, rs in groups.items():
        stocks = sorted((_num(r["stock_units"]) or 0 for r in rs), reverse=True)
        total = sum(stocks)
        rec = {**dict(zip(keys, gk)), "area_bin": gk[-1], "n_complexes": len(rs),
               "n_complexes_with_stock": sum(1 for s in stocks if s), "stock_units": total,
               "top1_stock_share": _r(stocks[0] / total) if total else "",
               "top3_stock_share": _r(sum(stocks[:3]) / total) if total else "",
               "trade_n_12m": sum(_num(r["trade_n_12m"]) or 0 for r in rs),
               "rent_n_12m": sum(_num(r.get("rent_n_12m")) or 0 for r in rs),
               "sample_n_price": sum(_num(r.get("sample_n_price")) or 0 for r in rs)}
        for m in RATE_METRICS:
            pairs = [(_num(r.get(m)), _num(r["stock_units"])) for r in rs]
            pairs = [(v, w) for v, w in pairs if v is not None and w]
            wsum = sum(w for _, w in pairs)
            rec[m] = _r(sum(v * w for v, w in pairs) / wsum) if wsum else ""
            rec[f"{m}_median"] = _r(median(v for v, _ in pairs)) if pairs else ""
            rec[f"{m}_n"] = len(pairs)
        out.append(rec)
    return out


def distribution(levels):
    """[(level, rows)] → 지표별 p10/p25/median/p75/p90 (area_bin별)."""
    out = []
    for level, rows in levels:
        for ab in [ALL, *AREA_LABELS]:
            rs = [r for r in rows if r["area_bin"] == ab]
            for m in RATE_METRICS:
                vals = [_num(r.get(m)) for r in rs]
                vals = [v for v in vals if v is not None]
                if not vals:
                    continue
                out.append({"level": level, "area_bin": ab, "metric": m, "n": len(vals),
                            **{f"p{p}" if p != 50 else "median": _r(_pct(vals, p)) for p in (10, 25, 50, 75, 90)}})
    return out


def pct_rank(vals, x):
    vals = [v for v in vals if v is not None]
    return sum(v <= x for v in vals) / len(vals) * 100 if vals else None


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 10:
        return None, len(pairs)

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0] * len(v)
        for rank, i in enumerate(order):
            r[i] = rank
        return r
    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    return 1 - 6 * sum((a - b) ** 2 for a, b in zip(rx, ry)) / (n * (n * n - 1)), n


def pareto(rows, level, min_stock):
    """가중치 없는 교집합: 가격방어 상위25% ∧ 매매회전율 상위25% ∧ 임대회전율 상위25% ∧ 수익률 상위50%."""
    rs = [r for r in rows if r["area_bin"] == ALL and (_num(r["stock_units"]) or 0) >= min_stock]
    for r in rs:  # 가격방어: 성숙 CAGR, 없으면 5년 CAGR
        r["_defense"] = _num(r.get("mature_price_cagr"))
        r["defense_metric"] = "mature_price_cagr"
        if r["_defense"] is None:
            r["_defense"], r["defense_metric"] = _num(r.get("price_cagr_5y")), "price_cagr_5y"
    conds = [("_defense", 75), ("trade_turnover_12m", 75), ("rent_turnover_12m", 75), ("gross_yield", 50)]
    th = {m: _pct([_num(r.get(m)) for r in rs], p) for m, p in conds}
    picked = [r for r in rs if all(th[m] is not None and _num(r.get(m)) is not None and _num(r[m]) >= th[m]
                                   for m, _ in conds)]
    out = []
    for r in picked:
        rec = {"level": level, **{k: v for k, v in r.items() if not k.startswith("_")}}
        rec["thresholds"] = ", ".join(f"{m.strip('_')}≥{_r(th[m])}" for m, _ in conds)
        out.append(rec)
    return out, th, len(rs)


def write_csv(path, rows, fields=None):
    fields = fields or list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run(codes, asof, maturity, conv_rate, start_ym=None, end_ym=None):
    rows, curve, mat, t_auto = build_panel(codes, asof, maturity, conv_rate, start_ym, end_ym)
    dong, gu = aggregate(rows, "법정동"), aggregate(rows, "시군구")
    write_csv(OUT_COMPLEX, rows, COMPLEX_FIELDS)
    write_csv(OUT_DONG, dong)
    write_csv(OUT_GU, gu)
    write_csv(OUT_AGE, curve)
    dist = distribution([("complex", rows), ("dong", dong), ("gu", gu)])
    write_csv(OUT_DIST, dist)
    p_c, th_c, n_c = pareto(rows, "complex", PARETO_MIN_STOCK["complex"])
    p_d, th_d, n_d = pareto(dong, "dong", PARETO_MIN_STOCK["dong"])
    write_csv(OUT_PARETO, p_d + p_c)

    print(f"\n저장: {OUT_COMPLEX} ({len(rows):,}행), {OUT_DONG} ({len(dong):,}), {OUT_GU} ({len(gu):,}), "
          f"{OUT_AGE}, {OUT_DIST}, {OUT_PARETO} (동 {len(p_d)} / 단지 {len(p_c)})")
    errs = Counter(e.split(" ")[0] for r in rows for e in r["error"].split("; ") if e)
    if errs:
        print("오류·결측 사유: " + ", ".join(f"{k} {v:,}" for k, v in errs.most_common()))

    print("\n[연차별 가격곡선] (시장지수 대비, 같은 단지×면적군 연차 a-1→a 변화의 중위)")
    for c in curve[:11]:
        yoy = c["yoy_change_median"]
        yoy = f"{yoy * 100:+.1f}%" if yoy != "" else "-"
        print(f"  {c['age']:>2}년차  그룹 {c['n_groups']:>5}  쌍 {c['n_pairs']:>5}  전년비 {yoy:>7}  "
              f"누적 {c['chain_index'] or '-'}{'  ← T' if c['is_T'] else ''}")

    print("\n[서울 분포] 단지(area_bin=전체) / 법정동")
    for level, rs in (("단지", rows), ("법정동", dong)):
        rs = [r for r in rs if r["area_bin"] == ALL]
        for m in ("trade_turnover_12m", "rent_turnover_12m", "mature_price_cagr", "price_cagr_5y", "mdd_5y",
                  "gross_yield"):
            vals = [_num(r.get(m)) for r in rs]
            vals = [v for v in vals if v is not None]
            if vals:
                print(f"  {level:<4} {m:<24} n={len(vals):>5}  " + "  ".join(
                    f"p{p}={_pct(vals, p):.2f}" for p in (10, 25, 50, 75, 90)))
    for level, rs in (("단지", rows), ("법정동", dong)):
        rs = [r for r in rs if r["area_bin"] == ALL]
        tv = [_num(r.get("trade_turnover_12m")) for r in rs]
        mc = [_num(r.get("mature_price_cagr")) for r in rs]
        mcv = [v for v in mc if v is not None]
        pr = pct_rank(tv, 4.0)
        print(f"  {level}: 매매회전율 4%는 하위 {pr:.0f}% 지점" if pr is not None else f"  {level}: 매매회전율 없음")
        if mcv:
            print(f"  {level}: mature CAGR ≥ 0 비율 {sum(v >= 0 for v in mcv) / len(mcv) * 100:.0f}% ({len(mcv)}곳)")
        rho, n = spearman([_num(r.get("mdd_5y")) for r in rs], [_num(r.get("gross_yield")) for r in rs])
        if rho is not None:
            print(f"  {level}: MDD(5y) vs 수익률 Spearman ρ = {rho:+.2f} (n={n}, MDD는 음수라 ρ<0이면 낙폭 클수록 수익률 높음)")
    print(f"\n[Pareto] 법정동(재고 {PARETO_MIN_STOCK['dong']}+ {n_d}곳) 기준 "
          + ", ".join(f"{k.strip('_')}≥{_r(v)}" for k, v in th_d.items()) + f" → {len(p_d)}곳")
    for r in sorted(p_d, key=lambda r: -_num(r.get(r["defense_metric"])))[:15]:
        print(f"  {r['시군구']} {r['법정동']}  재고 {r['stock_units']:,}  매매회전 {r['trade_turnover_12m']}%  "
              f"임대회전 {r['rent_turnover_12m']}%  방어({r['defense_metric']}) {r.get(r['defense_metric'])}%  "
              f"수익률 {r['gross_yield']}%  top1 {r['top1_stock_share']}")
    print(f"[Pareto] 단지(재고 {PARETO_MIN_STOCK['complex']}+ {n_c}곳) → {len(p_c)}곳 (파일 참고)")


def complex_deals(sgg_cd, umd, jibun, db_path=rt_core.DB_PATH):
    """화면용: 한 단지(지번)의 해제 제외 매매·전월세 원자료."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""SELECT dataset, jibun, deal_date, name, area, floor, price, deposit, monthly_rent,
                                      contract_type, build_year FROM deals
                               WHERE dataset IN ('offi_trade', 'offi_rent') AND sgg_cd=? AND umd=?
                                 AND IFNULL(cancel_type,'')=''""", (sgg_cd, umd)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if norm_jibun(d["jibun"]) == jibun and d["area"]:
            d["area_bin"] = rt_rent.area_bin(d["area"])
            out.append(d)
    return out


# ── 검증 ────────────────────────────────────────────

VERIFY_PICKS = [  # (구 코드, 법정동 접두, 개수) — 마곡·당산/영등포·구디/신도림·목동/오목교·강남
    ("11500", ("마곡동",), 2), ("11560", ("당산동", "영등포동"), 2), ("11530", ("구로동", "신도림동"), 2),
    ("11470", ("목동", "신정동"), 2), ("11680", ("",), 2),
]


def verify(asof, maturity="auto"):
    codes = sorted({c for c, _, _ in VERIFY_PICKS})
    rows, curve, mat, _ = build_panel(codes, asof, maturity, log=lambda m: None)
    full = {(r["시군구코드"], r["법정동"], r["지번"]): r for r in rows if r["area_bin"] == ALL}
    picks = []
    for sgg, prefixes, k in VERIFY_PICKS:
        cand = [r for key, r in full.items() if key[0] == sgg and r["법정동"].startswith(prefixes)
                and r["stock_quality"][0] in "AB" and _num(r["trade_n_12m"])]
        picks += sorted(cand, key=lambda r: -r["stock_units"])[:k]
    key = rt_core.load_service_key()
    w12 = window(asof, 0, 12)
    cache = {}

    def live(ds, sgg, jibun, umd, monthly_only=False):
        n = 0
        for m in rt_core.month_list(
                (datetime.strptime(w12[0], "%Y-%m-%d").date() + timedelta(days=1)).strftime("%Y%m"), ym(w12[1])):
            if (ds, sgg, m) not in cache:
                cache[(ds, sgg, m)] = rt_core.fetch_month(ds, sgg, m, key)
            n += sum(1 for d in cache[(ds, sgg, m)]
                     if d["umd"] == umd and norm_jibun(d["jibun"]) == jibun and not d["cancel_type"]
                     and in_win(d["deal_date"], w12) and (ds != "offi_trade" or d["price"])
                     and (not monthly_only or (d["monthly_rent"] or 0) > 0))
        return n

    conn = rt_rent.connect()
    out = []
    try:
        trades, _, _, _ = load(codes)
        for r in picks:
            k = (r["시군구코드"], r["법정동"], r["지번"])
            st = complex_stock(conn, k, r["단지명"])
            lt = live("offi_trade", k[0], k[2], k[1])
            lr = live("offi_rent", k[0], k[2], k[1])
            path = {}
            for d in trades:
                if d["key"] == k and r["건축년도"]:
                    path.setdefault(max(int(d["deal_date"][:4]) - r["건축년도"], 0), []).append(d["ppa"])
            path_s = " ".join(f"{a}:{median(v):,.0f}" for a, v in sorted(path.items()) if a <= 10 and len(v) >= 2)
            ok = lambda a, b: "O" if a == b else "X"  # noqa: E731
            out.append({
                "구": r["시군구"], "법정동": r["법정동"], "지번": r["지번"], "단지명": r["단지명"], "준공": r["건축년도"],
                "표제부호수": st["units_title"], "전유부오피스텔": st["units_expos"], "stock": r["stock_units"],
                "품질": r["stock_quality"][0],
                "매매12m(DB)": r["trade_n_12m"], "매매12m(API)": lt, "일치": ok(r["trade_n_12m"], lt),
                "임대12m(DB)": r.get("rent_n_12m", ""), "임대12m(API)": lr, "일치 ": ok(r.get("rent_n_12m"), lr),
                "매매회전%": r["trade_turnover_12m"], "임대회전%": r.get("rent_turnover_12m", ""),
                "검산": "O" if r["trade_turnover_12m"] == _r(turnover(lt, r["stock_units"])) else "X",
                "연차별 ㎡당가(만원)": path_s, "오류": r["error"],
            })
    finally:
        conn.close()
    rt_rent._print_table(out)
    print(f"\n기준일 {asof}, 집계창 {w12[0]} 초과 ~ {w12[1]}. 연차 = 거래연도 - 건축연도, 연차별 ㎡당가는 시장지수 보정 전 원값.")
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="서울 오피스텔 시장 패널 (방어력·유동성·수익성)")
    p.add_argument("--region", nargs="+", default=["서울"], help="지역 (기본 서울 전체)")
    p.add_argument("--from", dest="start", help="사용할 거래 시작 YYYYMM (수집 시 시작월)")
    p.add_argument("--to", dest="end", help="사용할 거래 끝 YYYYMM (수집 시 끝월, 기준일 기본값의 상한)")
    p.add_argument("--asof", help="기준일 YYYY-MM-DD (기본: 신고기한 30일이 지난 마지막 월말)")
    p.add_argument("--maturity", default="auto", help="성숙 연차 T: auto 또는 숫자")
    p.add_argument("--conv-rate", type=float, default=CONV_RATE, help=f"보증금 환산율 (기본 {CONV_RATE})")
    p.add_argument("--collect", action="store_true", help="오피스텔 매매·전월세를 --from~--to로 수집")
    p.add_argument("--build-stock", action="store_true", help="단지 건축물대장(표제부·전유부) 병렬 선조회")
    p.add_argument("--limit", type=int, help="--build-stock: 거래 많은 상위 N개 단지만")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--verify", action="store_true", help="10개 단지 검증표 (국토부 API 재조회 대조)")
    a = p.parse_args()
    codes = region_codes(a.region)
    asof = datetime.strptime(a.asof, "%Y-%m-%d").date() if a.asof else default_asof()
    if a.end and not a.asof:
        y, m = int(a.end[:4]), int(a.end[4:])
        asof = min(asof, (date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)))

    if a.collect:
        months = rt_core.month_list(a.start or "201501", a.end or rt_core.current_ym())
        for ds in ("offi_trade", "offi_rent"):
            s = rt_core.collect([ds], codes, months, progress=lambda d, t, msg: print(f"  [{d}/{t}] {msg}")
                                if d % 100 == 0 or d == t else None)
            print(f"{rt_core.DATASETS[ds]['label']}: 새로 {s['fetched']}개월 ({s['rows']:,}건), 건너뜀 {s['skipped']}"
                  + "".join(f"\n  {e}" for e in s["errors"] + s["unregistered"]))
        return
    if a.build_stock:
        build_stock(codes, a.limit, a.workers)
        return
    if a.verify:
        verify(asof, a.maturity)
        return
    run(codes, asof, a.maturity, a.conv_rate, a.start, a.end)


if __name__ == "__main__":
    main()
