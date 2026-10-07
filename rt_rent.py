"""
경매 물건별 연 월세회전율.

    연 월세회전율(%) = 최근 12개월 월세계약 건수 / 해당 건물 전체 호수 × 100

월세계약
  - realestate.db 전월세(offi_rent / apt_rent / rh_rent) 중 monthly_rent > 0, 해제 제외
    (보증부월세 포함, 전세 제외)
  - 계약구분(신규/갱신)이 70% 이상 채워져 있을 때만 신규 월세회전율을 낸다 (아니면 결측)
  - 집계 기간의 전월세 자료가 다 수집돼 있지 않으면 미산출

건물 매칭 (rt_match 재사용, 애매하면 미산출)
  1) 구 + 법정동 + 지번 정확 → 2) 동 + 건물명 → 3) 구 + 건물명(지번 미상일 때만)
  법원 용도가 오피스텔이면 오피스텔 → 아파트(도시형생활주택 등) 전월세 순으로 찾는다.

전체 호수 (국토교통부 건축HUB 건축물대장, 시군구코드+법정동코드+본번+부번)
  - 오피스텔: 전유부에서 용도가 오피스텔인 호만 센다. 표제부 호수에는 상가·근생 호가 섞여 있고
    표제부 용도란에는 드러나지 않는 경우가 많다 (예: 마곡나루역보타닉푸르지오시티 표제부 1,547호 중
    오피스텔 1,390호, 판매시설 126호). 전유부는 호당 5행 안팎·100행/호출이라 호출이 많아
    prefetch()로 병렬 선조회해 캐시하고, 캐시가 없으면 표제부 호수로 대신하며 비고에 남긴다.
  - 아파트: 표제부 세대수 / 연립·다세대: 세대수 → 호수 → 전유부
  - 한 지번에 이름이 다른 건물이 여럿이면 건물명이 맞는 것만 쓰고, 못 고르면 미산출
  - 결과는 realestate.db(buildings, building_units)에 캐시해 다시 호출하지 않는다
  - 법정동코드는 행정표준코드관리시스템(code.go.kr)에서 시군구 단위로 받아 bjdong_codes에 캐시

사용법:
    py rt_rent.py --verify 5               # 월세 거래가 많은 오피스텔 5곳 검증표 (API 재조회와 대조)
    py rt_rent.py --case 2025타경1234       # 특정 경매물건의 회전율
"""

import argparse
import calendar
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from statistics import median

import requests

import rt_core
from rt_match import MarketIndex, auction_location, name_score, norm_jibun, norm_name

BLD_API = "https://apis.data.go.kr/1613000/BldRgstHubService/"
BLD_APPLY = "국토교통부_건축HUB_건축물대장정보 서비스"
BLD_PAGE = 100
CODE_URL = "https://www.code.go.kr/stdcode/regCodeL.do"

BUILDING_TTL_DAYS = 365      # 건축물대장 캐시 유효기간
NOT_FOUND_TTL_DAYS = 30      # '대장 없음' 결과는 이 기간 뒤 다시 확인
WINDOW_MONTHS = 12
RECENT_MONTHS = 6
CONTRACT_TYPE_MIN_SHARE = 0.7  # 계약구분이 이 비율 이상 채워져야 신규 회전율 산출
MAX_TURNOVER = 100           # 이보다 크면 매칭 오류로 보고 미산출
BLD_NAME_MIN_SCORE = 0.75    # 한 지번 여러 건물일 때 대장 건물명 ↔ 실거래 건물명 기준

# 실거래 유형 → 전월세 데이터셋
RENT_DATASETS = {"오피스텔": "offi_rent", "아파트": "apt_rent", "연립다세대": "rh_rent"}
# 전유부 용도에서 해당 유형 호로 셀 키워드
UNIT_KEYWORDS = {"오피스텔": ("오피스텔",), "아파트": ("아파트",), "연립다세대": ("다세대", "연립")}

COLUMNS = [
    "전체호수", "호수출처", "월세(12개월)", "신규월세(12개월)", "연 월세회전율(%)", "신규 월세회전율(%)",
    "월세(6개월)", "월세 중위보증금(만원)", "월세 중위월세(만원)", "월세 기준일", "월세 집계기간",
    "전월세유형", "전월세매칭", "대장건물명", "대장용도", "회전율비고",
]

_blocked = {}  # 이번 실행에서 막힌 API (미등록/한도) → 메시지

# 전용면적 구간 (rt_officetel_market의 면적군과 공유)
AREA_BINS = [(20, "10~20"), (30, "20~30"), (40, "30~40"), (60, "40~60"), (float("inf"), "60+")]


def area_bin(area):
    if not area:
        return ""
    return next(label for upper, label in AREA_BINS if area < upper)


# ── 저장소 ───────────────────────────────────────────

def connect(db_path=rt_core.DB_PATH):
    conn = rt_core.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bjdong_codes (
            sgg_cd TEXT, umd TEXT, bjdong_cd TEXT, PRIMARY KEY (sgg_cd, umd));
        CREATE TABLE IF NOT EXISTS buildings (
            mgm_pk TEXT PRIMARY KEY, sgg_cd TEXT, bjdong_cd TEXT, plat_gb TEXT, bun TEXT, ji TEXT,
            jibun TEXT, building_name TEXT, dong_name TEXT, main_use TEXT, etc_use TEXT, main_atch TEXT,
            households INTEGER, units INTEGER, families INTEGER, fetched_at TEXT);
        CREATE INDEX IF NOT EXISTS ix_buildings_parcel ON buildings(sgg_cd, bjdong_cd, plat_gb, bun, ji);
        CREATE TABLE IF NOT EXISTS building_units (
            sgg_cd TEXT, bjdong_cd TEXT, plat_gb TEXT, bun TEXT, ji TEXT,
            building_name TEXT, dong_name TEXT, unit_use TEXT, n INTEGER, fetched_at TEXT);
        CREATE INDEX IF NOT EXISTS ix_building_units_parcel
            ON building_units(sgg_cd, bjdong_cd, plat_gb, bun, ji);
        CREATE TABLE IF NOT EXISTS building_unit_areas (
            sgg_cd TEXT, bjdong_cd TEXT, plat_gb TEXT, bun TEXT, ji TEXT,
            building_name TEXT, unit_use TEXT, area_bin TEXT, n INTEGER, fetched_at TEXT);
        CREATE INDEX IF NOT EXISTS ix_building_unit_areas_parcel
            ON building_unit_areas(sgg_cd, bjdong_cd, plat_gb, bun, ji);
        CREATE TABLE IF NOT EXISTS building_fetch (
            sgg_cd TEXT, bjdong_cd TEXT, plat_gb TEXT, bun TEXT, ji TEXT, kind TEXT, fetched_at TEXT,
            n INTEGER, PRIMARY KEY (sgg_cd, bjdong_cd, plat_gb, bun, ji, kind));
    """)
    return conn


# ── 법정동코드 ───────────────────────────────────────

def fetch_bjdong_codes(sgg_cd):
    """code.go.kr 법정동코드 목록에서 {법정동명: 법정동코드 5자리} (현존 코드만)."""
    out, page = {}, 1
    while True:
        resp = requests.post(CODE_URL, timeout=30, data={
            "cPage": page, "pageSize": 100, "regionCd": "", "locataddNm": "", "disuseAt": "0",
            "searchOk": "0", "codeseId": "00002", "sidoCd": sgg_cd[:2], "sggCd": sgg_cd[2:],
            "umdCd": "*", "riCd": "*", "chkWantCnt": "0"})
        text = re.sub(r"\s+", " ", resp.text)
        rows = re.findall(r'(\d{10})</td> <td class="table_center01">([^<]+)<', text)
        for code, name in rows:
            if code.startswith(sgg_cd) and code[5:] != "00000":
                out[name.split()[-1]] = code[5:]
        if len(rows) < 100:
            return out
        page += 1


def bjdong_code(conn, sgg_cd, umd):
    row = conn.execute("SELECT bjdong_cd FROM bjdong_codes WHERE sgg_cd=? AND umd=?", (sgg_cd, umd)).fetchone()
    if row:
        return row[0]
    if conn.execute("SELECT 1 FROM bjdong_codes WHERE sgg_cd=? LIMIT 1", (sgg_cd,)).fetchone():
        return None  # 이미 받은 시군구인데 없는 동 이름
    codes = fetch_bjdong_codes(sgg_cd)
    with conn:
        conn.executemany("INSERT OR REPLACE INTO bjdong_codes VALUES (?, ?, ?)",
                         [(sgg_cd, u, c) for u, c in codes.items()])
    return codes.get(umd)


def parse_parcel(jibun):
    """'336-8' -> ('0', '0336', '0008'), '산12' -> ('1', '0012', '0000'). 형식이 다르면 None."""
    m = re.fullmatch(r"(산)?(\d+)(?:-(\d+))?", norm_jibun(jibun))
    if not m:
        return None
    return ("1" if m.group(1) else "0", m.group(2).zfill(4), (m.group(3) or "0").zfill(4))


# ── 건축물대장 API ───────────────────────────────────

def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _bld_request(op, params, key):
    """-> (item 목록, totalCount)"""
    q = {"serviceKey": key, "_type": "json", **params}
    last_exc = None
    for attempt in range(6):
        try:
            resp = requests.get(BLD_API + op, params=q, timeout=60)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
            continue
        text = resp.text
        if "SERVICE_KEY_IS_NOT_REGISTERED" in text:
            raise rt_core.ApiNotRegistered(f"건축물대장: 인증키 미등록 (data.go.kr에서 '{BLD_APPLY}' 활용신청 필요)")
        if "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in text:
            raise rt_core.ApiError("건축물대장 일일 호출 한도 초과 - 내일 다시 시도하세요")
        if resp.status_code >= 500:
            last_exc = rt_core.ApiError(f"HTTP {resp.status_code}")
            time.sleep(1.5 * (attempt + 1))
            continue
        try:
            body = resp.json()["response"]
        except (ValueError, KeyError):
            last_exc = rt_core.ApiError(f"응답 파싱 실패(HTTP {resp.status_code}, {len(text)}바이트): {text[:80]}")
            time.sleep(1.5 * (attempt + 1))
            continue
        code = (body.get("header") or {}).get("resultCode", "")
        if code not in ("00", "000"):
            raise rt_core.ApiError(f"[{code}] {(body.get('header') or {}).get('resultMsg', '')}")
        b = body.get("body") or {}
        items = b.get("items") or {}
        items = items.get("item", []) if isinstance(items, dict) else []
        if isinstance(items, dict):
            items = [items]
        return items, _int(b.get("totalCount"))
    raise rt_core.ApiError(f"건축물대장 요청 실패 ({op}): {last_exc}")


def _bld_fetch_all(op, parcel, key):
    sgg_cd, bjdong_cd, plat_gb, bun, ji = parcel
    params = {"sigunguCd": sgg_cd, "bjdongCd": bjdong_cd, "platGbCd": plat_gb, "bun": bun, "ji": ji}
    rows, page = [], 1
    while True:
        items, total = _bld_request(op, {**params, "numOfRows": BLD_PAGE, "pageNo": page}, key)
        rows.extend(items)
        if len(rows) >= total or not items:
            return rows
        page += 1
        time.sleep(rt_core.REQUEST_DELAY)


def _is_fresh(conn, parcel, kind):
    row = conn.execute("""SELECT fetched_at, n FROM building_fetch WHERE sgg_cd=? AND bjdong_cd=? AND plat_gb=?
                          AND bun=? AND ji=? AND kind=?""", (*parcel, kind)).fetchone()
    if not row:
        return False
    ttl = BUILDING_TTL_DAYS if row[1] else NOT_FOUND_TTL_DAYS
    return datetime.now() - datetime.fromisoformat(row[0]) < timedelta(days=ttl)


def _parcel_where(alias=""):
    p = f"{alias}." if alias else ""
    return f"{p}sgg_cd=? AND {p}bjdong_cd=? AND {p}plat_gb=? AND {p}bun=? AND {p}ji=?"


def fetch_titles(conn, parcel, jibun, key):
    """표제부(동별) 조회 → buildings 저장."""
    save_titles(conn, parcel, jibun, _bld_fetch_all("getBrTitleInfo", parcel, key))


def save_titles(conn, parcel, jibun, items):
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(str(it.get("mgmBldrgstPk") or ""), *parcel, jibun, (it.get("bldNm") or "").strip(),
             (it.get("dongNm") or "").strip(), (it.get("mainPurpsCdNm") or "").strip(),
             (it.get("etcPurps") or "").strip(), str(it.get("mainAtchGbCd") or "").strip(),
             _int(it.get("hhldCnt")), _int(it.get("hoCnt")), _int(it.get("fmlyCnt")), now) for it in items]
    with conn:
        conn.execute(f"DELETE FROM buildings WHERE {_parcel_where()}", parcel)
        conn.executemany(f"INSERT OR REPLACE INTO buildings VALUES ({','.join('?' * 16)})", rows)
        conn.execute("INSERT OR REPLACE INTO building_fetch VALUES (?, ?, ?, ?, ?, 'title', ?, ?)",
                     (*parcel, now, len(rows)))


def fetch_units(conn, parcel, key):
    """전유공용면적 조회 → 전유 호를 (건물명, 동, 용도)별로 세어 building_units 저장."""
    save_units(conn, parcel, _bld_fetch_all("getBrExposPubuseAreaInfo", parcel, key))


def save_units(conn, parcel, items):
    hos = {}
    unit_area = {}  # (건물명, 동, 용도, 층구분, 호) -> 전유면적 합 (복층 등 한 호에 전유 행이 여럿)
    for it in items:
        if str(it.get("exposPubuseGbCd") or "").strip() != "1":  # 1=전유, 2=공용
            continue
        k = ((it.get("bldNm") or "").strip(), (it.get("dongNm") or "").strip(),
             f"{(it.get('mainPurpsCdNm') or '').strip()}|{(it.get('etcPurps') or '').strip()}")
        ho = ((it.get("flrGbCdNm") or ""), (it.get("hoNm") or "").strip())
        hos.setdefault(k, set()).add(ho)
        try:
            unit_area[(*k, *ho)] = unit_area.get((*k, *ho), 0.0) + float(it.get("area") or 0)
        except (TypeError, ValueError):
            pass
    by_bin = {}
    for (b, _d, u, *_), a in unit_area.items():
        by_bin[(b, u, area_bin(a))] = by_bin.get((b, u, area_bin(a)), 0) + 1
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(f"DELETE FROM building_units WHERE {_parcel_where()}", parcel)
        conn.executemany("INSERT INTO building_units VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [(*parcel, b, d, u, len(s), now) for (b, d, u), s in hos.items()])
        conn.execute(f"DELETE FROM building_unit_areas WHERE {_parcel_where()}", parcel)
        conn.executemany("INSERT INTO building_unit_areas VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [(*parcel, b, u, ab, n, now) for (b, u, ab), n in by_bin.items()])
        conn.execute("INSERT OR REPLACE INTO building_fetch VALUES (?, ?, ?, ?, ?, 'units', ?, ?)",
                     (*parcel, now, len(hos)))


def _call(fn, *args):
    """API 호출. 미등록/한도 초과면 이번 실행 동안 다시 부르지 않는다. -> 오류 메시지 또는 None"""
    if "bld" in _blocked:
        return _blocked["bld"]
    try:
        fn(*args)
    except rt_core.ApiNotRegistered as exc:
        _blocked["bld"] = str(exc)
        return str(exc)
    except rt_core.ApiError as exc:
        if "한도" in str(exc):
            _blocked["bld"] = str(exc)
        return str(exc)
    except requests.RequestException as exc:
        return f"네트워크 오류: {exc}"
    return None


def needs_units(titles, prop):
    """전유부(용도별 호수)가 필요한가: 오피스텔이거나 표제부에 호수·세대수가 없을 때."""
    return prop == "오피스텔" or not sum((t["ho"] or 0) + (t["hh"] or 0) for t in titles)


def get_building_stock(conn, sgg_cd, umd, jibun, prop="오피스텔", name="", key=None, fetch=True,
                       fetch_expos=True):
    """fetch_expos=False면 전유부는 캐시만 쓴다 (화면에서 오래 기다리지 않게)."""
    """
    건물 전체 호수/세대수.
    -> {"stock", "stock_src", "units", "households", "units_expos", "building_name", "main_use", "note"}
       stock이 None이면 note에 사유
    """
    out = {"stock": None, "stock_src": "", "units": None, "households": None, "units_expos": None,
           "building_name": "", "main_use": "", "note": ""}
    parcel_jb = parse_parcel(jibun)
    if not parcel_jb:
        out["note"] = f"지번 형식 불명({jibun})"
        return out
    try:
        bj = bjdong_code(conn, sgg_cd, umd)
    except requests.RequestException as exc:
        out["note"] = f"법정동코드 조회 실패: {exc}"
        return out
    if not bj:
        out["note"] = f"법정동코드 없음({umd})"
        return out
    parcel = (sgg_cd, bj, *parcel_jb)
    key = key or (rt_core.load_service_key() if fetch else None)

    if fetch and not _is_fresh(conn, parcel, "title"):
        err = _call(fetch_titles, conn, parcel, jibun, key)
        if err and not _has(conn, parcel, "title"):
            out["note"] = err
            return out
    if not _has(conn, parcel, "title"):
        out["note"] = "건축물대장 미조회" if not fetch else "건축물대장 없음"
        return out

    cur = conn.execute(f"""SELECT building_name, dong_name, main_use, etc_use, main_atch, households, units
                           FROM buildings WHERE {_parcel_where()}""", parcel)
    titles = [dict(zip(["bld", "dong", "main_use", "etc_use", "atch", "hh", "ho"], r)) for r in cur.fetchall()]
    main = [t for t in titles if t["atch"] in ("0", "")] or titles
    if not main:
        out["note"] = "건축물대장 없음"
        return out

    # 한 지번에 이름이 다른 건물이 여럿이면 실거래 건물명과 맞는 것만
    names = {norm_name(t["bld"]) for t in main if norm_name(t["bld"])}
    picked_names = None
    if len(names) > 1:
        picked = [t for t in main if t["bld"] and name and name_score(t["bld"], name) >= BLD_NAME_MIN_SCORE]
        if not picked and norm_name(name) and all(norm_name(name) in n for n in names):
            picked = main  # '골든애비뉴' ↔ '골든애비뉴제1동'/'골든애비뉴제2동': 한 단지의 동들 → 합산
        if not picked:
            out["note"] = f"한 지번에 건물 {len(names)}개 - 건물명으로 특정 불가"
            return out
        main = picked
        picked_names = {t["bld"] for t in picked}

    out["building_name"] = " / ".join(sorted({t["bld"] for t in main if t["bld"]}))
    out["main_use"] = " / ".join(sorted({f"{t['main_use']}({t['etc_use']})" if t["etc_use"] else t["main_use"]
                                         for t in main if t["main_use"]}))
    out["units"] = sum(t["ho"] or 0 for t in main)
    out["households"] = sum(t["hh"] or 0 for t in main)

    # 오피스텔은 상가 호를 빼려고 전유부를 본다. 그 외는 표제부 값이 없을 때만.
    need_expos = needs_units(main, prop)
    expos_err = None
    if need_expos:
        if fetch and fetch_expos and not _is_fresh(conn, parcel, "units"):
            expos_err = _call(fetch_units, conn, parcel, key)
        elif not _has(conn, parcel, "units"):
            expos_err = "선조회 안 됨(py rt_rent.py --prefetch)"
        if _has(conn, parcel, "units"):
            cur = conn.execute(f"SELECT building_name, unit_use, n FROM building_units WHERE {_parcel_where()}",
                               parcel)
            kws = UNIT_KEYWORDS.get(prop, ())
            out["units_expos"] = sum(n for b, use, n in cur.fetchall()
                                     if (picked_names is None or b in picked_names)
                                     and any(k in use for k in kws))

    if prop == "오피스텔":
        order = [("units_expos", "전유부 오피스텔 호수"), ("units", "표제부 호수"), ("households", "표제부 세대수")]
    elif prop == "아파트":
        order = [("households", "표제부 세대수"), ("units_expos", "전유부 아파트 호수"), ("units", "표제부 호수")]
    else:
        order = [("households", "표제부 세대수"), ("units", "표제부 호수"), ("units_expos", "전유부 호수")]
    for field, label in order:
        if out[field]:
            out["stock"], out["stock_src"] = out[field], label
            break
    if out["stock"] is None:
        out["note"] = "건축물대장에 호수·세대수 없음"
    elif expos_err and out["stock_src"] != order[0][1]:
        out["note"] = f"전유부 미조회({expos_err[:40]}) - 표제부 값 사용(상가 호 포함 → 회전율 과소 가능)"
    return out


def _has(conn, parcel, kind):
    return conn.execute(f"SELECT 1 FROM building_fetch WHERE {_parcel_where()} AND kind=?",
                        (*parcel, kind)).fetchone() is not None


# ── 전월세 색인 ──────────────────────────────────────

def months_before(d, n):
    y, m = d.year, d.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


class RentIndex:
    """전월세 실거래(해제 제외)를 유형별 MarketIndex로 색인 + 수집 범위(fetch_log)."""

    def __init__(self, parts, coverage):
        self.parts = {prop: idx for prop, idx in parts if idx.size}
        self.coverage = coverage  # {(dataset, sgg_cd): {ym, ...}}
        self.size = sum(idx.size for idx in self.parts.values())

    @classmethod
    def from_db(cls, db_path=rt_core.DB_PATH, props=tuple(RENT_DATASETS)):
        conn = rt_core.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            parts = []
            for prop in props:
                ds = RENT_DATASETS[prop]
                rows = conn.execute(
                    """SELECT dataset, sgg_cd, sgg_nm, umd, jibun, name, area, floor, deal_date, deposit,
                              monthly_rent, contract_type FROM deals
                       WHERE dataset=? AND IFNULL(cancel_type,'')=''""", (ds,)).fetchall()
                parts.append((prop, MarketIndex([{**dict(r), "price": None} for r in rows])))
            coverage = {}
            for ds, sgg, ym in conn.execute("SELECT dataset, sgg_cd, ym FROM fetch_log"):
                coverage.setdefault((ds, sgg), set()).add(ym)
        finally:
            conn.close()
        return cls(parts, coverage)

    def candidates(self, loc, props):
        """(거래 목록, 매칭방식, 매칭건물명, 매칭지번, 점수, 유형)"""
        for prop in props:
            idx = self.parts.get(prop)
            if idx is None:
                continue
            res = idx.candidates(loc)
            if res[0]:
                return (*res, prop)
        return [], "미매칭", "", "", 0, ""

    def missing_months(self, prop, sgg_cd, start, end):
        have = self.coverage.get((RENT_DATASETS[prop], sgg_cd), set())
        months = rt_core.month_list(start.strftime("%Y%m"), end.strftime("%Y%m"))
        missing = [m for m in months if m not in have]
        # 진행 중인 마지막 달은 아직 안 받았어도 허용
        return [] if missing == [months[-1]] else missing


def rent_props_for(usage):
    """법원 용도 → 찾아볼 전월세 유형 순서."""
    usage = usage or ""
    if "오피스텔" in usage:
        return ("오피스텔", "아파트")
    if "아파트" in usage:
        return ("아파트", "오피스텔")
    if any(k in usage for k in ("다세대", "연립", "빌라")):
        return ("연립다세대",)
    return ("오피스텔", "아파트")


def get_rent_deals(rent_index, loc, base_date, props=("오피스텔", "아파트")):
    """
    해당 건물의 최근 12개월 월세계약.
    -> {"match": (방식, 건물명, 지번, 점수, 유형), "buildings": {(sgg_cd, 동, 지번, 이름)},
        "total", "new", "renewal", "recent6", "typed_share", "deposit_med", "rent_med",
        "start", "end", "deals", "note"}
    """
    end = base_date
    start = months_before(end, WINDOW_MONTHS) + timedelta(days=1)
    start6 = months_before(end, RECENT_MONTHS) + timedelta(days=1)
    hits, method, mname, mjibun, score, prop = rent_index.candidates(loc, props)
    out = {"match": (method, mname, mjibun, score, prop), "start": start, "end": end, "deals": [],
           "buildings": {(d["sgg_cd"], d["umd"], norm_jibun(d["jibun"]), d["name"]) for d in hits},
           "total": None, "new": None, "renewal": None, "recent6": None, "typed_share": None,
           "deposit_med": None, "rent_med": None, "note": ""}
    if not hits:
        if any(p in rent_index.parts for p in props):
            out["note"] = "전월세 자료에 해당 건물 없음"
        else:
            labels = "·".join(rt_core.DATASETS[RENT_DATASETS[p]]["label"] for p in props)
            out["note"] = f"{labels} 자료 없음(수집 필요)"
        return out
    sggs = {d["sgg_cd"] for d in hits}
    missing = sorted({m for s in sggs for m in rent_index.missing_months(prop, s, start, end)})
    if missing:
        out["note"] = f"{rt_core.DATASETS[RENT_DATASETS[prop]]['label']} 수집 안 된 월 {missing[0]}~{missing[-1]}"
        return out

    s, e, s6 = start.isoformat(), end.isoformat(), start6.isoformat()
    win = [d for d in hits if s <= (d["deal_date"] or "") <= e and (d["monthly_rent"] or 0) > 0]
    typed = [d for d in win if (d["contract_type"] or "").strip() in ("신규", "갱신")]
    out.update({
        "deals": sorted(win, key=lambda d: d["deal_date"], reverse=True),
        "total": len(win),
        "recent6": sum(1 for d in win if d["deal_date"] >= s6),
        "typed_share": round(len(typed) / len(win), 2) if win else None,
        "deposit_med": median(d["deposit"] or 0 for d in win) if win else None,
        "rent_med": median(d["monthly_rent"] for d in win) if win else None,
    })
    if not win or len(typed) / len(win) >= CONTRACT_TYPE_MIN_SHARE:
        out["new"] = sum(1 for d in typed if d["contract_type"].strip() == "신규")
        out["renewal"] = sum(1 for d in typed if d["contract_type"].strip() == "갱신")
    return out


# ── 회전율 ──────────────────────────────────────────

def calc_rent_turnover(row, rent_index, base_date=None, conn=None, fetch=True, fetch_expos=True):
    """경매 1건 -> COLUMNS dict. 확신할 수 없으면 '연 월세회전율(%)'은 비우고 회전율비고에 사유."""
    out = dict.fromkeys(COLUMNS, "")
    base_date = base_date or date.today()
    loc = auction_location(row)
    rd = get_rent_deals(rent_index, loc, base_date, rent_props_for(row.get("용도")))
    method, mname, _, score, prop = rd["match"]
    out.update({"전월세유형": prop, "월세 기준일": base_date.isoformat(),
                "월세 집계기간": f"{rd['start'].isoformat()}~{rd['end'].isoformat()}",
                "전월세매칭": method + (f" {score}" if method.endswith("건물명") else "")})
    if rd["note"]:
        out["회전율비고"] = rd["note"]
        return out
    out.update({"월세(12개월)": rd["total"], "월세(6개월)": rd["recent6"],
                "신규월세(12개월)": "" if rd["new"] is None else rd["new"],
                "월세 중위보증금(만원)": "" if rd["deposit_med"] is None else round(rd["deposit_med"]),
                "월세 중위월세(만원)": "" if rd["rent_med"] is None else round(rd["rent_med"])})

    parcels = {(sgg, umd, jb) for sgg, umd, jb, _ in rd["buildings"]}
    names = {n for *_, n in rd["buildings"]}
    if len(parcels) != 1:
        out["회전율비고"] = f"후보 건물 {len(parcels)}곳 - 특정 불가"
        return out
    sgg_cd, umd, jibun = parcels.pop()
    own = conn is None
    conn = conn or connect()
    try:
        st = get_building_stock(conn, sgg_cd, umd, jibun, prop,
                                name=next(iter(names)) if len(names) == 1 else loc["건물명"], fetch=fetch,
                                fetch_expos=fetch_expos)
    finally:
        if own:
            conn.close()
    out.update({"전체호수": st["stock"] or "", "호수출처": st["stock_src"],
                "대장건물명": st["building_name"], "대장용도": st["main_use"]})
    if not st["stock"]:
        out["회전율비고"] = st["note"]
        return out
    rate = round(rd["total"] / st["stock"] * 100, 1)
    if rate > MAX_TURNOVER:
        out["회전율비고"] = f"회전율 {rate}% - 호수 대비 계약이 너무 많음(매칭 확인 필요)"
        return out
    out["연 월세회전율(%)"] = rate
    if rd["new"] is not None:
        out["신규 월세회전율(%)"] = round(rd["new"] / st["stock"] * 100, 1)
    elif rd["total"]:
        out["회전율비고"] = f"계약구분 기재율 {rd['typed_share']:.0%} - 신규 회전율 미산출"
    if st["note"]:
        out["회전율비고"] = "; ".join(x for x in (out["회전율비고"], st["note"]) if x)
    return out


def summary_line(r):
    """'전체호수 216 / 최근12개월 월세 31건 / 연 월세회전율 14.4%'"""
    def num(v, fmt):
        try:
            return format(float(v), fmt)
        except (TypeError, ValueError):
            return None

    rate = num(r.get("연 월세회전율(%)"), ".1f")
    if rate is None or rate == "nan":
        return f"연 월세회전율 미산출 ({r.get('회전율비고') or '사유 없음'})"
    return (f"전체호수 {num(r['전체호수'], '.0f')} / 최근12개월 월세 {num(r['월세(12개월)'], '.0f')}건 / "
            f"연 월세회전율 {rate}%")


# ── 일괄 선조회 ─────────────────────────────────────

def prefetch(rows, rent_index, base_date=None, workers=6, progress=None, units=True):
    """
    경매 물건들의 건축물대장을 병렬로 미리 받아 캐시한다 (API 응답이 호출당 수 초라 순차 조회는 느림).
    rows: 경매 dict 목록. base_date: 날짜 또는 row -> 날짜 함수. 반환: 새로 조회한 지번 수.
    """
    if "bld" in _blocked:
        return 0
    conn = connect()
    try:
        parcels, props = {}, {}
        for row in rows:
            bd = base_date(row) if callable(base_date) else (base_date or date.today())
            if bd is None:
                continue
            rd = get_rent_deals(rent_index, auction_location(row), bd, rent_props_for(row.get("용도")))
            spots = {(sgg, umd, jb) for sgg, umd, jb, _ in rd["buildings"]}
            if rd["note"] or len(spots) != 1:
                continue
            sgg_cd, umd, jibun = spots.pop()
            pj = parse_parcel(jibun)
            try:
                bj = bjdong_code(conn, sgg_cd, umd) if pj else None
            except requests.RequestException:
                bj = None
            if bj:
                parcels[(sgg_cd, bj, *pj)] = jibun
                props[(sgg_cd, bj, *pj)] = rd["match"][4]
    finally:
        conn.close()
    return prefetch_parcels(parcels, props, workers, progress, units)


def prefetch_parcels(parcels, props, workers=6, progress=None, units=True, force_units=()):
    """
    지번 목록의 표제부(+필요하면 전유부)를 병렬로 받아 캐시. 캐시가 유효하면 건너뛴다.
    parcels: {(sgg_cd, bjdong_cd, plat_gb, bun, ji): 지번}, props: {같은 키: 실거래 유형}
    force_units: 캐시가 있어도 전유부를 다시 받을 지번 (예: 면적별 호수가 없는 예전 캐시)
    반환: 새로 조회한 지번 수 (표제부 + 전유부). 미등록·한도 초과면 _blocked에 남기고 멈춘다.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if "bld" in _blocked:
        return 0
    progress = progress or (lambda done, total, msg: None)
    conn = connect()
    try:
        key = rt_core.load_service_key()

        def run(kind, jobs):
            op = "getBrTitleInfo" if kind == "title" else "getBrExposPubuseAreaInfo"
            with ThreadPoolExecutor(workers) as ex:
                futs = {ex.submit(_bld_fetch_all, op, pc, key): pc for pc in jobs}
                for i, f in enumerate(as_completed(futs), 1):
                    pc = futs[f]
                    try:
                        items = f.result()
                    except rt_core.ApiNotRegistered as exc:
                        _blocked["bld"] = str(exc)
                        ex.shutdown(cancel_futures=True)
                        return
                    except (rt_core.ApiError, requests.RequestException) as exc:
                        if "한도" in str(exc):
                            _blocked["bld"] = str(exc)
                            ex.shutdown(cancel_futures=True)
                            return
                        progress(i, len(jobs), f"{kind} {pc}: {exc}")
                        continue
                    if kind == "title":
                        save_titles(conn, pc, parcels[pc], items)
                    else:
                        save_units(conn, pc, items)
                    progress(i, len(jobs), f"{'표제부' if kind == 'title' else '전유부'} {i}/{len(jobs)}")

        titles = [pc for pc in parcels if not _is_fresh(conn, pc, "title")]
        run("title", titles)
        unit_jobs = []
        for pc in parcels if units else []:
            if "bld" in _blocked or not _has(conn, pc, "title") or (
                    _is_fresh(conn, pc, "units") and pc not in force_units):
                continue
            cur = conn.execute(f"""SELECT main_use, etc_use, main_atch, households, units FROM buildings
                                   WHERE {_parcel_where()}""", pc)
            ts = [dict(zip(["main_use", "etc_use", "atch", "hh", "ho"], r)) for r in cur.fetchall()]
            main = [t for t in ts if t["atch"] in ("0", "")] or ts
            if main and needs_units(main, props[pc]):
                unit_jobs.append(pc)
        if unit_jobs and "bld" not in _blocked:
            run("units", unit_jobs)
        return len(titles) + len(unit_jobs)
    finally:
        conn.close()


# ── 검증 ────────────────────────────────────────────

def live_monthly_count(prop, buildings, start, end, key, cache):
    """국토부 API를 지금 다시 불러 같은 건물·기간 월세 건수를 센다 (DB 집계 대조용).
    buildings: get_rent_deals()의 {(sgg_cd, 동, 지번, 이름)}"""
    ds = RENT_DATASETS[prop]
    n = 0
    for sgg_cd in sorted({b[0] for b in buildings}):
        for ym in rt_core.month_list(start.strftime("%Y%m"), end.strftime("%Y%m")):
            ck = (ds, sgg_cd, ym)
            if ck not in cache:
                cache[ck] = rt_core.fetch_month(ds, sgg_cd, ym, key)
                time.sleep(rt_core.REQUEST_DELAY)
            n += sum(1 for d in cache[ck]
                     if (d["sgg_cd"], d["umd"], norm_jibun(d["jibun"]), d["name"]) in buildings
                     and not d["cancel_type"] and (d["monthly_rent"] or 0) > 0
                     and start.isoformat() <= d["deal_date"] <= end.isoformat())
    return n


def verify(n=5, prop="오피스텔", base_date=None, live=True):
    """월세 거래가 많은 건물 n곳: DB 집계 vs API 재조회, 표제부/전유부 호수, 회전율."""
    base_date = base_date or date.today()
    ds = RENT_DATASETS[prop]
    start = months_before(base_date, WINDOW_MONTHS) + timedelta(days=1)
    conn = connect()
    try:
        top = conn.execute("""
            SELECT sgg_cd, sgg_nm, umd, jibun, name, COUNT(*) c FROM deals
            WHERE dataset=? AND IFNULL(cancel_type,'')='' AND monthly_rent>0 AND deal_date BETWEEN ? AND ?
            GROUP BY sgg_cd, umd, jibun, name ORDER BY c DESC LIMIT ?""",
            (ds, start.isoformat(), base_date.isoformat(), n)).fetchall()
        if not top:
            print(f"{rt_core.DATASETS[ds]['label']} 자료가 없습니다. 먼저 py collect.py -t {ds} 로 수집하세요.")
            return []
        rent = RentIndex.from_db(props=(prop,))
        key = rt_core.load_service_key()
        cache, rows = {}, []
        for sgg_cd, sgg_nm, umd, jibun, name, _ in top:
            loc = {"구": sgg_nm, "법정동": umd, "지번": jibun, "건물명": name, "층": None, "면적": None}
            rd = get_rent_deals(rent, loc, base_date, (prop,))
            st = get_building_stock(conn, sgg_cd, umd, norm_jibun(jibun), prop, name=name)
            live_n = ""
            if live:
                try:
                    live_n = live_monthly_count(prop, rd["buildings"], start, base_date, key, cache)
                except (rt_core.ApiError, rt_core.ApiNotRegistered) as exc:
                    live_n = f"오류: {exc}"[:30]
            rate = round(rd["total"] / st["stock"] * 100, 1) if st["stock"] and rd["total"] is not None else ""
            rows.append({
                "구": sgg_nm, "법정동": umd, "지번": jibun, "건물명": name,
                "매칭": f"{rd['match'][0]}({len(rd['buildings'])}개 건물)",
                "DB 월세": rd["total"], "API 재조회": live_n,
                "일치": "O" if live_n == rd["total"] else ("" if live_n == "" else "X"),
                "표제부 호수": st["units"], "표제부 세대수": st["households"], "전유부 호수": st["units_expos"],
                "채택 호수": st["stock"], "출처": st["stock_src"], "회전율(%)": rate,
                "대장 건물명": st["building_name"], "비고": rd["note"] or st["note"],
            })
    finally:
        conn.close()
    return rows


def _print_table(rows):
    if not rows:
        return
    cols = list(rows[0])
    widths = {c: max(len(str(c)), *(len(str(r[c] if r[c] is not None else "")) for r in rows)) for c in cols}
    print(" | ".join(str(c).ljust(widths[c]) for c in cols))
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(str(r[c] if r[c] is not None else "").ljust(widths[c]) for c in cols))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="경매 물건 연 월세회전율")
    p.add_argument("--verify", type=int, metavar="N", help="월세 거래가 많은 건물 N곳 검증표")
    p.add_argument("--prop", default="오피스텔", choices=list(RENT_DATASETS), help="검증할 유형")
    p.add_argument("--no-live", action="store_true", help="검증 시 국토부 API 재조회 생략")
    p.add_argument("--case", metavar="사건번호", help="경매 아카이브에서 사건번호로 찾아 회전율 출력")
    p.add_argument("--date", help="기준일 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--prefetch", action="store_true", help="입찰 예정 물건의 건축물대장을 병렬로 미리 받기")
    args = p.parse_args()
    base = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()

    if args.prefetch:
        import rt_bid
        ups = rt_bid.load_upcoming()
        n = prefetch(ups, RentIndex.from_db(), base,
                     progress=lambda d, t, m: print(f"  [{d}/{t}] {m}") if d % 20 == 0 or d == t else None)
        print(f"입찰 예정 {len(ups)}건 → 건축물대장 새로 조회 {n}곳")
        for msg in _blocked.values():
            print(f"[건축물대장] {msg}")
        return

    if args.verify:
        _print_table(verify(args.verify, args.prop, base, live=not args.no_live))
        if "bld" in _blocked:
            print(f"\n[건축물대장] {_blocked['bld']}")
        return

    if args.case:
        import rt_bid
        import csv
        with open(rt_bid.ARCHIVE_CSV, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r["사건번호"] == args.case]
        if not rows:
            print(f"{args.case}: 경매 아카이브에 없음")
            return
        rent = RentIndex.from_db()
        seen = set()
        for r in sorted(rows, key=lambda r: (bool(r.get("지번")), r["수집일시"]), reverse=True):
            if r["물건번호"] in seen:
                continue
            seen.add(r["물건번호"])
            t = calc_rent_turnover(r, rent, base)
            print(f"{r['사건번호']}({r['물건번호']}) {r['소재지']}")
            print(f"  {summary_line(t)}")
            print("  " + ", ".join(f"{k}={v}" for k, v in t.items() if v not in ("", None)))
        return
    p.print_help()


if __name__ == "__main__":
    main()
