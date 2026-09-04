#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""invoice_desk.py — 견적·청구서 발행 + 미수금 추적

프리랜서·소상공인의 실제 문제는 청구서 "만들기"가 아니라 **받기**다 — 누가 얼마를 아직 안 냈고,
언제부터 밀렸는지. 이 도구는 발행(채번·부가세·인쇄용 문서)과 수금(입금 대조·잔액·연체)을 한 대장으로 잇는다.

기능:
  - 발행: 품목 → 공급가액·부가세(10%)·합계(항등: 공급가액=round(합계/1.1)) · ★자동 채번(연도-일련,
    중복 0·결번 0) · 인쇄용 HTML 문서(브라우저 인쇄→PDF)
  - 대장(SQLite): 발행/부분입금/완납/연체 상태 추적
  - 입금 대조: 입금(일자·입금자·금액) ↔ 청구서 매칭 — ★부분 입금 = 잔액 추적, 매칭 불가 = 격리(묵살 금지)
  - ★연체 판정: 지급기한 경과 + 미완납 → 경과일·잔액 리포트 + 만기 도래 예정표

검증(--make-demo): ①발행 수기(공급가·부가세·합계 항등) ②채번 20건 중복 0·결번 0
  ③부분 입금 궤적(부분→완납, 잔액 수기) ④심은 입금 6건 분류(정확4·부분1·격리1)
  ⑤연체 판정(가상 시계 — 경과일 정확·미도래=예정) ⑥항등 2종+재현성 ⑦HTML 문서 실물.
※ 부가세율·지급기한 = 설정값. 발행·수금 관리 자동화이며 회계·세무 자문 아님.
"""
import os, sys, html, sqlite3, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, 'invoices.db')
VAT = 1.1
DUE_DAYS = 14                                              # 지급기한(설정값)


def open_db(path=DB):
    con = sqlite3.connect(path)
    con.execute('''CREATE TABLE IF NOT EXISTS invoices(
        no TEXT PRIMARY KEY, client TEXT NOT NULL, issued TEXT NOT NULL, due TEXT NOT NULL,
        supply INT NOT NULL, vat INT NOT NULL, total INT NOT NULL, paid INT NOT NULL DEFAULT 0)''')
    con.execute('''CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, payer TEXT, amount INT,
        matched_no TEXT, note TEXT)''')
    return con


def next_no(con, year):
    """채번: {연도}-{일련4자리} — 대장 최대값+1(중복·결번 0)."""
    row = con.execute("SELECT no FROM invoices WHERE no LIKE ? ORDER BY no DESC LIMIT 1",
                      (f'{year}-%',)).fetchone()
    seq = int(row[0].split('-')[1]) + 1 if row else 1
    return f'{year}-{seq:04d}'


def issue(con, client, items, issued, due_days=DUE_DAYS):
    """items = [(품목, 수량, 단가)] — 합계(부가세 포함가) 기준. 공급가액=round(합계/1.1) 항등."""
    total = sum(q * p for _, q, p in items)
    supply = round(total / VAT)
    vat = total - supply
    no = next_no(con, issued[:4])
    due = (dt.date.fromisoformat(issued) + dt.timedelta(days=due_days)).isoformat()
    con.execute('INSERT INTO invoices(no, client, issued, due, supply, vat, total) VALUES(?,?,?,?,?,?,?)',
                (no, client, issued, due, supply, vat, total))
    con.commit()
    write_doc(no, client, items, issued, due, supply, vat, total)
    return no, supply, vat, total


def write_doc(no, client, items, issued, due, supply, vat, total):
    rows = ''.join(f'<tr><td>{html.escape(n)}</td><td>{q}</td><td>{p:,}</td><td>{q*p:,}</td></tr>'
                   for n, q, p in items)
    doc = f"""<meta charset="utf-8"><title>청구서 {no}</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;max-width:640px;margin:30px auto;color:#14181f}}
h1{{font-size:22px;border-bottom:3px solid #365314;padding-bottom:8px}}
table{{border-collapse:collapse;width:100%;margin:14px 0}}td,th{{border:1px solid #cfd6de;padding:8px 10px;font-size:13.5px;text-align:right}}
th{{background:#f2f4f8}}td:first-child,th:first-child{{text-align:left}}
.sum td{{font-weight:800}}.meta{{font-size:13px;color:#5b6472}}</style>
<h1>청 구 서 <span style="float:right;font-size:14px">{no}</span></h1>
<p class="meta">받는 분: <b>{html.escape(client)}</b> · 발행일 {issued} · 지급기한 <b>{due}</b></p>
<table><tr><th>품목</th><th>수량</th><th>단가</th><th>금액</th></tr>{rows}
<tr class="sum"><td colspan="3">공급가액</td><td>{supply:,}</td></tr>
<tr class="sum"><td colspan="3">부가세(10%)</td><td>{vat:,}</td></tr>
<tr class="sum"><td colspan="3">합계</td><td>{total:,}원</td></tr></table>
<p class="meta">본 문서는 자동 발행되었습니다. 문의는 발행처로 연락 주세요.</p>"""
    open(os.path.join(HERE, f'청구서_{no}.html'), 'w', encoding='utf-8').write(doc)


def apply_payment(con, day, payer, amount):
    """입금 1건 매칭: ①입금자=고객명 & 미수 잔액 있는 가장 오래된 청구서 ②금액 일치 우선.
    부분 입금 허용(잔액 추적). 매칭 불가 = 격리(matched_no NULL)."""
    cands = con.execute('''SELECT no, total, paid FROM invoices
        WHERE client=? AND paid < total ORDER BY issued''', (payer,)).fetchall()
    target = None
    for no, total, paid in cands:                          # 금액 정확 일치(잔액=입금) 우선
        if total - paid == amount:
            target = (no, total, paid)
            break
    if target is None and cands:
        target = cands[0]                                  # 아니면 가장 오래된 미수 건에 충당
    if target is None:
        con.execute('INSERT INTO payments(day,payer,amount,matched_no,note) VALUES(?,?,?,NULL,?)',
                    (day, payer, amount, '매칭 불가 — 확인 필요'))
        con.commit()
        return None
    no, total, paid = target
    if paid + amount > total:                              # 과입금 = 격리(묵살 금지)
        con.execute('INSERT INTO payments(day,payer,amount,matched_no,note) VALUES(?,?,?,NULL,?)',
                    (day, payer, amount, f'과입금({no} 잔액 {total-paid:,} 초과) — 확인 필요'))
        con.commit()
        return None
    con.execute('UPDATE invoices SET paid=paid+? WHERE no=?', (amount, no))
    con.execute('INSERT INTO payments(day,payer,amount,matched_no,note) VALUES(?,?,?,?,?)',
                (day, payer, amount, no, ''))
    con.commit()
    return no


def status_of(inv, today):
    no, client, issued, due, supply, vat, total, paid = inv
    if paid >= total:
        return '완납'
    if dt.date.fromisoformat(due) < today:
        return '연체'
    return '부분입금' if paid > 0 else '발행'


def report(con, today):
    """→ (대장 rows, 연체 rows, 예정 rows, 격리 rows)"""
    invs = con.execute('SELECT * FROM invoices ORDER BY no').fetchall()
    rows, overdue, upcoming = [], [], []
    for inv in invs:
        no, client, issued, due, supply, vat, total, paid = inv
        st = status_of(inv, today)
        rows.append([no, client, issued, due, total, paid, total - paid, st])
        if st == '연체':
            overdue.append([no, client, due, (today - dt.date.fromisoformat(due)).days, total - paid])
        elif st in ('발행', '부분입금'):
            upcoming.append([no, client, due, total - paid])
    quar = [list(r) for r in con.execute(
        'SELECT day, payer, amount, note FROM payments WHERE matched_no IS NULL')]
    return rows, overdue, upcoming, quar


# ── 검증 데모 ───────────────────────────────────────────────────────
def main_demo():
    if os.path.exists(DB):
        os.remove(DB)
    for f in os.listdir(HERE):
        if f.startswith('청구서_') and f.endswith('.html'):
            os.remove(os.path.join(HERE, f))
    con = open_db()
    today = dt.date(2026, 9, 4)

    # ① 발행 수기: 품목 3개 → 합계 1,100,000 · 공급가 1,000,000 · 부가세 100,000
    no1, s1, v1, t1 = issue(con, '한빛상사', [('데이터 정리 자동화', 1, 770_000),
                                          ('월간 리포트 설정', 1, 220_000),
                                          ('교육 1회', 1, 110_000)], '2026-08-10')
    ok1 = (t1 == 1_100_000 and s1 == 1_000_000 and v1 == 100_000 and s1 + v1 == t1)

    # 추가 발행(연체·예정·부분입금 시나리오용)
    no2 = issue(con, '누리물산', [('수집기 구축', 1, 2_200_000)], '2026-08-12')[0]    # 기한 8/26 → 연체 후보
    no3 = issue(con, '바다상회', [('대시보드 제작', 1, 550_000)], '2026-08-30')[0]     # 기한 9/13 → 예정
    no4 = issue(con, '한빛상사', [('추가 수정', 1, 330_000)], '2026-09-01')[0]

    # ② 채번: 총 20건까지 발행 → 중복 0·결번 0
    for i in range(16):
        issue(con, f'테스트{i}', [('항목', 1, 110_000)], '2026-09-02')
    nos = [r[0] for r in con.execute('SELECT no FROM invoices ORDER BY no')]
    ok2 = (len(nos) == 20 and len(set(nos)) == 20
           and nos == [f'2026-{i:04d}' for i in range(1, 21)])

    # ③④ 입금 6건: 정확4 · 부분1 · 매칭불가1
    m1 = apply_payment(con, '2026-08-20', '누리물산', 2_200_000)       # 정확(완납)
    m2 = apply_payment(con, '2026-09-01', '한빛상사', 500_000)         # ★부분(no1 잔액 600,000)
    m3 = apply_payment(con, '2026-09-02', '한빛상사', 600_000)         # 정확(잔액 일치 → no1 완납)
    m4 = apply_payment(con, '2026-09-03', '바다상회', 550_000)         # 정확(완납)
    m5 = apply_payment(con, '2026-09-03', '한빛상사', 330_000)         # 정확(no4 완납)
    m6 = apply_payment(con, '2026-09-03', '모르는입금자', 990_000)      # 격리
    inv1 = con.execute('SELECT total, paid FROM invoices WHERE no=?', (no1,)).fetchone()
    ok3 = (m2 == no1 and m3 == no1 and inv1 == (1_100_000, 1_100_000))  # 부분→완납 궤적
    ok4 = (m1 == no2 and m4 == no3 and m5 == no4 and m6 is None)

    # ⑤ 연체 판정(가상 오늘 9/4): 테스트 16건(기한 9/16)=예정 · no2 완납 · 연체 = 없어야 하나…
    #    연체 실증을 위해 미납 연체 1건 추가 발행(과거일)
    no5 = issue(con, '지연고객', [('작업', 1, 440_000)], '2026-08-01')[0]  # 기한 8/15 → 연체 20일
    rows, overdue, upcoming, quar = report(con, today)
    ok5 = (len(overdue) == 1 and overdue[0][0] == no5 and overdue[0][3] == 20
           and overdue[0][4] == 440_000 and len(upcoming) == 16)

    # ⑥ 항등: Σ매칭 입금 = Σpaid / 격리 1건(990,000) / 재현성
    tot_matched = con.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE matched_no IS NOT NULL').fetchone()[0]
    tot_paid = con.execute('SELECT SUM(paid) FROM invoices').fetchone()[0]
    rows2 = report(con, today)[0]
    ok6 = (tot_matched == tot_paid and len(quar) == 1 and quar[0][2] == 990_000 and rows == rows2)

    # ⑦ HTML 문서 실물: no1 파일 존재 + 핵심 수치 포함
    doc = open(os.path.join(HERE, f'청구서_{no1}.html'), encoding='utf-8').read()
    ok7 = ('1,000,000' in doc and '100,000' in doc and '1,100,000' in doc and '한빛상사' in doc)

    con.close()
    L = [f'# 청구·미수금 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 발행 21건 · 입금 6건(정확4·★부분1·격리1) · 연체·예정 심음 · 가상 오늘 = 2026-09-04',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 발행 수기(합계 1,100,000 → 공급가 1,000,000·부가세 100,000 항등) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 채번 20건 — 중복 0·결번 0(2026-0001~0020) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ ★부분 입금 궤적(500,000 부분 → 600,000 완납, 잔액 수기) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 입금 매칭 분류(정확 4 · 격리 1 — 모르는 입금자) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ ★연체 판정(기한 8/15 → 경과 20일·잔액 440,000) + 예정 {len(upcoming)}건 | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 항등(Σ매칭입금 {tot_matched:,}=Σ충당) · 격리 1건 · 재현성 | {"PASS" if ok6 else "★FAIL"} |',
         f'| ⑦ 인쇄용 HTML 문서 실물(수치·수신자 포함) | {"PASS" if ok7 else "★FAIL"} |',
         '', '- ※ 부가세율·지급기한 = 설정값. 발행·수금 관리 자동화이며 회계·세무 자문 아님.',
         '- ※ 과입금·모르는 입금은 대장에 충당하지 않고 격리(묵살 금지) — 돈 문제는 조용히 넘기지 않는다.']
    rep = os.path.join(HERE, 'invoice_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
