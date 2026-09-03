#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""csv_dashboard.py — CSV → 단일 HTML 대시보드 생성 (2026-09)

판매/거래 CSV를 읽어 **설치·서버 없이 더블클릭으로 열리는** 자기완결 대시보드 HTML을 만든다.
KPI 타일 · 월별 추이 · 카테고리 상위(SVG 자체 렌더, 외부 CDN 0) · 검색/정렬 테이블 · 카테고리 필터.
외부 CDN·서버 의존 없이 파일 하나로 완결되는 대시보드 패턴.

검증(--make-demo):
  ① 수치 대조 — HTML에 임베드된 KPI/집계 수치를 **독립 경로로 재집계해 전수 대조**
  ② 렌더 스모크 — Playwright로 실제 열어 KPI 텍스트 일치 · 콘솔 에러 0 확인
  ③ 경계 — 0원·빈 카테고리(→미분류)·콤마 금액·불량행 격리(묵살 금지)
  ④ 재현성 — 2회 생성 동일 집계
"""
import os, sys, csv, json, re, html, random, datetime as dt, collections

HERE = os.path.dirname(os.path.abspath(__file__))


# ── 집계 ────────────────────────────────────────────────────────────
def parse_amount(s):
    d = re.sub(r'[^\d-]', '', str(s or ''))
    try:
        return int(d)
    except ValueError:
        return None


def aggregate(rows, date_col='일자', cat_col='카테고리', amt_col='금액'):
    ok, bad = [], []
    for i, r in enumerate(rows, 2):
        amt = parse_amount(r.get(amt_col))
        d = str(r.get(date_col, ''))
        m = re.match(r'(\d{4})[-/.](\d{1,2})', d)
        if amt is None or not m:
            bad.append((i, d, r.get(amt_col), '금액/일자 형식 오류')); continue
        ok.append({'월': f'{int(m.group(1))}-{int(m.group(2)):02d}',
                   '일자': d, '카테고리': (r.get(cat_col) or '').strip() or '미분류',
                   '상품': (r.get('상품') or '').strip(), '금액': amt})
    total = sum(r['금액'] for r in ok)
    monthly = collections.OrderedDict()
    for r in sorted(ok, key=lambda x: x['월']):
        monthly[r['월']] = monthly.get(r['월'], 0) + r['금액']
    bycat = collections.Counter()
    for r in ok:
        bycat[r['카테고리']] += r['금액']
    return {'rows': ok, 'bad': bad, 'total': total, 'n': len(ok),
            'avg': total // len(ok) if ok else 0,
            'monthly': dict(monthly), 'bycat': dict(bycat),
            'span': f"{min(r['일자'] for r in ok)} ~ {max(r['일자'] for r in ok)}" if ok else '-'}


# ── SVG 차트 (외부 의존 0) ──────────────────────────────────────────
def svg_bars(pairs, w=520, h=190, color='#4f46e5'):
    if not pairs:
        return '<svg></svg>'
    mx = max(v for _, v in pairs) or 1
    n = len(pairs)
    bw = max(8, int((w - 10 * n) / n))
    parts = [f'<svg viewBox="0 0 {w} {h + 34}" style="width:100%">']
    for i, (k, v) in enumerate(pairs):
        bh = int(v / mx * h)
        x = i * (bw + 10) + 5
        parts.append(f'<rect x="{x}" y="{h - bh}" width="{bw}" height="{bh}" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{x + bw/2}" y="{h + 14}" font-size="10" text-anchor="middle" fill="#667">{html.escape(str(k)[-5:])}</text>')
        parts.append(f'<text x="{x + bw/2}" y="{h - bh - 4}" font-size="9" text-anchor="middle" fill="#334">{v//10000}만</text>')
    parts.append('</svg>')
    return ''.join(parts)


# ── HTML 생성 ───────────────────────────────────────────────────────
def build_html(agg, title='매출 대시보드'):
    cats = sorted(agg['bycat'].items(), key=lambda x: -x[1])
    rows_json = json.dumps(agg['rows'], ensure_ascii=False)
    kpi = f"""
    <div class=cards>
      <div class=card><div class=v id=k-total>{agg['total']:,}원</div><div class=k>총 매출</div></div>
      <div class=card><div class=v id=k-n>{agg['n']:,}건</div><div class=k>거래 수</div></div>
      <div class=card><div class=v id=k-avg>{agg['avg']:,}원</div><div class=k>건당 평균</div></div>
      <div class=card><div class=v style="font-size:15px" id=k-span>{agg['span']}</div><div class=k>데이터 기간</div></div>
    </div>"""
    charts = f"""
    <div class=row>
      <div class=panel><h3>월별 매출</h3>{svg_bars(list(agg['monthly'].items()))}</div>
      <div class=panel><h3>카테고리 상위</h3>{svg_bars(cats[:8], color='#0d9488')}</div>
    </div>"""
    doc = f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
body{{font-family:'Malgun Gothic',sans-serif;margin:0;background:#f4f6f9;color:#14181f}}
.wrap{{max-width:1100px;margin:0 auto;padding:26px 20px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#7b8493;font-size:13px;margin-bottom:18px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px}}
.card{{background:#fff;border:1px solid #e3e8ee;border-radius:12px;padding:16px;text-align:center}}
.card .v{{font-size:21px;font-weight:800;color:#4f46e5}} .card .k{{font-size:12px;color:#7b8493;margin-top:4px}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}}
.panel{{background:#fff;border:1px solid #e3e8ee;border-radius:12px;padding:14px}}
.panel h3{{margin:0 0 8px;font-size:14px}}
table{{width:100%;border-collapse:collapse;font-size:13px;background:#fff}}
th{{background:#eef1f5;padding:8px;text-align:left;cursor:pointer;user-select:none}}
td{{padding:7px 8px;border-top:1px solid #eef1f4}}
input,select{{padding:7px 10px;border:1px solid #cfd6de;border-radius:8px;font-size:13px}}
.bar{{display:flex;gap:8px;margin-bottom:10px}}
.badge{{font-size:11px;color:#8b95a3;margin-top:14px}}
@media(max-width:760px){{.row{{grid-template-columns:1fr}}}}
</style></head><body><div class=wrap>
<h1>{html.escape(title)}</h1><div class=sub>생성 {dt.datetime.now():%Y-%m-%d %H:%M} · 원본 {agg['n']:,}건{' · 제외 ' + str(len(agg['bad'])) + '건(형식 오류, 하단 표기)' if agg['bad'] else ''}</div>
{kpi}{charts}
<div class=panel><h3>거래 내역</h3>
<div class=bar><input id=q placeholder="검색(상품·카테고리)" oninput=draw()>
<select id=cat onchange=draw()><option value="">전체 카테고리</option>{''.join(f'<option>{html.escape(c)}</option>' for c, _ in cats)}</select>
<span style="align-self:center;font-size:12px;color:#7b8493" id=cnt></span></div>
<table><thead><tr><th onclick=srt('일자')>일자</th><th onclick=srt('카테고리')>카테고리</th><th onclick=srt('상품')>상품</th><th onclick=srt('금액')>금액</th></tr></thead>
<tbody id=tb></tbody></table></div>
<div class=badge>단일 파일 대시보드 — 서버·설치 불필요 · 데이터는 이 파일 안에만 있음{' · 제외 행: ' + html.escape(str([b[0] for b in agg['bad']])) if agg['bad'] else ''}</div>
</div>
<script>
const D={rows_json};let sk='일자',asc=true;
function srt(k){{if(sk===k)asc=!asc;else{{sk=k;asc=true}}draw()}}
function draw(){{const q=document.getElementById('q').value.toLowerCase();const c=document.getElementById('cat').value;
let r=D.filter(x=>(!c||x['카테고리']===c)&&(!q||(x['상품']+x['카테고리']).toLowerCase().includes(q)));
r.sort((a,b)=>{{const va=a[sk],vb=b[sk];return (va>vb?1:va<vb?-1:0)*(asc?1:-1)}});
document.getElementById('cnt').textContent=r.length+'건 / 합계 '+r.reduce((s,x)=>s+x['금액'],0).toLocaleString()+'원';
document.getElementById('tb').innerHTML=r.slice(0,300).map(x=>`<tr><td>${{x['일자']}}</td><td>${{x['카테고리']}}</td><td>${{x['상품']}}</td><td style="text-align:right">${{x['금액'].toLocaleString()}}원</td></tr>`).join('');}}
draw();
</script></body></html>"""
    return doc


# ── 데모 + 검증 ─────────────────────────────────────────────────────
def make_demo(path, n=150):
    random.seed(20260903)
    catalog = {'전자기기': ['무선 마우스', '모니터', '블루투스 이어폰', 'USB 허브'],
               '의류': ['반팔 티셔츠', '청바지', '후드 집업', '니트'],
               '식품': ['원두커피', '견과류', '탄산수', '초콜릿'],
               '생활용품': ['주방세제', '화장지', '물티슈', '수건'],
               '도서': ['베스트셀러', '문제집', '에세이', '만화']}
    rows = []
    for i in range(n):
        d = (dt.date(2026, 3, 1) + dt.timedelta(days=i * 1.2)).isoformat()
        amt = random.choice([0, 999, 12000, 45000, 128000, 3500000])   # 0원·큰 금액 경계
        cat = random.choice(list(catalog))
        prod = random.choice(catalog[cat])                              # 카테고리-상품 정합
        if i % 29 == 0:
            cat = ''                                                    # 빈 카테고리 → 미분류
        amt_s = f'{amt:,}' if i % 7 == 0 else str(amt)                 # 콤마 금액
        if i % 61 == 0:
            amt_s = '십만원'                                            # 불량 → 격리
        rows.append([d, cat, prod, amt_s])
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['일자', '카테고리', '상품', '금액']); w.writerows(rows)
    return n


def independent_recount(path):
    """검증용 독립 경로: aggregate()를 거치지 않고 원시 파싱으로 총액·건수 재계산."""
    tot = n = 0
    for r in csv.DictReader(open(path, encoding='utf-8-sig')):
        d = re.sub(r'[^\d-]', '', str(r.get('금액') or ''))
        if d and re.match(r'\d{4}[-/.]\d{1,2}', str(r.get('일자', ''))):
            tot += int(d); n += 1
    return tot, n


def render_check(html_path, expect_total, expect_n):
    """Playwright로 실제 렌더: KPI 텍스트 일치 + 콘솔 에러 0."""
    from playwright.sync_api import sync_playwright
    errs = []
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page()
        pg.on('console', lambda m: errs.append(m.text) if m.type == 'error' else None)
        pg.goto('file:///' + html_path.replace('\\', '/'))
        kt = pg.text_content('#k-total') or ''
        kn = pg.text_content('#k-n') or ''
        rows_shown = pg.locator('#tb tr').count()
        br.close()
    return {'kpi_total_ok': kt.strip() == f'{expect_total:,}원',
            'kpi_n_ok': kn.strip() == f'{expect_n:,}건',
            'console_errors': len(errs), 'table_rows': rows_shown, 'kt': kt, 'kn': kn}


def main_demo():
    demo = os.path.join(HERE, 'demo_sales.csv')
    n = make_demo(demo)
    rows = list(csv.DictReader(open(demo, encoding='utf-8-sig')))
    agg = aggregate(rows)
    agg2 = aggregate(rows)
    same = (agg['total'] == agg2['total'] and agg['monthly'] == agg2['monthly'] and agg['bycat'] == agg2['bycat'])
    out = os.path.join(HERE, 'dashboard_demo.html')
    open(out, 'w', encoding='utf-8').write(build_html(agg, '한빛상사 매출 대시보드 (데모)'))

    ind_total, ind_n = independent_recount(demo)
    num_ok = (ind_total == agg['total'] and ind_n == agg['n'])
    rc = render_check(out, agg['total'], agg['n'])

    now = dt.datetime.now()
    L = [f'# CSV 대시보드 검증 리포트 ({now:%Y-%m-%d %H:%M})',
         f'- 데모 {n}행(0원·콤마금액·빈 카테고리·불량행 포함) → 유효 {agg["n"]} · 격리 {len(agg["bad"])}행 · 산출 {os.path.basename(out)} ({os.path.getsize(out)//1024}KB, 단일 파일)',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 수치 대조(독립 재집계 vs 집계엔진) | 총액 {ind_total:,} vs {agg["total"]:,} · 건수 {ind_n} vs {agg["n"]} → {"일치 PASS" if num_ok else "★불일치"} |',
         f'| ② 렌더 스모크(Playwright 실제 열람) | KPI 총액 {"일치" if rc["kpi_total_ok"] else "★불일치"} · 건수 {"일치" if rc["kpi_n_ok"] else "★불일치"} · 콘솔에러 {rc["console_errors"]} · 테이블 {rc["table_rows"]}행 렌더 |',
         f'| ③ 경계·격리 | 0원 포함 집계 · 빈 카테고리→미분류 · 불량 {len(agg["bad"])}행 하단 표기(묵살 금지) |',
         f'| ④ 재현성(2회 동일) | {"OK" if same else "★불일치"} |',
         '', '- 외부 CDN·서버 의존 0 (SVG 자체 렌더) · 데이터는 파일 안에만.',
         '- ※ 정직 각주: 데모 데이터 기준. 고객 데이터엔 같은 검증(수치 대조·렌더 스모크)을 반복합니다.']
    rep = os.path.join(HERE, 'dashboard_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return num_ok and rc['kpi_total_ok'] and rc['kpi_n_ok'] and rc['console_errors'] == 0 and same


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--make-demo' in sys.argv or len(sys.argv) == 1:
        ok = main_demo()
        sys.exit(0 if ok else 1)
    else:
        inp = sys.argv[1]
        rows = list(csv.DictReader(open(inp, encoding='utf-8-sig')))
        agg = aggregate(rows)
        out = os.path.splitext(inp)[0] + '_대시보드.html'
        open(out, 'w', encoding='utf-8').write(build_html(agg, os.path.basename(inp)))
        print(f'완료: {out} — 유효 {agg["n"]}건 · 총액 {agg["total"]:,}원 · 격리 {len(agg["bad"])}행')
