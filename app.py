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

import rt_core
from rt_core import DATASETS, KOREAN
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

tab_search, tab_complex, tab_trend, tab_auction, tab_update = st.tabs(
    ["🔎 거래 검색", "🏢 단지별", "📈 추이", "⚖️ 경매 vs 시세", "⬇️ 데이터 업데이트"])


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
            for c in num_cols:
                ca[c] = pd.to_numeric(ca[c], errors="coerce")
            cols = ["매각기일", "사건번호", "물건번호", "구", "법정동", "건물명", "층", "전용면적(㎡)",
                    "감정가(만원)", "낙찰가(만원)", "낙찰가율(%)", "유찰횟수", "추정시세(만원)", "낙찰가/시세(%)",
                    "감정가/시세(%)", "매칭방식", "실거래유형", "매칭건물명", "시세산출", "비교기간", "매칭비고", "소재지"]
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
                if MATCHES_CSV.exists():
                    mt = pd.read_csv(MATCHES_CSV, encoding="utf-8-sig", dtype=str)
                    mt = mt[(mt["사건번호"] == sel["사건번호"]) & (mt["물건번호"] == sel["물건번호"])
                            & (mt["매각기일"] == sel["매각기일"])]
                    st.dataframe(mt.drop(columns=["사건번호", "물건번호", "매각기일"]), hide_index=True, width="stretch")

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
    st.caption(f"최대 {n_jobs:,}회 API 호출 (개발계정 일일 한도: 데이터셋당 10,000회)")

    if st.button("수집 시작", type="primary", disabled=not (u_ds and u_regions)):
        bar = st.progress(0.0)
        log = st.empty()

        def progress(done, total, msg):
            bar.progress(done / total, text=msg)

        s = rt_core.collect(u_ds, u_regions, rt_core.month_list(u_from, u_to), force=force, progress=progress)
        st.cache_data.clear()
        st.success(f"완료: 새로 받은 월 {s['fetched']}개 ({s['rows']:,}건), 건너뜀 {s['skipped']}개")
        for msg in s["unregistered"]:
            st.warning(msg)
        for msg in s["errors"]:
            st.error(msg)

    st.divider()
    st.subheader("법원경매 vs 시세 분석 (서울남부지법 오피스텔)")
    st.caption("① 경매 사이트에서 현재 조회 가능한 결과를 수집해 누적 아카이브에 병합 → "
               "② DB의 오피스텔 매매를 molit_all.csv로 내보내기 → ③ combined_analysis.py 실행")
    if st.button("경매 수집 + 분석 다시 실행"):
        steps = [
            [sys.executable, "court_crawler_expanded.py"],
            [sys.executable, "collect.py", "--legacy-csv", "molit_all.csv",
             "-r", "서울남부지법 관할 (7개 구)"],
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
