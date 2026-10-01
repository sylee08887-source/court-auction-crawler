"""
경매 물건 ↔ 실거래가 매칭 및 시세 추정.

매칭 순서
  1) 구 + 법정동 + 지번 일치                          → "지번"
  2) 구 + 법정동 + 건물명 유사(지번 표기가 달라도)      → "동+건물명"
  3) 구 + 건물명 거의 동일(법정동 표기가 다른 경우)     → "구+건물명"

시세 추정 (매각기일 기준 -6/+3개월 → 부족하면 -12/+3 → -24/+6으로 확장)
  - 경매 전용면적을 알면: 면적 ±10%(최소 ±2㎡) 거래의 ㎡당가 중위 × 면적  → "유사면적"
                          유사면적 거래가 없으면 건물 전체 ㎡당가 중위 × 면적 → "㎡당가×면적"
  - 면적을 모르면: 건물 전체 거래가 중위                                 → "건물중위(면적미상)"
"""

import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from statistics import median

import rt_core

WINDOWS = [(6, 3), (12, 3), (24, 6)]  # (개월 전, 개월 후)
MIN_DEALS = 3
NAME_MIN_SCORE = 0.75        # 같은 동 안에서 건물명 매칭 기준
NAME_MIN_SCORE_GU = 0.9      # 구 전체에서 건물명 매칭 기준


# ── 주소/이름 정규화 ─────────────────────────────────

def norm_jibun(j):
    j = re.sub(r"\s|번지", "", str(j or ""))
    return re.sub(r"-0+$", "", j)


def bonbun(j):
    return norm_jibun(j).split("-")[0] if j else ""


def norm_name(name):
    s = re.sub(r"\(.*?\)", "", str(name or ""))
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", s).lower()
    s = s.replace("오피스텔", "")
    return s


def name_score(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 3 and short in long_:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


FLOOR_RE = re.compile(r"제?\s*(지하)?\s*(\d+)\s*층")
UNIT_TAIL_RE = re.compile(r"\s(?:제?\s*[\dA-Za-z가-힣]*\d+동|제?\s*지?하?\d+층|제?\s*[\dA-Za-z\-]+호|지하)")


def parse_address(addr):
    """
    경매 소재지 문자열 -> {구, 법정동, 지번, 건물명, 층}
      '서울특별시 금천구 독산동 336-8 미림에이클래스 102동 11층1102호'
      '서울특별시 금천구 가산로 143 제5층 제504호 (가산동, 하이펠리스)'
    """
    out = {"구": "", "법정동": "", "지번": "", "건물명": "", "층": None}
    addr = str(addr or "").strip()
    m = re.search(r"(\S+구)\s", addr)
    if m:
        out["구"] = m.group(1)
    f = FLOOR_RE.search(addr)
    if f:
        out["층"] = -int(f.group(2)) if f.group(1) else int(f.group(2))

    paren = re.search(r"\(([^()]*)\)\s*$", addr)
    if paren:  # 도로명 주소: '(법정동, 건물명)'
        parts = [p.strip() for p in paren.group(1).split(",")]
        if parts and re.search(r"(동|가|리)$", parts[0]):
            out["법정동"] = parts[0]
        if len(parts) > 1:
            out["건물명"] = parts[1]
        return out

    m = re.search(r"\S+구\s+(\S+?(?:동|가|리))\s+(산?\d+(?:-\d+)?)(.*)$", addr)
    if m:  # 지번 주소: '동 지번 [건물명] [동/층/호]'
        out["법정동"], out["지번"] = m.group(1), m.group(2)
        rest = " " + m.group(3).strip()
        cut = UNIT_TAIL_RE.search(rest)
        name = rest[:cut.start()] if cut else rest
        out["건물명"] = re.sub(r"^[\s,]+|[\s,]+$", "", name)
    return out


def auction_location(row):
    """크롤러의 구조화 필드를 우선 쓰고, 없으면 소재지를 파싱한다."""
    parsed = parse_address(row.get("소재지", ""))
    loc = {
        "구": row.get("구") or parsed["구"],
        "법정동": row.get("법정동") or parsed["법정동"],
        "지번": row.get("지번") or parsed["지번"],
        "건물명": row.get("건물명") or parsed["건물명"],
        "층": parsed["층"],
    }
    if row.get("층호"):
        f = FLOOR_RE.search(row["층호"])
        if f:
            loc["층"] = -int(f.group(2)) if f.group(1) else int(f.group(2))
    try:
        loc["면적"] = float(row.get("전용면적(㎡)") or 0) or None
    except ValueError:
        loc["면적"] = None
    loc["구조화"] = bool(row.get("지번"))
    return loc


# ── 실거래 인덱스 ────────────────────────────────────

class MarketIndex:
    """오피스텔 매매 실거래(해제 제외)를 구/동/지번/건물 단위로 색인."""

    def __init__(self, deals):
        self.by_jibun = defaultdict(list)
        self.buildings_by_dong = defaultdict(lambda: defaultdict(list))
        self.buildings_by_gu = defaultdict(lambda: defaultdict(list))
        for d in deals:
            gu, dong, jibun = d["sgg_nm"], d["umd"], norm_jibun(d["jibun"])
            self.by_jibun[(gu, dong, jibun)].append(d)
            bkey = (dong, jibun, d["name"])
            self.buildings_by_dong[(gu, dong)][bkey].append(d)
            self.buildings_by_gu[gu][bkey].append(d)
        self.size = len(deals)

    @classmethod
    def from_db(cls, dataset="offi_trade", db_path=rt_core.DB_PATH):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT sgg_nm, umd, jibun, name, area, deal_date, price, floor FROM deals
                   WHERE dataset=? AND IFNULL(cancel_type,'')='' AND price>0""", (dataset,)).fetchall()
        finally:
            conn.close()
        return cls([dict(r) for r in rows])

    @classmethod
    def from_legacy_csv(cls, path):
        import csv
        deals = []
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("해제여부"):
                    continue
                date = r.get("계약년월일") or (
                    f"{r['계약년도']}-{r['계약월'].zfill(2)}-{r['계약일'].zfill(2)}" if r.get("계약년도") else "")
                try:
                    deals.append({"sgg_nm": r["구"], "umd": r["읍면동"], "jibun": r["지번"],
                                  "name": r["오피스텔명"], "area": float(r["전용면적(㎡)"] or 0),
                                  "deal_date": date, "price": int(str(r["거래금액(만원)"]).replace(",", "")),
                                  "floor": r.get("층")})
                except (ValueError, KeyError):
                    continue
        return cls(deals)

    def candidates(self, loc):
        """(거래 목록, 매칭방식, 매칭건물명, 매칭지번, 점수)"""
        gu, dong, jibun, bname = loc["구"], loc["법정동"], norm_jibun(loc["지번"]), loc["건물명"]
        if not gu:
            return [], "미매칭", "", "", 0
        bon = bonbun(jibun)

        if dong and jibun:
            hits = self.by_jibun.get((gu, dong, jibun), [])
            if hits:
                names = sorted({h["name"] for h in hits})
                # 한 지번에 여러 건물이면 건물명으로 좁힌다
                if bname and len(names) > 1:
                    best = max(names, key=lambda n: name_score(n, bname))
                    if name_score(best, bname) >= NAME_MIN_SCORE:
                        hits = [h for h in hits if h["name"] == best]
                        names = [best]
                return hits, "지번", " / ".join(names), jibun, 1.0

        if bname:
            tiers = [(self.buildings_by_dong.get((gu, dong), {}) if dong else {}, "동+건물명", NAME_MIN_SCORE)]
            # 지번을 알면서 다른 동에 붙는 건 같은 이름의 다른 건물일 가능성이 커서 구 단위 매칭은 지번 미상일 때만
            if not jibun:
                tiers.append((self.buildings_by_gu.get(gu, {}), "구+건물명", NAME_MIN_SCORE_GU))
            for pool, method, threshold in tiers:
                scored = [(name_score(k[2], bname), k) for k in pool]
                scored = [s for s in scored if s[0] >= threshold]
                # 지번을 알면 본번이 같은 건물만 인정 (예: 336-8 ↔ 336)
                if bon:
                    scored = [s for s in scored if bonbun(s[1][1]) == bon]
                if method == "구+건물명" and len({k for _, k in scored}) > 1:
                    continue  # 구 안에 같은 이름 건물이 여럿이면 판단 보류
                if not scored:
                    continue
                top = max(s for s, _ in scored)
                keys = [k for s, k in scored if s == top]
                hits = [d for k in keys for d in pool[k]]
                return hits, method, " / ".join(sorted({k[2] for k in keys})), \
                    " / ".join(sorted({k[1] for k in keys})), round(top, 2)

        return [], "미매칭", "", "", 0


class MultiIndex:
    """
    여러 실거래 유형을 순서대로 시도한다.
    법원 용도는 '오피스텔'이어도 도시형생활주택 등은 아파트로 신고된 경우가 있어
    오피스텔 → 아파트 순으로 찾는다.
    """

    def __init__(self, parts):
        self.parts = [(label, idx) for label, idx in parts if idx.size]
        self.size = sum(idx.size for _, idx in self.parts)

    @classmethod
    def from_db(cls, datasets=("offi_trade", "apt_trade"), db_path=rt_core.DB_PATH):
        return cls([(rt_core.DATASETS[ds]["prop"], MarketIndex.from_db(ds, db_path)) for ds in datasets])

    def candidates(self, loc):
        """(거래 목록, 매칭방식, 매칭건물명, 매칭지번, 점수, 실거래유형)"""
        for label, idx in self.parts:
            res = idx.candidates(loc)
            if res[0]:
                return (*res, label)
        return [], "미매칭", "", "", 0, ""


# ── 시세 추정 ────────────────────────────────────────

def _in_window(deals, base_dt, before, after):
    start = (base_dt - timedelta(days=before * 30.4)).strftime("%Y-%m-%d")
    end = (base_dt + timedelta(days=after * 30.4)).strftime("%Y-%m-%d")
    return [d for d in deals if start <= (d["deal_date"] or "") <= end]


def estimate(deals, base_dt, area):
    """-> (추정시세(만원), 산출방식, 사용거래목록, 기간라벨)"""
    usable = [d for d in deals if d["price"] and d["area"]]
    for before, after in WINDOWS:
        win = _in_window(usable, base_dt, before, after)
        if not win:
            continue
        label = f"-{before}/+{after}개월"
        if area:
            tol = max(2.0, area * 0.1)
            near = [d for d in win if abs(d["area"] - area) <= tol]
            if len(near) >= MIN_DEALS or (near and (before, after) == WINDOWS[-1]):
                ppa = median(d["price"] / d["area"] for d in near)
                return round(ppa * area), f"유사면적 {len(near)}건", near, label
            if len(win) >= MIN_DEALS or (before, after) == WINDOWS[-1]:
                ppa = median(d["price"] / d["area"] for d in win)
                return round(ppa * area), f"㎡당가×면적 {len(win)}건", win, label
        elif len(win) >= MIN_DEALS or (before, after) == WINDOWS[-1]:
            return round(median(d["price"] for d in win)), f"건물중위(면적미상) {len(win)}건", win, label
    return None, "", [], ""


def match_auction(row, index):
    """경매 1건 -> 요약 dict와 사용한 실거래 목록."""
    loc = auction_location(row)
    out = {
        "구": loc["구"], "법정동": loc["법정동"], "지번": loc["지번"], "건물명": loc["건물명"],
        "층": loc["층"] if loc["층"] is not None else "", "전용면적(㎡)": loc["면적"] or "",
        "매칭방식": "미매칭", "실거래유형": "", "매칭건물명": "", "매칭지번": "", "건물명유사도": "",
        "후보거래수": 0, "비교거래수": 0, "비교기간": "", "시세산출": "", "추정시세(만원)": "",
        "최근비교거래일": "", "낙찰가/시세(%)": "", "최저가/시세(%)": "", "감정가/시세(%)": "", "매칭비고": "",
    }
    try:
        base_dt = datetime.strptime(row.get("매각기일", "")[:10], "%Y-%m-%d")
    except ValueError:
        out["매칭비고"] = "매각기일 없음"
        return out, []

    res = index.candidates(loc)
    hits, method, mname, mjibun, score = res[:5]
    out["실거래유형"] = res[5] if len(res) > 5 else ("오피스텔" if hits else "")
    out.update({"매칭방식": method, "매칭건물명": mname, "매칭지번": mjibun,
                "건물명유사도": score if method.endswith("건물명") else "", "후보거래수": len(hits)})
    if not hits:
        if not loc["구"]:
            out["매칭비고"] = "주소 파싱 실패"
        elif not loc["건물명"] and not loc["지번"]:
            out["매칭비고"] = "지번·건물명 없음"
        else:
            out["매칭비고"] = "실거래 자료에 해당 건물 없음"
        return out, []

    est, how, used, label = estimate(hits, base_dt, loc["면적"])
    if est is None:
        out["매칭비고"] = "비교 기간 내 거래 없음"
        return out, []

    def pct(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return ""
        return round(v / est * 100, 1) if v > 0 else ""

    out.update({
        "비교거래수": len(used), "비교기간": label, "시세산출": how, "추정시세(만원)": est,
        "최근비교거래일": max(d["deal_date"] for d in used),
        "낙찰가/시세(%)": pct(row.get("낙찰가(만원)")),
        "최저가/시세(%)": pct(row.get("최저입찰가(만원)")),
        "감정가/시세(%)": pct(row.get("감정가(만원)")),
    })
    return out, used
