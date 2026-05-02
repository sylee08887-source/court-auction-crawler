# 서울남부지방법원 경매 실거래가 분석

서울남부지방법원 관할 구역(강서·양천·구로·금천·영등포·동작·관악)의  
법원경매 낙찰 데이터와 국토부 실거래가를 수집·비교하는 크롤러입니다.

## 스크립트 구성

| 파일 | 설명 |
|------|------|
| `court_crawler.py` | 법원경매 오피스텔 낙찰결과 수집 |
| `molit_crawler.py` | 국토부 오피스텔 매매 실거래가 수집 |
| `combined_analysis.py` | 낙찰가 vs 시세 비교 분석 |
| `villa_court_crawler.py` | 법원경매 빌라(다세대·연립) 낙찰결과 수집 |
| `villa_molit_crawler.py` | 국토부 연립다세대 매매 실거래가 수집 |

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

# 오피스텔 실거래가 수집 (국토부)
py molit_crawler.py 202401 202604
py molit_crawler.py 202401 202604 output.csv

# 낙찰가 vs 시세 비교
py combined_analysis.py

# 빌라 버전
py villa_court_crawler.py
py villa_molit_crawler.py 202401 202604 villa_all.csv
```

## 데이터 출처

- **법원경매**: [대법원 경매정보](https://www.courtauction.go.kr) (현재 진행 사건만 제공)
- **실거래가**: [국토교통부 실거래가 공개시스템](https://rt.molit.go.kr) via data.go.kr API
