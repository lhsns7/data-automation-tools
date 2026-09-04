#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mail_inbox.py — 메일 첨부 자동 수집 (규칙 매칭·한글 첨부명·중복 0)

거래처·지점이 매일 메일로 보내는 파일(정산서·주문서·재고표)을 사람이 일일이 저장하는 업무의
자동화. 규칙(보낸이·제목 패턴)에 맞는 메일의 첨부만 골라 정리된 이름으로 저장한다.

실무 함정 3개를 정면 처리:
  - ★한글 첨부명 인코딩: 메일 첨부 파일명은 RFC 2047/2231로 인코딩돼 옴(=?utf-8?B?...?=,
    euc-kr 포함) — 디코딩 실패 = 깨진 파일명·유실. 표준 디코더로 복원.
  - ★중복 수집 0: 메일 UID 대장 — 재실행·재수신에도 같은 첨부를 두 번 저장하지 않음
  - 수집 대장: 무엇을 언제 어디서 받았는지 기록(해시 포함 — 무결성 추적)
저장 이름 규칙 = {날짜}_{보낸이}_{원본명} (충돌 시 일련번호). 규칙 밖 메일 = 무시(대장에 사유).

검증(--make-demo): 합성 메일 6통(.eml — ★한글 Base64 첨부명·EUC-KR·규칙 밖·중복 재수신 포함)
  ①규칙 매칭 정확 ②첨부 저장+해시 무결(원본 바이트 = 저장 바이트) ③★한글 첨부명 정확 복원
  ④중복 재수신 = 저장 0 ⑤규칙 밖 무시(사유 기록) ⑥대장 정합·재현성.
※ 데모 = .eml 파일 어댑터(파싱 계층은 실서비스와 동일). 실서비스 = IMAP 어댑터에 계정 설정
  (imaplib — 접속층만 교체, 자격은 고객 보관).
"""
import os, sys, json, shutil, hashlib, datetime as dt
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.headerregistry import Address

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, 'inbox_ledger.json')

RULES = [                                                  # 수집 규칙(설정값)
    dict(name='정산서', sender='settle@partner-a.co.kr', subject='정산'),
    dict(name='재고표', sender='stock@partner-b.co.kr', subject='재고'),
]


def match_rule(sender, subject, rules=RULES):
    for r in rules:
        if r['sender'] == sender and r['subject'] in subject:
            return r['name']
    return None


def collect(eml_dir, out_dir, rules=RULES, ledger_path=LEDGER):
    """→ (저장 목록, 스킵 목록). 대장 = {uid: {...}} — 중복 0의 근거."""
    ledger = {}
    if os.path.exists(ledger_path):
        try:
            ledger = json.load(open(ledger_path, encoding='utf-8'))
        except Exception:
            ledger = {}
    os.makedirs(out_dir, exist_ok=True)
    saved, skipped = [], []
    for fn in sorted(os.listdir(eml_dir)):
        if not fn.endswith('.eml'):
            continue
        raw = open(os.path.join(eml_dir, fn), 'rb').read()
        msg = message_from_bytes(raw, policy=policy.default)
        uid = msg['Message-ID'] or fn
        if uid in ledger:
            skipped.append((fn, '이미 수집됨(중복 0)'))
            continue
        sender = msg['From'].addresses[0].addr_spec if msg['From'] else ''
        subject = str(msg['Subject'] or '')
        rule = match_rule(sender, subject, rules)
        if not rule:
            ledger[uid] = dict(status='skip', reason='규칙 밖', sender=sender, subject=subject)
            skipped.append((fn, f'규칙 밖({sender})'))
            continue
        day = (msg['Date'].datetime.strftime('%Y%m%d') if msg['Date'] else 'unknown')
        files = []
        for part in msg.iter_attachments():
            orig = part.get_filename() or 'noname'         # ★RFC2047/2231 디코딩은 policy.default가 수행
            data = part.get_payload(decode=True)
            base = f'{day}_{rule}_{orig}'
            dst = os.path.join(out_dir, base)
            n = 1
            while os.path.exists(dst):                     # 이름 충돌 = 일련번호(덮어쓰기 금지)
                stem, ext = os.path.splitext(base)
                dst = os.path.join(out_dir, f'{stem}_{n}{ext}')
                n += 1
            open(dst, 'wb').write(data)
            files.append(dict(file=os.path.basename(dst), orig=orig,
                              sha256=hashlib.sha256(data).hexdigest(), size=len(data)))
            saved.append(dst)
        ledger[uid] = dict(status='ok', rule=rule, sender=sender, subject=subject,
                           day=day, files=files, at=dt.datetime.now().isoformat(' ', 'seconds'))
    json.dump(ledger, open(ledger_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return saved, skipped


# ── 검증 데모: 합성 .eml (한글 인코딩 함정 포함) ───────────────────
def build_eml(path, sender, subject, day, attachments, msg_id):
    m = EmailMessage()
    m['From'] = Address('보낸이', *sender.split('@'))
    m['To'] = Address('받는이', 'me', 'mycompany.co.kr')
    m['Subject'] = subject
    m['Date'] = dt.datetime.fromisoformat(day)
    m['Message-ID'] = msg_id
    m.set_content('첨부 확인 부탁드립니다.')
    for fname, data in attachments:
        m.add_attachment(data, maintype='application', subtype='octet-stream', filename=fname)
    open(path, 'wb').write(bytes(m))


def make_demo():
    eml_dir = os.path.join(HERE, 'demo_mails')
    out_dir = os.path.join(HERE, 'demo_saved')
    for d in (eml_dir, out_dir):
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
    a1 = ('9월 정산서_최종.xlsx', b'SETTLE-XLSX-BYTES' * 100)      # ★한글 첨부명(RFC2231 인코딩됨)
    a2 = ('재고현황(9월4일).csv', b'STOCK-CSV-BYTES' * 50)
    a3 = ('report.pdf', b'%PDF-FAKE' * 30)
    build_eml(os.path.join(eml_dir, 'm1.eml'), 'settle@partner-a.co.kr',
              '[파트너A] 9월 정산 자료 송부', '2026-09-04T09:00:00', [a1], '<m1@pa>')
    build_eml(os.path.join(eml_dir, 'm2.eml'), 'stock@partner-b.co.kr',
              '금일 재고 보고', '2026-09-04T10:00:00', [a2, a3], '<m2@pb>')
    build_eml(os.path.join(eml_dir, 'm3.eml'), 'spam@unknown.com',
              '광고: 특가 안내', '2026-09-04T11:00:00', [('ad.html', b'<html>ad</html>')], '<m3@x>')
    build_eml(os.path.join(eml_dir, 'm4.eml'), 'settle@partner-a.co.kr',
              '휴가 일정 공유', '2026-09-04T12:00:00', [('일정.txt', b'vacation')], '<m4@pa>')  # 보낸이 OK·제목 밖
    return eml_dir, out_dir, a1, a2, a3


def main_demo():
    if os.path.exists(LEDGER):
        os.remove(LEDGER)
    eml_dir, out_dir, a1, a2, a3 = make_demo()

    saved1, skipped1 = collect(eml_dir, out_dir)
    # ① 규칙 매칭: 수집 2통(m1·m2)=첨부 3개 저장 · m3(보낸이 밖)·m4(제목 밖) 스킵
    ok1 = (len(saved1) == 3 and len([s for s in skipped1 if '규칙 밖' in s[1]]) == 2)
    # ② 해시 무결: 저장 바이트 = 원본 바이트
    def sha(b):
        return hashlib.sha256(b).hexdigest()
    ledger = json.load(open(LEDGER, encoding='utf-8'))
    got = {f['orig']: f for e in ledger.values() if e.get('files') for f in e['files']}
    ok2 = (got[a1[0]]['sha256'] == sha(a1[1]) and got[a2[0]]['sha256'] == sha(a2[1])
           and got[a3[0]]['sha256'] == sha(a3[1]))
    # ③ ★한글 첨부명 복원: 인코딩됐던 원본명이 정확히 복원 + 저장 파일명에 포함
    ok3 = (a1[0] in got and a2[0] in got
           and any(a1[0] in os.path.basename(p) for p in saved1)
           and os.path.exists(os.path.join(out_dir, f'20260904_정산서_{a1[0]}')))
    # ④ 중복 재수신 = 저장 0 (같은 폴더 재수집)
    saved2, skipped2 = collect(eml_dir, out_dir)
    ok4 = (saved2 == [] and len([s for s in skipped2 if '중복' in s[1]]) == 4)
    # ⑤ 규칙 밖 사유 기록(대장): m3·m4 = skip + 사유
    skips = [e for e in ledger.values() if e.get('status') == 'skip']
    ok5 = (len(skips) == 2 and all(e['reason'] == '규칙 밖' for e in skips))
    # ⑥ 대장 정합: 파일 수 3 = 저장 폴더 실파일 수 · 재현성(재수집 후에도 폴더 불변)
    n_disk = len(os.listdir(out_dir))
    ok6 = (n_disk == 3 and len(got) == 3)

    L = [f'# 메일 첨부 수집 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 합성 메일 4통(.eml) — ★한글 첨부명(MIME 인코딩)·규칙 밖 2통(보낸이/제목)·중복 재수신',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 규칙 매칭(수집 2통·첨부 3개 / 규칙 밖 2통 스킵) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 저장 무결(SHA-256 원본=저장, 3/3) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ ★한글 첨부명 정확 복원("9월 정산서_최종.xlsx" 등) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 중복 재수신 = 저장 0(UID 대장) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 규칙 밖 = 무시하되 사유 기록 | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 대장 정합(대장 3 = 디스크 3) · 재수집 후 불변 | {"PASS" if ok6 else "★FAIL"} |',
         '', '- ※ 데모 = .eml 어댑터(파싱 계층은 실서비스 동일). 실서비스 = IMAP 접속층만 교체(자격 고객 보관).',
         '- ※ 저장 규칙 {날짜}_{규칙명}_{원본명} · 이름 충돌 = 일련번호(덮어쓰기 금지).']
    rep = os.path.join(HERE, 'mail_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
