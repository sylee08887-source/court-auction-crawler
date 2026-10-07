"""
실거래가 검색 화면 (Streamlit).

실행: 실거래가_검색.bat 더블클릭  또는  py -m streamlit run app.py
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import rt_bid
import rt_core
import rt_officetel_market as rom
import rt_rent
from rt_core import DATASETS, KOREAN
from rt_match import MultiIndex
from rt_regions import CODE_TO_NAME, PRESETS, region_label

PYEONG = 3.3058
BASE_DIR = Path(__file__).resolve().parent
COMBINED_CSV = BASE_DIR / "combined_result.csv"
MATCHES_CSV = BASE_DIR / "combined_matches.csv"

st.set_page_config(page_title="실거래가 검색", page_icon="🏢", layout="wide")


# ── 데이터 로드 ──────────────────────────────────────

@st.cache_data(show_spinner=False)
def load_coverage():
    return pd.DataFrame(rt_core.coverage())


@st.cache_data(show_spinner="불러오는 중...")
def load_deals(dataset, sgg_codes, ym_from, ym_to):
    if not sgg_codes:
        return pd.DataFrame(columns=rt_core.COLUMNS)
    conn = sqlite3.connect(rt_core.DB_PATH)
    try:
        q = f"""SELECT * FROM deals WHERE dataset=? AND deal_ym BETWEEN ? AND ?
                AND sgg_cd IN ({','.join('?' * len(sgg_codes))})"""
        df = pd.read_sql_query(q, conn, params=[dataset, ym_from, ym_to, *sgg_codes])
    finally:
        conn.close()
    df["deal_date"] = pd.to_datetime(df["deal_date"], errors="coerce")
    if DATASETS[dataset]["kind"] == "매매":
        df["pyeong_price"] = (df["price"] / (df["area"] / PYEONG)).round(0)
    else:
        df["rent_type"] = df["monthly_rent"].fillna(0).map(lambda v: "월세" if v > 0 else "전세")
    return df


def ym_label(ym):
    return f"{ym[:4]}-{ym[4:]}"


def fmt_won(manwon):
    """만원 -> '3억 5,000만' 형식."""
    if manwon is None or pd.isna(manwon):
        return "-"
    manwon = int(round(manwon))
    eok, rest = divmod(manwon, 10000)
    if eok and rest:
        return f"{eok}억 {rest:,}만"
    if eok:
        return f"{eok}억"
    return f"{rest:,}만"


def display_frame(df, kind):
    cols = ["deal_date", "sgg_nm", "umd", "jibun", "name", "area", "floor"]
    if kind == "매매":
        cols += ["price", "pyeong_price", "build_year", "dealing_gbn", "cancel_type", "buyer", "seller"]
    else:
        cols += ["rent_type", "deposit", "monthly_rent", "contract_type", "contract_term",
                 "pre_deposit", "pre_monthly_rent", "build_year", "house_type"]
    if df["apt_dong"].fillna("").ne("").any():
        cols.insert(5, "apt_dong")
    names = {**KOREAN, "pyeong_price": "전용평당가(만원)", "rent_type": "전세/월세"}
    out = df[[c for c in cols if c in df.columns]].rename(columns=names)
    return out


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")


# ── 사이드바: 검색 조건 ──────────────────────────────

cov = load_coverage()
st.sidebar.title("🏢 실거래가 검색")

have = set(cov["dataset"]) if not cov.empty else set()
ds_options = list(DATASETS)
default_ds = "offi_trade" if "offi_trade" in have else (sorted(have)[0] if have else "offi_trade")
dataset = st.sidebar.selectbox(
    "데이터", ds_options, index=ds_options.index(default_ds),
    format_func=lambda d: DATASETS[d]["label"] + ("" if d in have else "  (데이터 없음)"),
)
kind = DATASETS[dataset]["kind"]
ds_cov = cov[cov["dataset"] == dataset] if not cov.empty else pd.DataFrame()

region_options = sorted(set(CODE_TO_NAME) | set(ds_cov.get("sgg_cd", [])), key=region_label)
stored_regions = list(ds_cov["sgg_cd"]) if not ds_cov.empty else []


def apply_preset():
    name = st.session_state["preset"]
    if name in PRESETS:
        st.session_state["regions"] = PRESETS[name]
    elif name == "수집된 지역 전체":
        st.session_state["regions"] = stored_regions


preset_names = ["직접 선택", "수집된 지역 전체", *PRESETS]
st.sidebar.selectbox("지역 묶음", preset_names, key="preset", on_change=apply_preset)
if "regions" not in st.session_state or st.session_state.get("_ds") != dataset:
    st.session_state["regions"] = stored_regions
    st.session_state["_ds"] = dataset
regions = st.sidebar.multiselect("지역 (시군구)", region_options, key="regions", format_func=region_label)

if ds_cov.empty:
    months_avail = rt_core.month_list("202401", rt_core.current_ym())
else:
    months_avail = rt_core.month_list(ds_cov["from"].min(), ds_cov["to"].max())
default_start = months_avail[max(0, len(months_avail) - 12)]
ym_from, ym_to = st.sidebar.select_slider(
    "기간", options=months_avail, value=(default_start, months_avail[-1]), format_func=ym_label)

query = st.sidebar.text_input("검색어 (단지명·동·지번, 띄어쓰기 = AND)", placeholder="예: 문래 현대홈시티")
area_rng = st.sidebar.slider("전용면적 (㎡)", 0, 300, (0, 300), step=5)
if kind == "매매":
    price_rng = st.sidebar.slider("거래금액 (억)", 0.0, 50.0, (0.0, 50.0), step=0.5)
    exclude_cancel = st.sidebar.checkbox("해제된 거래 제외", value=True)
else:
    rent_filter = st.sidebar.radio("전세/월세", ["전체", "전세", "월세"], horizontal=True)

# ── 필터 적용 ──────────────────────────────────────

raw = load_deals(dataset, tuple(regions), ym_from, ym_to)
df = raw.copy()
if query.strip():
    hay = (df["sgg_nm"].fillna("") + " " + df["umd"].fillna("") + " " + df["jibun"].fillna("")
           + " " + df["name"].fillna("")).str.replace(" ", "", regex=False)
    for tok in query.split():
        df = df[hay.loc[df.index].str.contains(tok, regex=False)]
df = df[df["area"].fillna(0).between(area_rng[0], area_rng[1] if area_rng[1] < 300 else 1e9)]
if kind == "매매":
    hi = price_rng[1] * 10000 if price_rng[1] < 50 else 1e12
    df = df[df["price"].fillna(0).between(price_rng[0] * 10000, hi)]
    if exclude_cancel:
        df = df[df["cancel_type"].fillna("") == ""]
elif rent_filter != "전체":
    df = df[df["rent_type"] == rent_filter]
df = df.sort_values("deal_date", ascending=False)

# ── 화면 ───────────────────────────────────────────

tab_search, tab_complex, tab_trend, tab_bid, tab_auction, tab_market, tab_update = st.tabs(
    ["🔎 거래 검색", "🏢 단지별", "📈 추이", "🎯 입찰 도우미", "⚖️ 경매 vs 시세", "🛡️ 오피스텔 시장", "⬇️ 데이터 업데이트"])


def show_metrics(d):
    c = st.columns(4)
    c[0].metric("거래 건수", f"{len(d):,}건")
    if kind == "매매":
        c[1].metric("중위 거래금액", fmt_won(d["price"].median()))
        c[2].metric("중위 전용평당가", fmt_won(d["pyeong_price"].median()))
        c[3].metric("최근 계약일", d["deal_date"].max().strftime("%Y-%m-%d") if len(d) else "-")
    else:
        jeonse, wolse = d[d["rent_type"] == "전세"], d[d["rent_type"] == "월세"]
        c[1].metric("전세 중위 보증금", fmt_won(jeonse["deposit"].median()), f"{len(jeonse):,}건", delta_color="off")
        c[2].metric("월세 중위 (보증금/월세)",
                    f"{fmt_won(wolse['deposit'].median())} / {fmt_won(wolse['monthly_rent'].median())}",
                    f"{len(wolse):,}건", delta_color="off")
        c[3].metric("최근 계약일", d["deal_date"].max().strftime("%Y-%m-%d") if len(d) else "-")


def complex_detail(sub, title):
    st.markdown(f"#### {title}")
    show_metrics(sub)
    ycol = "price" if kind == "매매" else "deposit"
    ytitle = "거래금액(만원)" if kind == "매매" else "보증금(만원)"
    chart = alt.Chart(sub.dropna(subset=["deal_date", ycol])).mark_circle(size=70).encode(
        x=alt.X("deal_date:T", title="계약일"),
        y=alt.Y(f"{ycol}:Q", title=ytitle, scale=alt.Scale(zero=False)),
        color=alt.Color("area:Q", title="전용㎡", scale=alt.Scale(scheme="viridis")),
        tooltip=[alt.Tooltip("deal_date:T", title="계약일"), alt.Tooltip("name:N", title="단지"),
                 alt.Tooltip("area:Q", title="전용㎡"), alt.Tooltip("floor:Q", title="층"),
                 alt.Tooltip(f"{ycol}:Q", title=ytitle, format=",")],
    ).properties(height=320).interactive()
    st.altair_chart(chart, width="stretch")
    st.dataframe(display_frame(sub, kind), hide_index=True, width="stretch")


with tab_search:
    if raw.empty:
        st.info("선택한 조건에 저장된 데이터가 없습니다. '⬇️ 데이터 업데이트' 탭에서 먼저 수집하세요.")
    else:
        show_metrics(df)
        st.caption(f"{DATASETS[dataset]['label']} · {ym_label(ym_from)} ~ {ym_label(ym_to)} · "
                   f"행을 클릭하면 같은 건물의 전체 거래 이력이 아래에 표시됩니다.")
        view = display_frame(df, kind)
        event = st.dataframe(view, hide_index=True, width="stretch", height=420,
                             on_select="rerun", selection_mode="single-row", key="deal_table")
        st.download_button("CSV 다운로드", csv_bytes(view), file_name=f"{dataset}_{ym_from}_{ym_to}.csv",
                           mime="text/csv")
        if event.selection.rows:
            row = df.iloc[event.selection.rows[0]]
            hist = raw[(raw["sgg_cd"] == row["sgg_cd"]) & (raw["jibun"] == row["jibun"])
                       & (raw["name"] == row["name"])].sort_values("deal_date", ascending=False)
            if kind == "매매":
                hist = hist[hist["cancel_type"].fillna("") == ""]
            complex_detail(hist, f"{row['name'] or '(이름 없음)'} — {row['sgg_nm']} {row['umd']} {row['jibun']}")

with tab_complex:
    if df.empty:
        st.info("조건에 맞는 거래가 없습니다.")
    else:
        keys = ["sgg_nm", "umd", "jibun", "name"]
        if kind == "매매":
            g = df.groupby(keys, dropna=False).agg(
                거래수=("price", "size"), 최근계약일=("deal_date", "max"),
                중위거래금액=("price", "median"), 중위전용평당가=("pyeong_price", "median"),
                최소면적=("area", "min"), 최대면적=("area", "max"), 건축년도=("build_year", "max"))
        else:
            g = df.groupby(keys, dropna=False).agg(
                거래수=("deposit", "size"), 최근계약일=("deal_date", "max"),
                중위보증금=("deposit", "median"), 중위월세=("monthly_rent", "median"),
                최소면적=("area", "min"), 최대면적=("area", "max"), 건축년도=("build_year", "max"))
        g = g.reset_index().rename(columns=KOREAN).sort_values("거래수", ascending=False)
        g["최근계약일"] = g["최근계약일"].dt.strftime("%Y-%m-%d")
        st.caption(f"{len(g):,}개 건물 · 행을 클릭하면 상세 이력이 표시됩니다.")
        ev = st.dataframe(g.round(1), hide_index=True, width="stretch", height=420,
                          on_select="rerun", selection_mode="single-row", key="complex_table")
        st.download_button("CSV 다운로드", csv_bytes(g), file_name=f"{dataset}_단지별.csv", mime="text/csv")
        if ev.selection.rows:
            sel = g.iloc[ev.selection.rows[0]]
            sub = df[(df["sgg_nm"] == sel["시군구"]) & (df["umd"] == sel["법정동"])
                     & (df["jibun"] == sel["지번"]) & (df["name"] == sel["단지·건물명"])]
            complex_detail(sub, f"{sel['단지·건물명'] or '(이름 없음)'} — {sel['시군구']} {sel['법정동']} {sel['지번']}")

with tab_trend:
    if df.empty:
        st.info("조건에 맞는 거래가 없습니다.")
    else:
        m = df.copy()
        m["월"] = pd.to_datetime(m["deal_ym"], format="%Y%m")
        if kind == "매매":
            agg = m.groupby(["월", "sgg_nm"]).agg(값=("pyeong_price", "median"), 건수=("price", "size")).reset_index()
            ytitle = "중위 전용평당가(만원)"
        else:
            agg = m[m["rent_type"] == "전세"].groupby(["월", "sgg_nm"]).agg(
                값=("deposit", "median"), 건수=("deposit", "size")).reset_index()
            ytitle = "전세 중위 보증금(만원)"
        base = alt.Chart(agg).encode(x=alt.X("월:T", title=None),
                                     color=alt.Color("sgg_nm:N", title="시군구"))
        st.altair_chart(base.mark_line(point=True).encode(
            y=alt.Y("값:Q", title=ytitle, scale=alt.Scale(zero=False)),
            tooltip=["sgg_nm", alt.Tooltip("월:T", format="%Y-%m"), alt.Tooltip("값:Q", format=","), "건수"],
        ).properties(height=320, title=ytitle), width="stretch")
        st.altair_chart(base.mark_bar().encode(
            y=alt.Y("sum(건수):Q", title="거래 건수"), tooltip=["sgg_nm", "건수"],
        ).properties(height=220, title="월별 거래 건수"), width="stretch")
        st.caption("최근 1~2개월은 신고 기한(계약 후 30일) 때문에 건수가 적게 집계됩니다.")

@st.cache_resource(show_spinner="실거래 색인 만드는 중...")
def market_index():
    return MultiIndex.from_db()


@st.cache_resource(show_spinner="전월세 색인 만드는 중...")
def rent_index():
    return rt_rent.RentIndex.from_db()


TURN_NUM_COLS = ["전체호수", "월세(12개월)", "신규월세(12개월)", "연 월세회전율(%)", "신규 월세회전율(%)",
                 "월세(6개월)", "월세 중위보증금(만원)", "월세 중위월세(만원)"]


@st.cache_data(show_spinner="입찰 예정 물건 평가 중... (처음엔 건축물대장 조회로 몇 분 걸릴 수 있음)")
def bid_table():
    if not (BASE_DIR / rt_bid.ARCHIVE_CSV).exists():
        return pd.DataFrame(), {}
    upcoming = rt_bid.load_upcoming(BASE_DIR / rt_bid.ARCHIVE_CSV)
    # 표제부만 선조회. 전유부(오피스텔 호수)는 호출이 많아 `py rt_bid.py` 또는 `py rt_rent.py --prefetch`로 받아 둔 캐시만 쓴다
    rt_rent.prefetch(upcoming, rent_index(), units=False)
    conn = rt_rent.connect()
    try:
        rows, comps = rt_bid.evaluate(upcoming, market_index(), bid_history(), rent_index(), conn,
                                      fetch_expos=False)
    finally:
        conn.close()
    df = pd.DataFrame(rows)
    for c in ["층", "전용면적(㎡)", "유찰횟수", "최저가/시세(%)", "감정가/시세(%)", "시점보정(평균)", *TURN_NUM_COLS]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df, {f"{k[0]}|{k[1]}": v for k, v in comps.items()}


@st.cache_resource
def bid_history():
    return rt_bid.History(BASE_DIR / rt_bid.HISTORY_CSV)


ACQ_TAX_RATE = 0.046  # 오피스텔(비주택) 취득세+지방교육세+농특세 합계 기준

with tab_bid:
    bids, comps = bid_table()
    if bids.empty:
        st.info("입찰 예정 물건이 없습니다. '⬇️ 데이터 업데이트' 탭에서 경매 수집을 실행하세요.")
    else:
        st.caption("예상 낙찰가 = 최저가 × 과거 같은 구·유찰횟수 물건의 (낙찰가/최저가) 하위25%·중위·상위25%. "
                   "추정시세 = 같은 건물·비슷한 면적 실거래(시점 보정). "
                   "권리관계(대항력 있는 임차인 등)는 반영되지 않습니다.")
        g1, g2, g3, g4 = st.columns([2, 3, 2, 2])
        b_gu = g1.multiselect("구", sorted(bids["구"].dropna().unique()), key="bid_gu")
        judge_all = bids["판단"].fillna("").replace("", "(일반)")
        judge_opts = list(judge_all.value_counts().index)
        b_judge = g2.multiselect("판단", judge_opts, key="bid_judge",
                                 default=[j for j in judge_opts if j.startswith(("시세 대비 저가", "저가 예상"))])
        b_conf = g3.multiselect("시세 신뢰도", ["상", "중", "하"], default=["상", "중", "하"], key="bid_conf")
        b_max = g4.number_input("최저가 상한(만원, 0=없음)", min_value=0, value=0, step=1000, key="bid_max")
        dts = pd.to_datetime(bids["매각기일"])
        dmin, dmax = dts.min().date(), dts.max().date()
        b_dates = st.slider("매각기일", min_value=dmin, max_value=dmax, value=(dmin, dmax), key="bid_dates")

        v = bids.assign(판단=judge_all)
        if b_gu:
            v = v[v["구"].isin(b_gu)]
        if b_judge:
            v = v[v["판단"].isin(b_judge)]
        if b_conf:
            v = v[v["시세신뢰도"].isin(b_conf) | v["시세신뢰도"].fillna("").eq("")]
        if b_max:
            v = v[v["최저입찰가(만원)"] <= b_max]
        v = v[pd.to_datetime(v["매각기일"]).dt.date.between(*b_dates)]
        v = v.sort_values(["예상중위/시세(%)", "매각기일"], na_position="last")

        c = st.columns(4)
        c[0].metric("입찰 예정 (필터 후)", f"{len(v):,} / {len(bids):,}건")
        c[1].metric("시세 대비 저가 예상", f"{(judge_all == '시세 대비 저가 예상').sum():,}건")
        c[2].metric("권리관계 확인 필요", f"{judge_all.str.startswith('⚠️').sum():,}건")
        c[3].metric("최저가 ≥ 시세", f"{judge_all.str.startswith('최저가 ≥').sum():,}건")

        cols = ["매각기일", "사건번호", "물건번호", "판단", "구", "법정동", "건물명", "층", "전용면적(㎡)", "유찰횟수",
                "감정가(만원)", "최저입찰가(만원)", "예상낙찰가_하(만원)", "예상낙찰가_중(만원)", "예상낙찰가_상(만원)",
                "추정시세(만원)", "예상중위/시세(%)", "최저가/시세(%)", "시세신뢰도", "비교기간",
                "전체호수", "월세(12개월)", "연 월세회전율(%)", "소재지"]
        ev = st.dataframe(v[cols], hide_index=True, width="stretch", height=420, on_select="rerun",
                          selection_mode="single-row", key="bid_table")
        st.download_button("CSV 다운로드", csv_bytes(v[cols]), file_name="입찰예정.csv", mime="text/csv")

        if ev.selection.rows:
            r = v.iloc[ev.selection.rows[0]]
            minimum = r["최저입찰가(만원)"]
            market = r["추정시세(만원)"]
            st.markdown(f"### {r['사건번호']} ({r['물건번호']}) · {r['건물명'] or ''} {r['전용면적(㎡)']}㎡")
            st.caption(f"{r['소재지']} · 매각기일 {r['매각기일']} · 유찰 {r['유찰횟수']}회 · "
                       "법원경매정보(courtauction.go.kr)에서 사건번호로 매각물건명세서를 확인하세요.")
            if str(r["판단"]).startswith("⚠️"):
                st.error("유찰이 많거나 최저가가 시세보다 지나치게 낮습니다. 대항력 있는 임차인의 보증금 인수, "
                         "선순위 권리 등으로 실제 부담이 낙찰가보다 클 가능성이 높습니다.")
            else:
                st.warning("이 계산에는 권리관계가 반영되지 않습니다. 입찰 전 매각물건명세서·등기부·전입세대열람으로 "
                           "인수할 권리가 없는지 반드시 확인하세요.")
            k = st.columns(4)
            k[0].metric("최저입찰가", fmt_won(minimum), f"보증금 {fmt_won(r['입찰보증금(만원)'])}", delta_color="off")
            k[1].metric("예상 낙찰가(중위)", fmt_won(r["예상낙찰가_중(만원)"]),
                        f"{fmt_won(r['예상낙찰가_하(만원)'])} ~ {fmt_won(r['예상낙찰가_상(만원)'])}", delta_color="off")
            k[2].metric("추정시세", fmt_won(market),
                        f"신뢰도 {r['시세신뢰도'] or '-'} · {r['비교기간'] or ''}", delta_color="off")
            k[3].metric("감정가", fmt_won(r["감정가(만원)"]))

            st.markdown("#### 임대 수요 (연 월세회전율)")
            t = st.columns(4)
            turn = r["연 월세회전율(%)"]
            t[0].metric("연 월세회전율", f"{turn:.1f}%" if pd.notna(turn) else "미산출",
                        f"신규 {r['신규 월세회전율(%)']:.1f}%" if pd.notna(r["신규 월세회전율(%)"]) else None,
                        delta_color="off")
            t[1].metric("전체 호수", f"{r['전체호수']:,.0f}호" if pd.notna(r["전체호수"]) else "-",
                        r["호수출처"] or None, delta_color="off")
            t[2].metric("최근 12개월 월세", f"{r['월세(12개월)']:,.0f}건" if pd.notna(r["월세(12개월)"]) else "-",
                        f"최근 6개월 {r['월세(6개월)']:,.0f}건" if pd.notna(r["월세(6개월)"]) else None,
                        delta_color="off")
            t[3].metric("월세 중위 (보증금/월세)",
                        f"{fmt_won(r['월세 중위보증금(만원)'])} / {fmt_won(r['월세 중위월세(만원)'])}"
                        if pd.notna(r["월세 중위월세(만원)"]) else "-", delta_color="off")
            st.caption(f"{rt_rent.summary_line(r)} · 집계기간 {r['월세 집계기간']} · "
                       f"전월세 매칭 {r['전월세매칭'] or '-'} ({r['전월세유형'] or '-'}) · "
                       f"건축물대장 {r['대장건물명'] or '-'} {r['대장용도'] or ''}"
                       + (f" · ⚠️ {r['회전율비고']}" if r["회전율비고"] else "")
                       + " · 월세회전율 = 최근 12개월 월세계약(보증부월세 포함, 전세·해제 제외) ÷ 전체 호수. "
                         "최근 1개월은 신고 기한 때문에 덜 잡힙니다.")

            st.markdown("#### 입찰가 계산기")
            floor_bid = int(minimum) if pd.notna(minimum) else 0
            mid = r["예상낙찰가_중(만원)"]
            default_bid = max(int(mid) if pd.notna(mid) else floor_bid, floor_bid)
            my_bid = st.number_input("내 입찰가 (만원)", min_value=floor_bid, value=default_bid, step=100,
                                     key=f"mybid_{r['사건번호']}_{r['물건번호']}")
            hist = bid_history()
            bucket = rt_bid.yuchal_bucket(r["유찰횟수"])
            to_min = my_bid / minimum * 100 if floor_bid else None
            share, basis = rt_bid.win_share(hist, r["구"], bucket, to_min) if to_min else (None, "")
            tax = my_bid * ACQ_TAX_RATE
            kc = st.columns(4)
            kc[0].metric("최저가 대비", f"{to_min:.1f}%" if to_min else "-")
            kc[1].metric("시세 대비", f"{my_bid / market * 100:.1f}%" if pd.notna(market) and market else "-")
            kc[2].metric("과거 낙찰가보다 높았을 비율", f"{share:.0f}%" if share is not None else "-",
                         basis, delta_color="off")
            kc[3].metric("취득세 포함 총액(추정)", fmt_won(my_bid + tax), f"취득세 등 4.6% {fmt_won(tax)}",
                         delta_color="off")
            st.caption("'과거 낙찰가보다 높았을 비율' = 같은 조건 과거 낙찰 사례 중 (낙찰가/최저가)가 내 입찰가 비율 이하였던 비중. "
                       "낙찰 확률의 대략적 참고치이며 경쟁자 수는 반영하지 않습니다. 취득세율은 오피스텔(비주택) 일반 기준이며 "
                       "주택 수·감면 여부에 따라 다릅니다.")

            s_hist, _ = hist.samples(rt_bid.RATIO_COL, r["구"], bucket)
            if not s_hist.empty and to_min:
                hd = pd.DataFrame({"ratio": s_hist.values})
                bars = alt.Chart(hd).mark_bar(opacity=0.7).encode(
                    x=alt.X("ratio:Q", bin=alt.Bin(step=2), title="과거 낙찰가/최저가(%)"),
                    y=alt.Y("count():Q", title="건수"))
                rule = alt.Chart(pd.DataFrame({"x": [to_min]})).mark_rule(color="red", size=2).encode(x="x:Q")
                st.altair_chart((bars + rule).properties(height=200, title=f"과거 분포 ({basis}) · 빨간선 = 내 입찰가"),
                                width="stretch")

            used = comps.get(f"{r['사건번호']}|{r['물건번호']}", [])
            if used:
                cd = pd.DataFrame(used)
                cd["㎡당가(보정)"] = (cd["price"] * cd["adj"] / cd["area"]).round(0)
                cd = cd.rename(columns={"deal_date": "계약일", "name": "건물명", "area": "전용면적(㎡)",
                                        "price": "거래금액(만원)", "floor": "층", "adj": "시점보정", "jibun": "지번"})
                st.markdown(f"#### 시세 산출에 쓴 실거래 ({len(cd)}건 · {r['시세산출']})")
                st.dataframe(cd[["계약일", "건물명", "지번", "전용면적(㎡)", "층", "거래금액(만원)", "시점보정", "㎡당가(보정)"]]
                             .sort_values("계약일", ascending=False), hide_index=True, width="stretch")

with tab_auction:
    if not COMBINED_CSV.exists():
        st.info("combined_result.csv가 없습니다. '⬇️ 데이터 업데이트' 탭에서 경매 분석을 실행하세요.")
    else:
        ca = pd.read_csv(COMBINED_CSV, encoding="utf-8-sig", dtype=str)
        if "추정시세(만원)" not in ca.columns:
            st.warning("이전 형식의 결과 파일입니다. '⬇️ 데이터 업데이트' 탭에서 경매 분석을 다시 실행하세요.")
        else:
            num_cols = ["감정가(만원)", "최저입찰가(만원)", "낙찰가(만원)", "낙찰가율(%)", "유찰횟수", "전용면적(㎡)",
                        "추정시세(만원)", "비교거래수", "낙찰가/시세(%)", "최저가/시세(%)", "감정가/시세(%)"]
            turn_cols = [c for c in ["전체호수", "월세(12개월)", "연 월세회전율(%)", "회전율비고"] if c in ca.columns]
            for c in num_cols + [c for c in turn_cols if c in TURN_NUM_COLS]:
                ca[c] = pd.to_numeric(ca[c], errors="coerce")
            cols = ["매각기일", "사건번호", "물건번호", "구", "법정동", "건물명", "층", "전용면적(㎡)",
                    "감정가(만원)", "낙찰가(만원)", "낙찰가율(%)", "유찰횟수", "추정시세(만원)", "낙찰가/시세(%)",
                    "감정가/시세(%)", *turn_cols, "매칭방식", "실거래유형", "매칭건물명", "시세산출", "비교기간",
                    "매칭비고", "소재지"]
            f1, f2, f3 = st.columns([3, 2, 2])
            a_query = f1.text_input("소재지·건물명 검색", key="auction_q")
            a_gu = f2.multiselect("구", sorted(ca["구"].dropna().unique()), key="auction_gu")
            a_mode = f3.radio("표시", ["시세 산출된 건", "전체", "미매칭만"], horizontal=True, key="auction_mode")
            s = ca
            if a_mode == "시세 산출된 건":
                s = s[s["추정시세(만원)"].notna()]
            elif a_mode == "미매칭만":
                s = s[s["추정시세(만원)"].isna()]
            if a_gu:
                s = s[s["구"].isin(a_gu)]
            for tok in a_query.split():
                hay = s["소재지"].fillna("") + s["건물명"].fillna("") + s["매칭건물명"].fillna("")
                s = s[hay.str.contains(tok, regex=False)]
            s = s[cols].sort_values("매각기일", ascending=False)

            c = st.columns(4)
            c[0].metric("물건 수", f"{len(s):,}")
            c[1].metric("시세 산출률(전체)", f"{ca['추정시세(만원)'].notna().mean() * 100:.0f}%")
            c[2].metric("중위 낙찰가율(감정가 대비)", f"{s['낙찰가율(%)'].median():.1f}%" if len(s) else "-")
            c[3].metric("중위 낙찰가/시세", f"{s['낙찰가/시세(%)'].median():.1f}%" if s["낙찰가/시세(%)"].notna().any() else "-")
            st.caption("시세 = 매각기일 전후 같은 건물·비슷한 면적(±10%) 거래의 ㎡당가 중위 × 경매 전용면적. "
                       "행을 클릭하면 시세 산출에 쓴 실거래가 표시됩니다.")
            ev = st.dataframe(s, hide_index=True, width="stretch", height=420, on_select="rerun",
                              selection_mode="single-row", key="auction_table")
            st.download_button("CSV 다운로드", csv_bytes(s), file_name="경매_vs_시세.csv", mime="text/csv")
            if ev.selection.rows:
                sel = s.iloc[ev.selection.rows[0]]
                st.markdown(f"#### {sel['사건번호']} ({sel['물건번호']}) · {sel['소재지']}")
                k = st.columns(4)
                k[0].metric("낙찰가", fmt_won(sel["낙찰가(만원)"]))
                k[1].metric("추정시세", fmt_won(sel["추정시세(만원)"]))
                k[2].metric("감정가", fmt_won(sel["감정가(만원)"]))
                k[3].metric("낙찰가/시세", f"{sel['낙찰가/시세(%)']:.1f}%" if pd.notna(sel["낙찰가/시세(%)"]) else "-")
                if "연 월세회전율(%)" in sel.index:
                    st.caption(f"매각기일 기준 {rt_rent.summary_line(sel.fillna('').to_dict())}")
                if MATCHES_CSV.exists():
                    mt = pd.read_csv(MATCHES_CSV, encoding="utf-8-sig", dtype=str)
                    mt = mt[(mt["사건번호"] == sel["사건번호"]) & (mt["물건번호"] == sel["물건번호"])
                            & (mt["매각기일"] == sel["매각기일"])]
                    st.dataframe(mt.drop(columns=["사건번호", "물건번호", "매각기일"]), hide_index=True, width="stretch")


MARKET_METRICS = {
    "가격방어": ["mature_price_cagr", "price_cagr_5y", "price_cagr_3y", "mdd_5y", "mdd_3y", "positive_year_ratio"],
    "매매유동성": ["trade_turnover_12m", "trade_turnover_24m_ann", "trade_turnover_36m_ann", "active_trade_month_ratio",
                "max_trade_month_share"],
    "임대유동성": ["rent_turnover_12m", "monthly_rent_turnover_12m", "new_contract_turnover_12m", "active_rent_month_ratio"],
    "수익률": ["gross_yield", "gross_yield_equiv"],
}
MARKET_ALL = [m for ms in MARKET_METRICS.values() for m in ms]
MARKET_LABEL = {
    "mature_price_cagr": "성숙기 가격 CAGR(%)", "price_cagr_5y": "5년 가격 CAGR(%)", "price_cagr_3y": "3년 가격 CAGR(%)",
    "mdd_5y": "5년 MDD(%)", "mdd_3y": "3년 MDD(%)", "positive_year_ratio": "상승 연도 비율",
    "trade_turnover_12m": "매매회전율 12m(%)", "trade_turnover_24m_ann": "매매회전율 24m 연환산(%)",
    "trade_turnover_36m_ann": "매매회전율 36m 연환산(%)", "active_trade_month_ratio": "거래 있는 달 비율",
    "max_trade_month_share": "최대 월 거래 비중", "rent_turnover_12m": "임대회전율 12m(%)",
    "monthly_rent_turnover_12m": "월세회전율 12m(%)", "new_contract_turnover_12m": "신규계약 회전율 12m(%)",
    "active_rent_month_ratio": "임대 있는 달 비율", "gross_yield": "월세수익률(%)", "gross_yield_equiv": "환산수익률(%)",
}
MARKET_NUM = ["stock_units", "연식", "건축년도", "trade_n_12m", "rent_n_12m", "top1_stock_share", "top3_stock_share",
              "sample_n_price", "sample_n_rent", "initial_supply_event", "chain_index", "yoy_change_median", "is_T",
              "n_pairs", "age"]


@st.cache_data(show_spinner="시장 패널 불러오는 중...")
def load_market(mtime):
    out = {}
    for name, f in [("complex", rom.OUT_COMPLEX), ("dong", rom.OUT_DONG), ("gu", rom.OUT_GU), ("age", rom.OUT_AGE),
                    ("pareto", rom.OUT_PARETO)]:
        path = BASE_DIR / f
        df = pd.read_csv(path, encoding="utf-8-sig", dtype={"시군구코드": str, "지번": str}) \
            if path.exists() and path.stat().st_size > 5 else pd.DataFrame()
        for c in df.columns:
            if c in MARKET_ALL or c in MARKET_NUM or c.endswith("_median"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
        out[name] = df
    return out


def _fmt(v, spec, suffix=""):
    return f"{v:{spec}}{suffix}" if pd.notna(v) else "-"


with tab_market:
    mk_path = BASE_DIR / rom.OUT_COMPLEX
    if not mk_path.exists():
        st.info("시장 패널이 없습니다. 터미널에서 순서대로 실행하세요:\n\n"
                "`py rt_officetel_market.py --collect --from 201501` → `py rt_officetel_market.py --build-stock` → "
                "`py rt_officetel_market.py`")
    else:
        mk = load_market(mk_path.stat().st_mtime)
        cx = mk["complex"]
        st.caption(f"기준일 {cx['asof'].iloc[0] if len(cx) else '-'} · 성숙 연차 T = "
                   f"{cx['maturity_age'].iloc[0] if len(cx) else '-'} · 회전율 = 거래(계약)건수 ÷ 오피스텔 호수(건축물대장). "
                   "임대회전율은 공실률이 아니라 임대유동성. **종합점수는 아직 없음** — raw 지표와 분포만 봅니다.")
        f1, f2, f3, f4, f5 = st.columns([1.2, 2, 2, 1.3, 1.3])
        level = f1.radio("단위", ["법정동", "구", "단지"], key="mk_level")
        m_gu = f2.multiselect("구", sorted(cx["시군구"].dropna().unique()), key="mk_gu")
        dong_pool = cx[cx["시군구"].isin(m_gu)] if m_gu else cx
        m_dong = f3.multiselect("법정동", sorted(dong_pool["법정동"].dropna().unique()), key="mk_dong")
        m_bin = f4.selectbox("면적군(㎡)", [rom.ALL, *rom.AREA_LABELS], key="mk_bin")
        min_stock = f5.number_input("최소 재고(호)", min_value=0, value=50 if level == "단지" else 300, step=50,
                                    key=f"mk_min_{level}")
        base = {"단지": cx, "법정동": mk["dong"], "구": mk["gu"]}[level]
        v = base[base["area_bin"] == m_bin] if len(base) else base
        if m_gu:
            v = v[v["시군구"].isin(m_gu)]
        if m_dong and level != "구":
            v = v[v["법정동"].isin(m_dong)]
        v = v[v["stock_units"].fillna(0) >= min_stock]
        if level == "단지" and len(cx):
            g1, g2 = st.columns(2)
            ages = cx["연식"].dropna()
            if len(ages):
                lo, hi = int(ages.min()), int(ages.max())
                age_rng = g1.slider("연식(년)", lo, hi, (lo, hi), key="mk_age")
                v = v[v["연식"].between(*age_rng)]
            quals = sorted(cx["stock_quality"].dropna().unique())
            qual = g2.multiselect("재고 품질", quals, default=[q for q in quals if q[0] in "AB"], key="mk_qual")
            if qual:
                v = v[v["stock_quality"].isin(qual)]

        st.markdown(f"#### 분포 ({level} {len(v):,}곳)")
        dist = []
        for fam, ms in MARKET_METRICS.items():
            for m in ms:
                s = v[m].dropna() if m in v.columns else pd.Series(dtype=float)
                if len(s):
                    dist.append({"구분": fam, "지표": MARKET_LABEL[m], "n": len(s),
                                 **{f"p{q}": round(s.quantile(q / 100), 2) for q in (10, 25, 50, 75, 90)}})
        st.dataframe(pd.DataFrame(dist), hide_index=True, width="stretch")

        opts = [m for m in MARKET_ALL if m in v.columns]
        if opts:
            s1, s2 = st.columns(2)
            xm = s1.selectbox("X축", opts, index=opts.index("trade_turnover_12m"), format_func=MARKET_LABEL.get,
                              key="mk_x")
            ym_ = s2.selectbox("Y축", opts, index=opts.index("mature_price_cagr"), format_func=MARKET_LABEL.get,
                               key="mk_y")
            pts = v.dropna(subset=[xm, ym_])
            if len(pts):
                tips = [c for c in ["시군구", "법정동", "단지명", "stock_units", xm, ym_, "gross_yield", "rent_turnover_12m"]
                        if c in pts.columns]
                st.altair_chart(alt.Chart(pts).mark_circle(opacity=0.7).encode(
                    x=alt.X(f"{xm}:Q", title=MARKET_LABEL[xm]), y=alt.Y(f"{ym_}:Q", title=MARKET_LABEL[ym_]),
                    size=alt.Size("stock_units:Q", title="재고(호)"), color=alt.Color("시군구:N", legend=None),
                    tooltip=list(dict.fromkeys(tips))).properties(height=380).interactive(), width="stretch")
                st.caption(f"{len(pts):,}곳 표시 (두 지표가 모두 있는 곳). 원 크기 = 오피스텔 호수.")

            st.markdown("#### 지표별 정렬")
            sort_by = st.selectbox("정렬 기준", opts, index=opts.index("mature_price_cagr"),
                                   format_func=MARKET_LABEL.get, key="mk_sort")
            id_cols = {"단지": ["시군구", "법정동", "단지명", "지번", "건축년도", "연식", "stock_quality"],
                       "법정동": ["시군구", "법정동", "n_complexes", "top1_stock_share", "top3_stock_share"],
                       "구": ["시군구", "n_complexes", "top1_stock_share", "top3_stock_share"]}[level]
            show = [c for c in id_cols + ["stock_units"] + opts + ["trade_n_12m", "rent_n_12m", "sample_n_price",
                                                                   "confidence", "error"] if c in v.columns]
            table = v.sort_values(sort_by, ascending=sort_by == "max_trade_month_share", na_position="last")[show]
            ev = st.dataframe(table.rename(columns=MARKET_LABEL), hide_index=True, width="stretch", height=380,
                              on_select="rerun", selection_mode="single-row", key=f"mk_table_{level}")
            st.download_button("CSV 다운로드", csv_bytes(table), file_name=f"오피스텔시장_{level}.csv", mime="text/csv")

            if level == "단지" and ev.selection.rows:
                r = table.iloc[ev.selection.rows[0]]
                row = cx[(cx["시군구"] == r["시군구"]) & (cx["법정동"] == r["법정동"]) & (cx["지번"] == r["지번"])
                         & (cx["area_bin"] == m_bin)].iloc[0]
                st.markdown(f"### {r['단지명']} — {r['시군구']} {r['법정동']} {r['지번']} (준공 {r['건축년도']})")
                k = st.columns(4)
                k[0].metric("오피스텔 호수", _fmt(row["stock_units"], ",.0f"), row["stock_quality"], delta_color="off")
                k[1].metric("매매회전율 12m", _fmt(row["trade_turnover_12m"], ".1f", "%"),
                            f"{_fmt(row['trade_n_12m'], '.0f')}건", delta_color="off")
                k[2].metric("임대회전율 12m", _fmt(row["rent_turnover_12m"], ".1f", "%"),
                            f"월세 {_fmt(row['monthly_rent_turnover_12m'], '.1f', '%')}", delta_color="off")
                k[3].metric("월세수익률", _fmt(row["gross_yield"], ".2f", "%"),
                            f"환산 {_fmt(row['gross_yield_equiv'], '.2f', '%')} (전환율 {row['conversion_rate']})",
                            delta_color="off")
                st.caption("가격방어: " + " · ".join(f"{MARKET_LABEL[m]} {row[m]}" for m in MARKET_METRICS["가격방어"]
                                                     if pd.notna(row[m])) + f" · 가격 신뢰도 {row['price_confidence']}"
                           + (" · ⚠️ 분양·입주 초기 대량거래 있음(가격 산정 제외)" if row["initial_supply_event"] == 1 else "")
                           + (f" · ⚠️ {row['error']}" if isinstance(row["error"], str) and row["error"] else ""))
                deals = pd.DataFrame(rom.complex_deals(row["시군구코드"], r["법정동"], str(r["지번"])))
                if len(deals):
                    deals["deal_date"] = pd.to_datetime(deals["deal_date"])
                    if m_bin != rom.ALL:
                        deals = deals[deals["area_bin"] == m_bin]
                    tr = deals[deals["dataset"] == "offi_trade"].dropna(subset=["price"]).copy()
                    if len(tr):
                        tr["㎡당가"] = tr["price"] / tr["area"]
                        tr["분기"] = tr["deal_date"].dt.to_period("Q").dt.start_time
                        qm = tr.groupby("분기").agg(중위=("㎡당가", "median"), 건수=("㎡당가", "size")).reset_index()
                        dots = alt.Chart(tr).mark_circle(size=25, opacity=0.35).encode(
                            x=alt.X("deal_date:T", title=None),
                            y=alt.Y("㎡당가:Q", title="㎡당 매매가(만원)", scale=alt.Scale(zero=False)),
                            tooltip=["deal_date:T", "area", "floor", "price"])
                        line = alt.Chart(qm[qm["건수"] >= 2]).mark_line(point=True, color="#d62728").encode(
                            x="분기:T", y="중위:Q", tooltip=["분기:T", alt.Tooltip("중위:Q", format=",.0f"), "건수"])
                        st.altair_chart((dots + line).properties(
                            height=280, title="매매 ㎡당가 (점 = 거래, 선 = 분기 중위 2건 이상)"), width="stretch")
                    rn = deals[(deals["dataset"] == "offi_rent") & (deals["monthly_rent"].fillna(0) > 0)].copy()
                    if len(rn):
                        rn["월"] = rn["deal_date"].dt.to_period("M").dt.start_time
                        mon = rn.groupby("월").agg(월세계약=("monthly_rent", "size"),
                                                   중위월세=("monthly_rent", "median")).reset_index()
                        st.altair_chart(alt.Chart(mon).mark_bar().encode(
                            x=alt.X("월:T", title=None), y=alt.Y("월세계약:Q", title="월세 계약 수"),
                            tooltip=["월:T", "월세계약", "중위월세"]).properties(height=180, title="월별 월세 계약"),
                            width="stretch")

        st.markdown("#### Pareto 후보 (가중치 없음)")
        st.caption("가격방어(성숙기 CAGR, 없으면 5년 CAGR) 상위 25% ∧ 매매회전율 상위 25% ∧ 임대회전율 상위 25% ∧ "
                   "월세수익률 상위 50%의 교집합. 법정동은 재고 300호+, 단지는 50호+ 중에서. 최종 점수는 결과를 보고 2차 결정.")
        pa = mk["pareto"]
        if len(pa):
            st.dataframe(pa[[c for c in ["level", "시군구", "법정동", "단지명", "stock_units", "defense_metric",
                                         "mature_price_cagr", "price_cagr_5y", "trade_turnover_12m",
                                         "rent_turnover_12m", "gross_yield", "top1_stock_share", "thresholds"]
                             if c in pa.columns]], hide_index=True, width="stretch")
        else:
            st.info("네 조건을 모두 만족하는 후보가 없습니다 (지표 간 trade-off가 크다는 뜻).")

        age = mk["age"]
        if len(age) and "chain_index" in age.columns:
            st.markdown("#### 신축 연차별 가격곡선 (서울)")
            st.caption("같은 단지×면적군이 연차 a-1 → a로 갈 때 ㎡당가 변화(서울 성숙 재고 분기지수 대비)의 중위를 누적. "
                       "정상 감가 속도로 수렴하는 첫 연차가 T(빨간 선).")
            ag = age[age["chain_index"].notna()]
            curve = alt.Chart(ag).mark_line(point=True).encode(
                x=alt.X("age:Q", title="건물 연차 (거래연도 - 건축연도)"),
                y=alt.Y("chain_index:Q", title="누적 지수 (0년차 = 1)", scale=alt.Scale(zero=False)),
                tooltip=["age", "chain_index", "yoy_change_median", "n_pairs"])
            rule = alt.Chart(ag[ag["is_T"] == 1]).mark_rule(color="red").encode(x="age:Q")
            st.altair_chart((curve + rule).properties(height=260), width="stretch")


with tab_update:
    st.subheader("국토부 실거래가 수집")
    st.caption("이미 받은 과거 월은 건너뛰고, 아직 신고가 들어오는 최근 3개월만 다시 받습니다.")

    if st.button("인증키로 쓸 수 있는 데이터 확인"):
        with st.spinner("확인 중..."):
            st.session_state["access"] = rt_core.check_access()
    access = st.session_state.get("access", {})
    if access:
        st.table(pd.DataFrame([{"데이터": DATASETS[d]["label"],
                                "상태": "✅ 사용 가능" if ok is True else ("❌ 미등록" if ok is False else f"⚠️ {ok}"),
                                "data.go.kr 활용신청명": DATASETS[d]["apply"]} for d, ok in access.items()]))

    u_ds = st.multiselect("데이터", list(DATASETS), default=[dataset], format_func=lambda d: DATASETS[d]["label"])
    u_preset = st.selectbox("지역 묶음", ["직접 선택", *PRESETS], key="u_preset")
    u_regions = st.multiselect("지역", region_options, format_func=region_label,
                               default=PRESETS[u_preset] if u_preset in PRESETS else regions)
    extra = st.text_input("목록에 없는 시군구 코드 (5자리, 쉼표로 구분)", "")
    u_regions = list(dict.fromkeys(u_regions + [c.strip() for c in extra.split(",") if c.strip().isdigit()]))
    all_months = rt_core.month_list("200601", rt_core.current_ym())
    u_from, u_to = st.select_slider("수집 기간", options=all_months,
                                    value=(all_months[-13], all_months[-1]), format_func=ym_label)
    force = st.checkbox("이미 받은 월도 다시 받기")
    n_jobs = len(u_ds) * len(u_regions) * len(rt_core.month_list(u_from, u_to))
    st.caption(f"최대 {n_jobs:,}회 API 호출 (개발계정 일일 한도: 데이터셋당 10,000회) · "
               "연 월세회전율에는 '오피스텔 전월세'(+아파트 전월세)와 data.go.kr "
               f"'{rt_rent.BLD_APPLY}' 활용신청이 필요합니다.")

    if st.button("수집 시작", type="primary", disabled=not (u_ds and u_regions)):
        bar = st.progress(0.0)
        log = st.empty()

        def progress(done, total, msg):
            bar.progress(done / total, text=msg)

        s = rt_core.collect(u_ds, u_regions, rt_core.month_list(u_from, u_to), force=force, progress=progress)
        st.cache_data.clear()
        st.cache_resource.clear()
        st.success(f"완료: 새로 받은 월 {s['fetched']}개 ({s['rows']:,}건), 건너뜀 {s['skipped']}개")
        for msg in s["unregistered"]:
            st.warning(msg)
        for msg in s["errors"]:
            st.error(msg)

    st.divider()
    st.subheader("법원경매 vs 시세 분석 (서울남부지법 오피스텔)")
    st.caption("① 경매 사이트에서 현재 조회 가능한 물건(입찰 예정 포함)을 수집해 누적 아카이브에 병합 → "
               "② combined_analysis.py로 과거 낙찰가 vs 시세 재계산 (입찰 도우미의 예상 낙찰가 기준)")
    if st.button("경매 수집 + 분석 다시 실행"):
        steps = [
            [sys.executable, "court_crawler_expanded.py"],
            [sys.executable, "combined_analysis.py"],
        ]
        for cmd in steps:
            with st.spinner(" ".join(cmd[1:])):
                r = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            st.code((r.stdout + r.stderr)[-3000:] or "(출력 없음)", language=None)
            if r.returncode != 0:
                st.error(f"실패: {' '.join(cmd[1:])}")
                break
        st.cache_data.clear()
        st.cache_resource.clear()

    st.divider()
    st.subheader("보유 데이터 현황")
    cov_now = load_coverage()
    if cov_now.empty:
        st.info("아직 수집된 데이터가 없습니다.")
    else:
        cv = cov_now.assign(데이터=cov_now["dataset"].map(lambda d: DATASETS[d]["label"]),
                            지역=cov_now["sgg_cd"].map(region_label))
        st.dataframe(cv[["데이터", "지역", "from", "to", "months", "rows", "last_fetch"]].rename(columns={
            "from": "시작", "to": "끝", "months": "월수", "rows": "건수", "last_fetch": "최종 수집"}),
            hide_index=True, width="stretch")
