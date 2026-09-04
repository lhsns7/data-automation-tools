#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_mask.py — 개인정보 마스킹·일관 가명화 (잔존 0 재스캔 증명)

운영 데이터를 개발·테스트·외부 공유용으로 넘길 때의 처리기. 흔한 사고 두 가지를 막는다:
  ① 숨은 개인정보 — 컬럼명이 '비고'인데 값이 전화번호인 것들 (컬럼명 힌트 + ★값 패턴 이중 스캔)
  ② "마스킹했다"는 착각 — 처리 후 출력 전체를 다시 스캔해 **원본 개인정보 잔존 0을 증명**해야 완료

마스킹 방식(항목별):
  - 전화 010-****-5678(형식 보존) · 이메일 k***@도메인(도메인 보존) · 주민번호 앞6+******* ·
    계좌 뒤4만 · 주소 시/구까지
  - ★이름 = 일관 가명화(솔트 해시): 같은 사람 → 같은 가명 → **여러 파일 간 조인 관계 보존**.
    가명↔원본 매핑은 출력에 넣지 않고 별도 파일(분리 보관 경고 동봉)

검증(--make-demo): ①심은 개인정보 전수 검출(정규 컬럼 6종 + ★'비고' 속 숨은 전화) ②처리 후
  재스캔 잔존 0 ③형식 보존(마스킹된 전화도 전화 형식·이메일 도메인 유지) ④일관 가명화(같은 이름
  =같은 가명, 2파일 교차 일치 = 조인 보존) ⑤비대상 컬럼 무변경·행수 보존 ⑥재현성(같은 솔트=같은
  출력·다른 솔트=다른 가명) ⑦매핑 분리(출력에 원본 이름 0).
※ ★정직선: 이 도구는 **가명처리(pseudonymization)**다 — 완전 익명화(재식별 불가) 보장이 아니며,
  조합 재식별 위험은 데이터 맥락에 따라 남는다. 법적 충분성 판단은 개인정보 전문가 검토 사안.
"""
import os, sys, csv, re, hmac, hashlib, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))

P_PHONE = re.compile(r'01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}')
P_EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
P_RRN = re.compile(r'\d{6}[-.\s]?[1-4]\d{6}')              # 주민등록번호
NAME_COLS = ('이름', '성명', '고객명', '담당자', '수취인')
ACCT_COLS = ('계좌', '계좌번호')
ADDR_COLS = ('주소', '배송지')

성 = '김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노정'
이름자 = '민서준우하은지호도윤서연시우아린주원예은건우다인선재유나'


def pseudonym(name, salt):
    """일관 가명: HMAC(salt, 원본) → 한국식 가명. 같은 원본=같은 가명, 솔트 다르면 다른 가명."""
    h = hmac.new(salt.encode(), name.encode(), hashlib.sha256).digest()
    return 성[h[0] % len(성)] + 이름자[h[1] % len(이름자)] + 이름자[h[2] % len(이름자)]


def mask_phone(m):
    d = re.sub(r'\D', '', m.group(0))
    return f'{d[:3]}-****-{d[-4:]}'


def mask_email(m):
    local, dom = m.group(0).split('@', 1)
    return (local[0] + '***@' + dom) if local else m.group(0)


def mask_rrn(m):
    d = re.sub(r'\D', '', m.group(0))
    return d[:6] + '-*******'


def mask_value(col, val, salt, mapping):
    """컬럼명 힌트 + 값 패턴 이중 적용. 반환 (마스킹값, 검출항목 set)."""
    found = set()
    v = str(val)
    if any(k in col for k in NAME_COLS) and v.strip():
        found.add('이름')
        pn = pseudonym(v.strip(), salt)
        mapping[v.strip()] = pn
        return pn, found
    if any(k in col for k in ACCT_COLS) and re.search(r'\d{6,}', v):
        found.add('계좌')
        d = re.sub(r'\D', '', v)
        return '****' + d[-4:], found
    if any(k in col for k in ADDR_COLS) and v.strip():
        found.add('주소')
        parts = v.split()
        return ' '.join(parts[:2]) + (' 이하 생략' if len(parts) > 2 else ''), found
    if P_RRN.search(v):                                    # 값 패턴(컬럼명 무관 — 숨은 개인정보)
        found.add('주민번호')
        v = P_RRN.sub(mask_rrn, v)
    if P_PHONE.search(v):
        found.add('전화')
        v = P_PHONE.sub(mask_phone, v)
    if P_EMAIL.search(v):
        found.add('이메일')
        v = P_EMAIL.sub(mask_email, v)
    return v, found


def mask_file(src, dst, salt, mapping):
    """CSV 1개 처리 → (행수, 검출 항목 카운트)"""
    rows = list(csv.DictReader(open(src, encoding='utf-8-sig')))
    hits = {}
    out_rows = []
    for r in rows:
        nr = {}
        for c, v in r.items():
            nv, found = mask_value(c, v, salt, mapping)
            nr[c] = nv
            for f in found:
                hits[f] = hits.get(f, 0) + 1
        out_rows.append(nr)
    with open(dst, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    return len(rows), hits


def residual_scan(path, originals):
    """★잔존 검사: 출력 파일에서 개인정보 패턴 + 원본 문자열(이름 등) 잔존 수. 0이어야 완료."""
    text = open(path, encoding='utf-8-sig').read()
    n = len(P_RRN.findall(text)) + len(P_PHONE.findall(text)) + len(P_EMAIL.findall(text))
    n += sum(1 for o in originals if o and o in text)
    return n


# ── 검증 데모 ───────────────────────────────────────────────────────
def make_demo():
    names = ['홍길동', '김영희', '박철수', '홍길동', '이순신', '김영희']   # 중복 = 일관성 검증용
    c_path = os.path.join(HERE, 'demo_customers.csv')
    with open(c_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['고객명', '전화번호', '이메일', '주민번호', '계좌번호', '주소', '가입일', '등급', '비고'])
        for i, nm in enumerate(names):
            w.writerow([nm, f'010-12{i:02d}-34{i:02d}', f'user{i}@example.com',
                        f'90010{i}-1{i:06d}', f'110-234-56789{i}',
                        '서울시 강남구 테헤란로 123 45동 678호', '2026-01-15', 'VIP' if i % 2 else '일반',
                        f'재연락 요망 010-99{i:02d}-88{i:02d}' if i == 2 else '특이사항 없음'])  # ★숨은 전화
    o_path = os.path.join(HERE, 'demo_orders.csv')
    with open(o_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['주문번호', '고객명', '상품', '금액'])
        for i, nm in enumerate(names):
            w.writerow([f'O{i:03d}', nm, '상품A', 12_000 + i * 1000])
    return c_path, o_path, names


def main_demo():
    c_path, o_path, names = make_demo()
    salt = 'demo-salt-2026'
    mapping = {}
    mc = os.path.join(HERE, 'masked_customers.csv')
    mo = os.path.join(HERE, 'masked_orders.csv')
    n1, hits1 = mask_file(c_path, mc, salt, mapping)
    n2, hits2 = mask_file(o_path, mo, salt, mapping)

    # ① 심은 개인정보 전수 검출: 이름6·전화6+★숨은1=7·이메일6·주민6·계좌6·주소6
    ok1 = (hits1.get('이름') == 6 and hits1.get('전화') == 7 and hits1.get('이메일') == 6
           and hits1.get('주민번호') == 6 and hits1.get('계좌') == 6 and hits1.get('주소') == 6
           and hits2.get('이름') == 6)
    # ② 잔존 0: 출력 2파일 재스캔 — 패턴·원본 이름 잔존 0
    res = residual_scan(mc, set(names)) + residual_scan(mo, set(names))
    ok2 = (res == 0)
    # ③ 형식 보존: 마스킹된 전화 '010-****-3400' 형식 · 이메일 도메인 보존
    rows_c = list(csv.DictReader(open(mc, encoding='utf-8-sig')))
    ok3 = (all(re.fullmatch(r'010-\*{4}-\d{4}', r['전화번호']) for r in rows_c)
           and all(r['이메일'].endswith('@example.com') and '***' in r['이메일'] for r in rows_c))
    # ④ 일관 가명화 + 2파일 교차(조인 보존): 홍길동 2회=같은 가명, 고객↔주문 같은 행 같은 가명
    rows_o = list(csv.DictReader(open(mo, encoding='utf-8-sig')))
    pseudo_c = [r['고객명'] for r in rows_c]
    pseudo_o = [r['고객명'] for r in rows_o]
    ok4 = (pseudo_c == pseudo_o and pseudo_c[0] == pseudo_c[3] and pseudo_c[1] == pseudo_c[5]
           and len({pseudo_c[0], pseudo_c[1], pseudo_c[2], pseudo_c[4]}) == 4)
    # ⑤ 비대상 무변경·행수: 금액·가입일·등급 원본 동일, 행수 6/6
    src_c = list(csv.DictReader(open(c_path, encoding='utf-8-sig')))
    src_o = list(csv.DictReader(open(o_path, encoding='utf-8-sig')))
    ok5 = (n1 == 6 and n2 == 6
           and [r['가입일'] for r in rows_c] == [r['가입일'] for r in src_c]
           and [r['등급'] for r in rows_c] == [r['등급'] for r in src_c]
           and [r['금액'] for r in rows_o] == [r['금액'] for r in src_o])
    # ⑥ 재현성: 같은 솔트 = 같은 가명 / 다른 솔트 = 다른 가명(재식별 방어)
    same = pseudonym('홍길동', salt) == pseudo_c[0]
    diff = pseudonym('홍길동', 'other-salt') != pseudo_c[0]
    ok6 = (same and diff)
    # ⑦ 매핑 분리: 매핑에 원본 4명 존재하되 출력 파일엔 없음(②로 증명) — 별도 저장+경고
    map_path = os.path.join(HERE, 'pseudonym_map.csv')
    with open(map_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['원본', '가명', '※ 이 파일 = 재식별 키. 마스킹 산출물과 분리 보관·전달 금지'])
        w.writerows([o, p, ''] for o, p in sorted(mapping.items()))
    ok7 = (len(mapping) == 4)

    L = [f'# 개인정보 마스킹 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 고객 6행(항목 6종+★비고 속 숨은 전화)+주문 6행(이름 공유 — 조인 보존 검증) · 처리 후 재스캔',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 심은 개인정보 전수 검출(이름6·전화 6+숨은1·이메일6·주민6·계좌6·주소6) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② ★처리 후 재스캔 잔존 {res}건(패턴+원본 이름) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 형식 보존(전화 010-****-nnnn · 이메일 도메인 유지) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ ★일관 가명화(같은 사람=같은 가명 · 2파일 교차 일치=조인 보존) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 비대상 컬럼 무변경 · 행수 보존(6/6) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 재현성(같은 솔트=같은 가명 · 다른 솔트=다른 가명) | {"PASS" if ok6 else "★FAIL"} |',
         f'| ⑦ 매핑 분리(가명↔원본 = 별도 파일 + 분리 보관 경고) | {"PASS" if ok7 else "★FAIL"} |',
         '', '- ※ ★정직선: 본 도구 = **가명처리** — 완전 익명화(재식별 불가) 보장 아님. 조합 재식별 위험은',
         '  데이터 맥락에 따라 남으며, 법적 충분성 판단은 개인정보 전문가 검토 사안.',
         '- ※ 마스킹 방식·대상 컬럼·솔트 = 설정값(고객 데이터 구조에 1회 맞춤).']
    rep = os.path.join(HERE, 'mask_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
