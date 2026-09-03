#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pdf_excel.py — 거래명세서 PDF 묶음 → 정리 엑셀 추출기 (2026-09)

폴더의 거래명세서/견적서 PDF들을 읽어 문서 정보(번호·일자·업체)와 품목 행(품명·수량·단가·금액)을
추출하고, 문서 요약 + 전체 품목 상세 + 검증 리포트를 서식 엑셀로 낸다.

정직 설계:
  - 대상 = **텍스트 기반 PDF**(스캔 이미지는 OCR 별도 — 명시). 고객 양식마다 파서 1회 맞춤(템플릿 방식).
  - **내부 일관성 검증 내장**: 추출한 품목 합계 vs 문서에 인쇄된 합계 대조 → 불일치 문서는 표시(명세서 자체 오류도 검출).
  - 파싱 불가/텍스트 없는 PDF = 격리 표기(묵살 금지).

검증(--make-demo): 데모 명세서 12건을 **HTML→Chromium PDF로 생성**(생성 경로) 후 **pypdf로 추출**(독립 경로)
  → 원본 값 전수 대조(문서 필드·품목 행·금액) + 합계 일관성 + 빈 PDF 격리 + 재현성.
"""
import os, sys, re, json, glob, random, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook
from pypdf import PdfReader


# ── 추출 (독립 경로: pypdf 텍스트 → 정규식) ──────────────────────────
RE_DOC = re.compile(r'문서번호\s*[:：]?\s*([A-Z]{2}-\d{4}-\d{3})')
RE_DATE = re.compile(r'거래일자\s*[:：]?\s*(\d{4}-\d{2}-\d{2})')
RE_VENDOR = re.compile(r'공급자\s*[:：]?\s*([가-힣A-Za-z0-9()\s]{2,20}?)\s*(?:등록번호|$|\n)')
RE_ITEM = re.compile(r'^(.{2,30}?)\s+(\d{1,4})\s+([\d,]{1,12})\s+([\d,]{1,15})\s*$', re.M)
RE_TOTAL = re.compile(r'합계금액\s*[:：]?\s*([\d,]+)')


def _n(s):
    return int(str(s).replace(',', ''))


def extract_pdf(path):
    """PDF 1건 → {doc, date, vendor, items[(품명,수량,단가,금액)], printed_total, ok, why}"""
    try:
        txt = '\n'.join((pg.extract_text() or '') for pg in PdfReader(path).pages)
    except Exception as e:
        return {'ok': False, 'why': f'PDF 읽기 실패 {type(e).__name__}'}
    if len(txt.strip()) < 20:
        return {'ok': False, 'why': '텍스트 없음(스캔 이미지 추정 — OCR 필요)'}
    doc = RE_DOC.search(txt)
    date = RE_DATE.search(txt)
    vendor = RE_VENDOR.search(txt)
    items = [(m.group(1).strip(), _n(m.group(2)), _n(m.group(3)), _n(m.group(4)))
             for m in RE_ITEM.finditer(txt)
             if not any(k in m.group(1) for k in ('품명', '합계', '수량'))]
    total = RE_TOTAL.search(txt)
    if not (doc and date and items and total):
        return {'ok': False, 'why': f'필드 누락(doc={bool(doc)} date={bool(date)} items={len(items)} total={bool(total)})'}
    return {'ok': True, 'doc': doc.group(1), 'date': date.group(1),
            'vendor': (vendor.group(1).strip() if vendor else ''),
            'items': items, 'printed_total': _n(total.group(1)),
            'sum_items': sum(it[3] for it in items)}


def run(folder, out=None):
    pdfs = sorted(glob.glob(os.path.join(folder, '*.pdf')))
    docs, quar = [], []
    for p in pdfs:
        r = extract_pdf(p)
        (docs if r.get('ok') else quar).append((os.path.basename(p), r))
    sum_rows, item_rows = [], []
    for name, r in docs:
        consistent = (r['sum_items'] == r['printed_total'])
        sum_rows.append([r['doc'], r['date'], r['vendor'], len(r['items']),
                         r['sum_items'], r['printed_total'],
                         'OK' if consistent else '★불일치(문서 확인 필요)', name])
        for it in r['items']:
            item_rows.append([r['doc'], r['date'], r['vendor'], *it])
    out = out or os.path.join(folder, '..', '명세서_통합.xlsx')
    info = write_workbook(os.path.abspath(out), {
        '문서 요약': (['문서번호', '거래일자', '공급자', '품목수', '품목합계', '인쇄합계', '합계검증', '파일'], sum_rows),
        '품목 상세': (['문서번호', '거래일자', '공급자', '품명', '수량', '단가', '금액'], item_rows),
        **({'격리(확인 필요)': (['파일', '사유'], [[n, r['why']] for n, r in quar])} if quar else {}),
    }, summary={
        '생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'PDF 수 / 추출 성공 / 격리': f'{len(pdfs)} / {len(docs)} / {len(quar)}',
        '품목 행 합계': f'{len(item_rows)}행',
        '합계 일관성': f"{sum(1 for r_ in sum_rows if r_[6]=='OK')}/{len(sum_rows)} 문서 일치",
        '주의': '텍스트 기반 PDF 대상(스캔 이미지=OCR 별도) · 양식이 다르면 파서 1회 맞춤'})
    return info, docs, quar, sum_rows, item_rows


# ── 데모 PDF 생성 (생성 경로: HTML → Chromium 인쇄, 추출과 완전 분리) ──
HTML_TMPL = """<html><head><meta charset=utf-8><style>
body{{font-family:'Malgun Gothic';padding:34px;color:#111}}
h1{{font-size:20px;letter-spacing:8px;text-align:center;margin-bottom:18px}}
.meta{{font-size:12.5px;line-height:1.8;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th,td{{border:1px solid #555;padding:6px 8px;text-align:right}}
th{{background:#eee}} td:first-child,th:first-child{{text-align:left}}
.tot{{margin-top:12px;font-size:14px;font-weight:bold;text-align:right}}
</style></head><body>
<h1>거래명세서</h1>
<div class=meta>문서번호: {doc}<br>거래일자: {date}<br>공급자: {vendor} 등록번호: 123-45-{regno}</div>
<table><tr><th>품명</th><th>수량</th><th>단가</th><th>공급가액</th></tr>{rows}</table>
<div class=tot>합계금액: {total:,}</div>
</body></html>"""

CATALOG = [('무선 마우스', 38000), ('기계식 키보드', 112000), ('모니터 27인치', 289000),
           ('USB-C 케이블 2m', 9900), ('노트북 거치대', 27000), ('웹캠 FHD', 45000),
           ('공유기 AX3000', 89000), ('외장 SSD 1TB', 129000), ('멀티탭 6구', 15900),
           ('사은품 스티커', 0)]                     # 0원 경계
VENDORS = ['한빛전산', '두리안테크', '가온상사', '푸른유통(주)']


def make_demo(folder, n=12):
    random.seed(20260903)
    os.makedirs(folder, exist_ok=True)
    for f in glob.glob(os.path.join(folder, '*.pdf')):
        os.remove(f)
    truth = []
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page()
        for i in range(1, n + 1):
            # 마지막 문서 = 30품목(강제 멀티페이지 + 중복 품명) — 실전 함정 하드닝
            items = random.choices(CATALOG, k=30) if i == n else random.sample(CATALOG, random.randint(2, 5))
            rows_data = [(nm, random.randint(1, 20), price) for nm, price in items]
            total = sum(q * p for _, q, p in rows_data)
            doc = {'doc': f'HB-2026-{i:03d}',
                   'date': (dt.date(2026, 8, 1) + dt.timedelta(days=i * 2)).isoformat(),
                   'vendor': random.choice(VENDORS), 'regno': f'{10000 + i}',
                   'items': [(nm, q, p, q * p) for nm, q, p in rows_data], 'total': total}
            rows = ''.join(f'<tr><td>{nm}</td><td>{q}</td><td>{p:,}</td><td>{q*p:,}</td></tr>'
                           for nm, q, p, _ in doc['items'])
            pg.set_content(HTML_TMPL.format(doc=doc['doc'], date=doc['date'], vendor=doc['vendor'],
                                            regno=doc['regno'], rows=rows, total=total))
            pg.pdf(path=os.path.join(folder, f'명세서_{i:02d}.pdf'), format='A4')
            truth.append(doc)
        br.close()
    # 격리 검증용: 텍스트 없는 빈 PDF 1건
    open(os.path.join(folder, '스캔본_흉내.pdf'), 'wb').write(
        b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n'
        b'3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\nxref\n0 4\ntrailer<</Size 4/Root 1 0 R>>\n%%EOF')
    json.dump(truth, open(os.path.join(folder, '_truth.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    return truth


def main_demo():
    folder = os.path.join(HERE, 'demo_pdfs')
    truth = make_demo(folder)
    info, docs, quar, sum_rows, item_rows = run(folder, os.path.join(HERE, '명세서_통합.xlsx'))
    _, docs2, _, sum2, item2 = run(folder, os.path.join(HERE, '_recheck.xlsx'))
    same = (sum_rows == sum2 and item_rows == item2)
    os.remove(os.path.join(HERE, '_recheck.xlsx'))

    # 왕복 전수 대조: 생성 원본(truth) vs 추출 결과
    ext = {r['doc']: r for _, r in docs}
    field_ok = item_ok = amt_ok = 0
    n_items_truth = sum(len(t['items']) for t in truth)
    for t in truth:
        e = ext.get(t['doc'])
        if not e:
            continue
        field_ok += (e['date'] == t['date'] and t['vendor'].replace('(주)', '') in e['vendor'].replace('(주)', '')
                     and e['printed_total'] == t['total'])
        from collections import Counter
        tc = Counter(tuple(it) for it in t['items'])       # 멀티셋 대조(중복 품명 보존)
        ec = Counter(tuple(it) for it in e['items'])
        item_ok += sum((tc & ec).values())
        amt_ok += (e['sum_items'] == t['total'])
    consist = sum(1 for r in sum_rows if r[6] == 'OK')

    now = dt.datetime.now()
    L = [f'# PDF→엑셀 추출 검증 리포트 ({now:%Y-%m-%d %H:%M})',
         f'- 데모: 거래명세서 PDF {len(truth)}건(HTML→Chromium 인쇄 생성) + 텍스트 없는 PDF 1건 · 추출 = pypdf(독립 경로)',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 문서 추출 | **{len(docs)}/{len(truth)}건** (격리 {len(quar)}건 = 텍스트 없는 PDF, 사유 표기) |',
         f'| ② 왕복 전수 대조 — 문서 필드(일자·공급자·합계) | **{field_ok}/{len(truth)}** 일치 |',
         f'| ② 왕복 전수 대조 — 품목 행(품명·수량·단가·금액) | **{item_ok}/{n_items_truth}** 일치 |',
         f'| ③ 합계 일관성(품목합 vs 인쇄합계) | **{consist}/{len(sum_rows)}** 문서 일치 |',
         f'| ④ 경계 | 0원 품목·콤마 금액·(주) 상호 포함 |',
         f'| ⑤ 재현성(2회 동일) | {"OK" if same else "★불일치"} |',
         f'| 산출 | 명세서_통합.xlsx ({info["sheets"]}시트) |',
         '', '- ※ 텍스트 기반 PDF 대상(스캔 이미지 = OCR 별도). 고객 양식이 다르면 파서 1회 맞춤(템플릿 방식) — 맞춘 뒤 같은 전수 대조 리포트 제공.']
    rep = os.path.join(HERE, 'pdf_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return (len(docs) == len(truth) and field_ok == len(truth)
            and item_ok == n_items_truth and consist == len(sum_rows) and same and len(quar) == 1)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--make-demo' in sys.argv or len(sys.argv) == 1:
        ok = main_demo()
        sys.exit(0 if ok else 1)
    else:
        info, docs, quar, *_ = run(sys.argv[1])
        print(f"완료: {info['path']} — 추출 {len(docs)} · 격리 {len(quar)}")
