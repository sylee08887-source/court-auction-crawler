"""
국토부 실거래가 범용 수집기 (CLI). 결과는 realestate.db에 증분 저장된다.

사용법:
    py collect.py --check                                   # 인증키로 쓸 수 있는 데이터셋 확인
    py collect.py -t offi_trade -r "서울남부지법 관할 (7개 구)" --from 202401
    py collect.py -t apt_trade offi_trade -r 송파구 강동구 --from 202501 --to 202609
    py collect.py -t offi_trade -r 11560 --from 202401 --force   # 이미 받은 월도 다시 받기
    py collect.py --coverage                                # 보유 데이터 현황
    py collect.py --legacy-csv molit_all.csv                # combined_analysis.py용 CSV 내보내기

데이터셋: apt_trade, apt_rent, offi_trade, offi_rent, rh_trade, rh_rent, sh_trade, sh_rent
지역: 시군구명(예: 영등포구, '경기도 성남시 분당구'), 5자리 코드, 또는 rt_regions.PRESETS 이름
"""

import argparse
import sys

import rt_core
from rt_regions import PRESETS, region_label, resolve

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description="국토부 실거래가 범용 수집기")
    p.add_argument("-t", "--types", nargs="+", default=["offi_trade"], choices=list(rt_core.DATASETS))
    p.add_argument("-r", "--regions", nargs="+", default=["서울남부지법 관할 (7개 구)"])
    p.add_argument("--from", dest="start", default=None, help="시작 YYYYMM (기본: 12개월 전)")
    p.add_argument("--to", dest="end", default=None, help="종료 YYYYMM (기본: 이번 달)")
    p.add_argument("--force", action="store_true", help="저장된 월도 다시 수집")
    p.add_argument("--check", action="store_true", help="데이터셋별 인증키 사용 가능 여부 확인")
    p.add_argument("--coverage", action="store_true", help="보유 데이터 현황 출력")
    p.add_argument("--legacy-csv", metavar="PATH", help="오피스텔 매매를 molit_all.csv 형식으로 내보내기")
    args = p.parse_args()

    if args.check:
        for ds, ok in rt_core.check_access().items():
            mark = "O 사용 가능" if ok is True else ("X 미등록" if ok is False else f"? {ok}")
            print(f"  {rt_core.DATASETS[ds]['label']:<12} {mark}")
        return

    if args.coverage:
        for c in rt_core.coverage():
            print(f"  {rt_core.DATASETS[c['dataset']]['label']:<12} {region_label(c['sgg_cd']):<20} "
                  f"{c['from']}~{c['to']} ({c['months']}개월) {c['rows']:>7,}건  최종수집 {c['last_fetch']}")
        return

    try:
        codes = resolve(args.regions)
    except ValueError as exc:
        print(f"오류: {exc}\n프리셋: {', '.join(PRESETS)}")
        sys.exit(1)

    if args.legacy_csv:
        n = rt_core.export_legacy_offi_csv(args.legacy_csv, codes)
        print(f"오피스텔 매매 {n:,}건 -> {args.legacy_csv}")
        return

    end = args.end or rt_core.current_ym()
    if args.start:
        start = args.start
    else:
        y, m = int(end[:4]), int(end[4:])
        start = f"{y - 1}{m:02d}"
    months = rt_core.month_list(start, end)

    print(f"데이터셋: {', '.join(rt_core.DATASETS[t]['label'] for t in args.types)}")
    print(f"지역: {', '.join(region_label(c) for c in codes)}")
    print(f"기간: {start} ~ {end} ({len(months)}개월)\n")

    def progress(done, total, msg):
        print(f"  [{done}/{total}] {msg}")

    s = rt_core.collect(args.types, codes, months, force=args.force, progress=progress)
    print(f"\n완료: 새로 받은 월 {s['fetched']}개 ({s['rows']:,}건), 건너뜀 {s['skipped']}개")
    for msg in s["unregistered"]:
        print(f"  [미등록] {msg}")
    for msg in s["errors"]:
        print(f"  [오류] {msg}")


if __name__ == "__main__":
    main()
