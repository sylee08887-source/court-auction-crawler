# 실거래가 검색 + 서울남부지방법원 경매 분석

국토부 실거래가를 **유형·지역 구분 없이** 수집·검색하는 도구와,
서울남부지방법원 관할(강서·양천·구로·금천·영등포·동작·관악) 오피스텔 경매 낙찰가를 시세와 비교하는 크롤러입니다.

## 빠른 시작 (클릭으로 사용)

1. `실거래가_검색.bat` 더블클릭 → 브라우저에 검색 화면이 열림
2. **⬇️ 데이터 업데이트** 탭에서 데이터·지역·기간을 고르고 `수집 시작`
3. 왼쪽 사이드바에서 데이터/지역/기간/검색어/면적/금액으로 필터
   - **🔎 거래 검색**: 개별 거래 목록. 행 클릭 → 같은 건물의 전체 거래 이력과 차트
   - **🏢 단지별**: 건물별 거래수·중위가·전용평당가. 행 클릭 → 상세
   - **📈 추이**: 시군구별 월 중위 평당가(전세는 중위 보증금)와 거래 건수
   - **⚖️ 경매 vs 시세**: `combined_result.csv` 검색·조회
   - 각 표는 `CSV 다운로드` 가능

명령줄로도 같은 수집을 할 수 있습니다.

```bash
py collect.py --check                                         # 인증키로 쓸 수 있는 데이터 확인
py collect.py -t offi_trade -r "서울남부지법 관할 (7개 구)" --from 202401
py collect.py -t apt_trade offi_trade -r 송파구 강동구 --from 202501
py collect.py --coverage                                      # 보유 현황
```

## 범용 실거래가 구조 (설계)

```
data.go.kr API (8종) ──► rt_core.fetch_month ──► 정규화(공통 스키마) ──► realestate.db (SQLite)
                                                                         │
                         collect.py (CLI) / app.py (검색 화면) ◄─────────┘
                                                                         │
                         export_legacy_offi_csv ──► molit_all.csv ──► combined_analysis.py (경매 비교)
```

| 파일 | 역할 |
|------|------|
| `rt_regions.py` | 시군구 코드 표 + 지역 묶음(프리셋). 이름·코드·프리셋을 코드 목록으로 변환 |
| `rt_core.py` | 8종 데이터셋 정의, API 호출(재시도/미등록 감지), 공통 스키마 정규화, SQLite 저장, 증분 수집 |
| `collect.py` | 명령줄 수집기 |
| `app.py` / `실거래가_검색.bat` | Streamlit 검색 화면 |

**설계 원칙**

- **하나의 스키마**: 아파트·오피스텔·연립다세대·단독다가구 × 매매·전월세를 같은 컬럼으로 저장한다
  (`name`=단지/건물명, `price`=매매가, `deposit`/`monthly_rent`=보증금/월세 등). 유형을 추가할 때 수집·화면 코드는 바꿀 필요가 없다.
- **증분 수집**: `(데이터셋, 시군구, 계약년월)` 단위로 저장하고 `fetch_log`에 수집 시각을 남긴다.
  월말로부터 90일이 지난 뒤 받은 월은 확정으로 보고 건너뛰며, 최근 월은 12시간이 지나면 다시 받는다
  (계약 후 30일 신고 기한과 해제 신고 반영). 월 단위로 통째로 교체하므로 해제·정정도 반영된다.
- **미등록 감지**: 인증키에 활용신청이 안 된 데이터셋은 첫 호출에서 감지해 해당 데이터셋을 건너뛰고 신청할 서비스명을 알려준다.
- **지역 코드 검증**: 2026년 행정구역 개편(인천 제물포·영종·검단구, 부천 3개 구, 화성 4개 구)을 반영했다.
  API는 과거 거래도 신규 코드로만 조회된다. 광주광역시는 기존 코드(29xxx)가 0건이라 목록에서 뺐고, 목록에 없는 지역은 5자리 코드로 직접 입력한다.

**API 활용신청 현황** (`py collect.py --check`로 확인)

| 데이터 | 상태 |
|------|------|
| 아파트 매매 / 오피스텔 매매 / 연립다세대 전월세 | 사용 가능 |
| 오피스텔 전월세, 아파트 전월세, 연립다세대 매매, 단독다가구 매매·전월세 | 미등록 → data.go.kr에서 `국토교통부_○○ 실거래가 자료` 활용신청 |

**다음 개선 후보**

1. 경매 매칭을 DB 기반으로 전환(`combined_analysis.py`가 CSV 대신 `realestate.db` 직접 조회, 면적·층 유사도 가중 시세)
2. 전월세가 열리면 전세가율(전세/매매) 및 매매-전세 갭 계산 탭 추가
3. 네이버 매물(호가) 수집기(`Downloads/CLAUDE_CODE_HANDOFF.md`)를 같은 DB에 적재해 호가 vs 실거래 비교
4. Windows 작업 스케줄러로 매일 `collect.py` 자동 실행

---

## 경매 분석 (기존)

## 스크립트 구성

| 파일 | 설명 |
|------|------|
| `court_crawler.py` | 법원경매 오피스텔 낙찰결과 수집 |
| `court_crawler_expanded.py` | 상태값을 넓혀 가능한 많은 오피스텔 경매 데이터 수집 |
| `molit_crawler.py` | 국토부 오피스텔 매매 실거래가 수집 (→ `collect.py`로 대체 가능) |
| `combined_analysis.py` | 낙찰가 vs 시세 비교 분석 |
| `validate_outputs.py` | CSV 산출물 기본 품질 점검 |
| `villa_court_crawler.py` | 법원경매 빌라(다세대·연립) 낙찰결과 수집 |
| `villa_molit_crawler.py` | 국토부 연립다세대 매매 실거래가 수집 (※ 현재 인증키 미등록으로 동작 안 함) |

## 설치

```bash
pip install requests
```

## API 키 설정

1. [data.go.kr](https://www.data.go.kr) 회원가입
2. 아래 두 서비스 활용신청 (즉시 자동승인)
   - 오피스텔 매매 신고 자료
   - 연립다세대 매매 신고 자료
3. `config.example.py`를 `config.py`로 복사 후 인증키 입력

```bash
cp config.example.py config.py
# config.py 열어서 SERVICE_KEY 입력
```

## 사용법

```bash
# 오피스텔 낙찰결과 수집 (법원경매)
py court_crawler.py
py court_crawler.py 20260101          # 특정일 이후
py court_crawler.py 20260101 20261231 # 기간 지정
py court_crawler_expanded.py          # 확장 수집: 현재 스냅샷 + 누적 아카이브 저장

# 오피스텔 실거래가 수집 (국토부)
py molit_crawler.py 202401 202604             # molit_all.csv 저장
py molit_crawler.py 202401 202604 output.csv

# 낙찰가 vs 시세 비교
py combined_analysis.py

# 산출물 품질 점검
py validate_outputs.py

# 빌라 버전
py villa_court_crawler.py
py villa_molit_crawler.py 202401 202604 villa_all.csv
```

## 데이터 출처

- **법원경매**: [대법원 경매정보](https://www.courtauction.go.kr) (현재 진행 사건만 제공)
- **실거래가**: [국토교통부 실거래가 공개시스템](https://rt.molit.go.kr) via data.go.kr API

## 분석 메모

- `molit_crawler.py` 기본 출력은 `molit_all.csv`입니다.
- `combined_analysis.py`는 `molit_all.csv`가 있으면 로컬 CSV를 우선 사용하고, 없으면 API를 조회합니다.
- `court_crawler_expanded.py`는 현재 조회 가능한 창을 `court_auction_expanded.csv`에 저장하고, 기존 이력과 병합해 `court_auction_archive.csv`에 누적합니다.
- `combined_analysis.py`는 `court_auction_archive.csv`가 있으면 누적 아카이브를 우선 사용하고, 없으면 현재 스냅샷/실시간 조회 순으로 내려갑니다.
- 확장 경매 CSV는 상태별 행을 보존하고, 통합 분석에서는 사건+물건+주소+매각기일 기준으로 분석용 중복 제거를 합니다.
- 실거래 매칭은 `구+지번` 후보를 먼저 찾고, 경매 주소에 실거래 `오피스텔명`이 포함되면 `건물명+지번`으로 표시합니다.
- `combined_result.csv`에는 `매칭방식`, `매칭후보수`, `매칭비고`가 포함되어 미매칭 원인을 확인할 수 있습니다.
