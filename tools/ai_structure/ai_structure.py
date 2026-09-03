#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_structure.py — AI 데이터 구조화 템플릿 (2026-09)

고객이 가진 지저분한 데이터(CSV/엑셀) → **분류·정규화·집계·요약** → 바로 쓰는 서식 엑셀.
- AI-a(규칙): 무료·결정적·오프라인. `rules.classify()` 하나만 고객 도메인에 맞추면 됨.
- AI-b(LLM): 같은 파이프라인에서 백엔드만 교체(ai.AnthropicBackend). 실호출은 3중 게이트(유료·사용자 승인).
설계선(§규제): 사실 가공(구조화·분류·집계·요약)만. 해석·판단·투자/의료/법률 추천/전망 = 안 함.

산출: 원본+AI 파생열 시트 · 카테고리 집계 시트 · 처리 리포트(어떤 규칙으로 분류했나=재현) · 요약 시트.
사용: python ai_structure.py --make-demo   (데모 데이터 생성+처리+검증)
      python ai_structure.py 입력.csv [출력.xlsx]
"""
import os, sys, csv, io, datetime as dt, collections, re

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'core'))
import ai
from xlsx import write_workbook, coerce

# ── AI-a 규칙 (고객 도메인에 맞춰 교체하는 유일한 곳) ──────────────────────
CATEGORY_RULES = [
    ('전자기기', r'노트북|마우스|키보드|충전|이어폰|헤드셋|모니터|usb|케이블|보조배터리|스피커'),
    ('의류',     r'티셔츠|맨투맨|후드|바지|청바지|자켓|점퍼|양말|니트|셔츠|원피스'),
    ('식품',     r'커피|원두|과자|라면|생수|음료|초콜릿|견과|시리얼|차\b|비스킷'),
    ('생활용품', r'세제|휴지|물티슈|수건|세면|칫솔|비누|샴푸|주방|청소'),
    ('도서',     r'책|도서|문제집|참고서|소설|만화'),
]
_COMPILED = [(c, re.compile(p, re.I)) for c, p in CATEGORY_RULES]


def normalize_amount(s):
    """'12,000원' '₩12000' '12000 원' → 12000 (정수). 실패 시 None."""
    if s is None: return None
    d = re.sub(r'[^\d]', '', str(s))
    return int(d) if d else None


def classify_row(text):
    """AI-a 결정적 규칙: 상품명 → 카테고리 + 매칭근거(재현). ai.RulesBackend에 넘길 fn."""
    t = str(text)
    for cat, pat in _COMPILED:
        m = pat.search(t)
        if m:
            return {'카테고리': cat, '분류근거': f'규칙 "{m.group(0)}"'}
    return {'카테고리': '기타', '분류근거': '무매칭'}


# ── 파이프라인 ──────────────────────────────────────────────────────
def load_rows(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        return list(r), r.fieldnames


def structure(path, out=None, product_col='상품명', amount_col='금액', date_col='주문일'):
    rows, cols = load_rows(path)
    n = len(rows)
    # AI-a 분류 (ai.process — RulesBackend, 무료·결정적)
    task = ai.Task('상품 카테고리 분류', system='상품명을 카테고리로 분류(사실 분류, 판단 아님)',
                   schema={'type': 'object', 'properties': {'카테고리': {'type': 'string'}, '분류근거': {'type': 'string'}}})
    texts = [row.get(product_col, '') for row in rows]
    results, rep = ai.process(texts, task, ai.RulesBackend(classify_row), log=None)

    # 파생열 부착
    out_cols = list(cols) + ['카테고리', '정규화금액', '주문월', '분류근거']
    out_rows = []
    for row, res in zip(rows, results):
        amt = normalize_amount(row.get(amount_col))
        month = ''
        m = re.match(r'(\d{4})[-/.](\d{1,2})', str(row.get(date_col, '')))
        if m: month = f'{int(m.group(1))}-{int(m.group(2)):02d}'
        out_rows.append([row.get(c, '') for c in cols] + [res['카테고리'], amt, month, res['분류근거']])

    # 집계 (사실 집계: 카테고리별 건수·금액합·비중)
    agg = collections.defaultdict(lambda: [0, 0])
    for r in out_rows:
        cat = r[len(cols)]; amt = r[len(cols) + 1] or 0
        agg[cat][0] += 1; agg[cat][1] += amt
    tot_amt = sum(v[1] for v in agg.values()) or 1
    agg_header = ['카테고리', '건수', '금액합', '금액비중(%)', '건수비중(%)']
    agg_rows = [[cat, v[0], v[1], round(v[1] / tot_amt * 100, 1), round(v[0] / n * 100, 1)]
                for cat, v in sorted(agg.items(), key=lambda x: -x[1][1])]

    # 처리 리포트 (재현 가능성)
    classified = sum(1 for r in out_rows if r[len(cols)] != '기타')
    rule_lines = [[f'규칙 {i+1}', f'{cat} ← {pat}'] for i, (cat, pat) in enumerate(CATEGORY_RULES)]
    report_rows = [['처리 방식', 'AI-a 규칙(결정적·무료·오프라인)'],
                   ['입력 건수', n], ['분류 성공', f'{classified}건 ({classified/n*100:.1f}%)'],
                   ['기타(무매칭)', f'{n-classified}건'], ['금액 정규화 성공', f"{sum(1 for r in out_rows if r[len(cols)+1] is not None)}건"],
                   ['규칙 수', len(CATEGORY_RULES)]] + rule_lines

    summary = {'처리 시각': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
               '입력 건수': n, '출력 건수': len(out_rows), '건수 일치': 'OK' if n == len(out_rows) else '불일치',
               '분류 성공률': f'{classified/n*100:.1f}%', '카테고리 수': len(agg),
               '처리 방식': 'AI-a 규칙(무료·결정적) — LLM 옵션은 별도'}

    out = out or os.path.splitext(path)[0] + '_구조화.xlsx'
    info = write_workbook(out, {
        '구조화 데이터': (out_cols, out_rows),
        '카테고리 집계': (agg_header, agg_rows),
        '처리 리포트': (['항목', '내용'], report_rows),
    }, summary=summary)
    return info, {'n': n, 'classified': classified, 'agg': dict(agg), 'rep': rep, 'out_rows': out_rows, 'ncols': len(cols)}


# ── 데모 ────────────────────────────────────────────────────────────
def make_demo(path):
    """지저분한 주문 데이터 데모(앞자리0 코드·문자금액·오타·공백·무매칭 포함)."""
    rows = [
        ('2026-08-01', '007731', '  게이밍 마우스 로지텍  ', '38,000원', '재구매'),
        ('2026-08-01', '008102', '무선 이어폰', '₩89000', ''),
        ('2026/08/02', '007731', '기계식 키보드(적축)', '112000', '선물용'),
        ('2026-08-03', '004410', '유기농 원두커피 1kg', '24,500 원', ''),
        ('2026-08-03', '009980', '남성 반팔 티셔츠 L', '15900원', ''),
        ('2026.08.04', '004410', '콜드브루 커피 6팩', '18,000', '정기'),
        ('2026-08-05', '003321', '주방세제 리필 2L', '8900원', ''),
        ('2026-08-05', '009980', '기모 후드 집업', '43,000원', '교환희망'),
        ('2026-08-06', '007731', 'USB-C 충전 케이블 2m', '9,900원', ''),
        ('2026-08-07', '001200', '베스트셀러 소설 세트', '36000', '도서'),
        ('2026-08-08', '004410', '견과류 믹스 30봉', '21,900원', ''),
        ('2026-08-08', '009980', '청바지 슬림핏 32', '39900원', ''),
        ('2026-08-09', '003321', '3겹 화장지 30롤', '16,900', ''),
        ('2026-08-10', '008102', '노트북 거치대 알루미늄', '27,000원', ''),
        ('2026-08-11', '005050', '수제 도자기 머그컵', '19000원', '선물'),  # 무매칭 → 기타
        ('2026-08-11', '004410', '탄산수 라임 20캔', '14,500원', ''),
        ('2026-08-12', '007731', '블루투스 스피커 방수', '55,000', ''),
        ('2026-08-13', '009980', '순면 양말 10족', '12,900원', ''),
        ('2026-08-14', '003321', '주방 청소용 물티슈', '6,500원', ''),
        ('2026-08-15', '001200', '초등 수학 문제집', '13,000원', ''),
        ('2026-08-16', '005050', '반려동물 방석', '28,000원', ''),  # 무매칭
        ('2026-08-17', '004410', '초콜릿 선물세트', '32,000원', '선물'),
        ('2026-08-18', '008102', '4K 모니터 27인치', '289000원', ''),
        ('2026-08-19', '009980', '경량 패딩 자켓', '78,000', '겨울'),
        ('2026-08-20', '003321', '섬유유연제 대용량', '11,900원', ''),
        ('2026-08-21', '007731', '노트북 보조배터리', '45,000원', ''),
        ('2026-08-22', '004410', '즉석 라면 멀티팩', '9,800원', ''),
        ('2026-08-23', '005050', '캠핑 접이식 의자', '34,000원', ''),  # 무매칭
        ('2026-08-24', '001200', '영어 회화 참고서', '17,500원', ''),
        ('2026-08-25', '009980', '니트 스웨터 라운드넥', '41,000원', ''),
    ]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['주문일', '상품코드', '상품명', '금액', '고객메모'])
        w.writerows(rows)
    return len(rows)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    here = os.path.dirname(os.path.abspath(__file__))
    if '--make-demo' in sys.argv or len(sys.argv) == 1:
        demo = os.path.join(here, 'demo_orders.csv')
        n = make_demo(demo)
        print(f"데모 생성: {demo} ({n}행)")
        info, chk = structure(demo)
        print(f"\n=== 구조화 완료 ===")
        print(f"  출력: {info['path']} ({info['sheets']}시트 {info['rows']}행 {info['size_kb']}KB)")
        print(f"  입력={chk['n']} 출력={len(chk['out_rows'])} 건수일치={'OK' if chk['n']==len(chk['out_rows']) else '불일치'}")
        print(f"  분류 성공={chk['classified']}/{chk['n']} ({chk['classified']/chk['n']*100:.1f}%)")
        print(f"  카테고리 집계:")
        for cat, (cnt, amt) in sorted(chk['agg'].items(), key=lambda x: -x[1][1]):
            print(f"    {cat:8s} {cnt:2d}건  {amt:>10,}원")
        # 검증: 앞자리0 코드 보존 + 재현(2회 동일)
        info2, chk2 = structure(demo, out=os.path.join(here, '_recheck.xlsx'))
        same = chk['agg'] == chk2['agg']
        print(f"  재현성(2회 동일 집계): {'OK' if same else '불일치'}")
        z = sum(1 for r in chk['out_rows'] if str(r[1]).startswith('0'))
        print(f"  앞자리0 상품코드 보존 대상: {z}건 (엑셀에서 문자 유지)")
        os.remove(os.path.join(here, '_recheck.xlsx'))
    else:
        inp = sys.argv[1]; out = sys.argv[2] if len(sys.argv) > 2 else None
        info, chk = structure(inp, out)
        print(f"완료: {info['path']} — 입력 {chk['n']}행, 분류 {chk['classified']}건")
