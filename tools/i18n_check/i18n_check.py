#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""i18n_check.py — 번역 파일 정합 검사 (키·미번역·플레이스홀더)

다국어 서비스의 번역 파일(JSON)은 조용히 어긋난다: 키가 빠지고(런타임에 키 이름이 노출),
번역을 잊어 기준 언어가 그대로 남고, **플레이스홀더가 어긋나**({name}이 빠진 번역 = 깨진 문장
또는 크래시). 이 도구는 기준 언어 대비 대상 언어들을 전수 대조한다.

검사 5종:
  ① 누락 키(대상에 없음 — 런타임 키 노출) ② 고아 키(대상에만 있음 — 죽은 번역)
  ③ 미번역(값이 기준과 동일 — 복붙 방치. 브랜드명 등 허용 목록 예외)
  ④ ★플레이스홀더 불일치({name}·{0}·%s 집합이 기준과 다름 — 크래시·깨진 문장 원인)
  ⑤ 빈 값
산출 = 언어별 완성도 % + 항목별 목록(키·기대·실제). 중첩 JSON 지원(점 표기 평탄화).

검증(--make-demo) = 정답 선작성: ko 30키 기준, en에 결함 5종(누락3·고아2·미번역4 중 허용1·
  플레이스홀더 2·빈값1) 심음 / ja = 클린 → ①~⑤ 전수 정확 ⑥클린 언어 오탐 0 + 재현성.
※ 포맷 = JSON 기본(properties·CSV 어댑터 1회 맞춤). 허용 목록·플레이스홀더 패턴 = 설정값.
"""
import os, sys, re, json, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
PLACEHOLDER = re.compile(r'\{[a-zA-Z0-9_]+\}|%[sd]|%\d+\$[sd]')
ALLOW_SAME = {'brand.name'}                                # 미번역 허용(브랜드명 등, 설정값)


def flatten(d, prefix=''):
    out = {}
    for k, v in d.items():
        key = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = '' if v is None else str(v)
    return out


def check(base_path, target_path, allow_same=ALLOW_SAME):
    base = flatten(json.load(open(base_path, encoding='utf-8')))
    tgt = flatten(json.load(open(target_path, encoding='utf-8')))
    missing = sorted(set(base) - set(tgt))
    orphan = sorted(set(tgt) - set(base))
    untranslated, ph_mismatch, empty = [], [], []
    for k in sorted(set(base) & set(tgt)):
        b, t = base[k], tgt[k]
        if not t.strip():
            empty.append(k)
            continue
        if b == t and k not in allow_same and b.strip():
            untranslated.append(k)
        pb, pt = sorted(PLACEHOLDER.findall(b)), sorted(PLACEHOLDER.findall(t))
        if pb != pt:
            ph_mismatch.append((k, pb, pt))
    n_ok = len(set(base) & set(tgt)) - len(untranslated) - len(empty) - len(ph_mismatch)
    total = len(base)
    pct = max(0, n_ok) / total * 100 if total else 100
    return dict(missing=missing, orphan=orphan, untranslated=untranslated,
                ph_mismatch=ph_mismatch, empty=empty, pct=round(pct, 1),
                n_base=len(base), n_target=len(tgt))


def report_text(lang, r):
    L = [f'[{lang}] 완성도 {r["pct"]}% (기준 {r["n_base"]}키)']
    for label, items in (('누락 키(런타임 노출 위험)', r['missing']),
                         ('고아 키(죽은 번역)', r['orphan']),
                         ('미번역(기준과 동일)', r['untranslated']),
                         ('빈 값', r['empty'])):
        if items:
            L.append(f'  ★{label} {len(items)}: ' + ', '.join(items[:8]))
    for k, pb, pt in r['ph_mismatch']:
        L.append(f'  ★플레이스홀더 불일치 [{k}]: 기준 {pb} vs 대상 {pt}')
    if not any((r['missing'], r['orphan'], r['untranslated'], r['empty'], r['ph_mismatch'])):
        L.append('  문제 없음')
    return '\n'.join(L)


# ── 검증 데모 (정답 선작성) ────────────────────────────────────────
def make_demo():
    ko = {'brand': {'name': 'ColTools'},
          'auth': {'login': '로그인', 'logout': '로그아웃', 'welcome': '{name}님 환영합니다',
                   'error': '오류가 발생했습니다 (코드 %s)', 'retry': '다시 시도'},
          'cart': {'add': '담기', 'remove': '빼기', 'count': '상품 {count}개',
                   'total': '합계 {total}원', 'empty': '장바구니가 비었습니다'},
          'order': {'submit': '주문하기', 'cancel': '주문 취소', 'status': '주문 상태',
                    'shipped': '{date}에 발송됨', 'refund': '환불 신청'},
          'common': {'ok': '확인', 'no': '취소', 'save': '저장', 'delete': '삭제',
                     'search': '검색', 'loading': '불러오는 중', 'more': '더 보기',
                     'close': '닫기', 'back': '뒤로', 'next': '다음',
                     'yes': '예', 'help': '도움말', 'settings': '설정'}}
    en = {'brand': {'name': 'ColTools'},                    # 허용 목록 = 미번역 아님
          'auth': {'login': 'Log in', 'logout': 'Log out',
                   'welcome': 'Welcome!',                   # ★플레이스홀더 불일치({name} 소실)
                   'error': 'An error occurred (code %s)', 'retry': '다시 시도'},   # ★미번역
          'cart': {'add': 'Add', 'remove': 'Remove',
                   'count': '{count} items ({cnt})',        # ★플레이스홀더 불일치(여분 {cnt})
                   'total': 'Total {total} KRW', 'empty': ''},                      # ★빈 값
          'order': {'submit': 'Place order', 'cancel': 'Cancel order', 'status': 'Order status',
                    'shipped': 'Shipped on {date}', 'refund': 'Request refund',
                    'legacy': 'Old string'},                # ★고아 키
          'common': {'ok': 'OK', 'no': 'Cancel', 'save': 'Save', 'delete': 'Delete',
                     'search': '검색', 'loading': '불러오는 중', 'more': '더 보기',  # ★미번역 3
                     'close': 'Close', 'back': 'Back',
                     # ★누락 3: next·yes·help
                     'settings': 'Settings', 'extra': 'orphan too'}}                # ★고아 키 2
    ja = json.loads(json.dumps(ko, ensure_ascii=False))     # 클린 대상(완전 번역 흉내:
    ja_flat = {'auth': {'login': 'ログイン', 'logout': 'ログアウト',
                        'welcome': '{name}さん、ようこそ', 'error': 'エラーが発生しました (コード %s)',
                        'retry': '再試行'}}
    ja.update(ja_flat)                                      # 일부만 일본어화해도 나머지는 동일값 =
    for sec in ('cart', 'order', 'common'):                 # 미번역 오탐 검증에 걸리므로 전부 변형
        ja[sec] = {k: v + '(JA)' for k, v in ko[sec].items()}
    for name, data in (('ko', ko), ('en', en), ('ja', ja)):
        json.dump(data, open(os.path.join(HERE, f'demo_{name}.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)


def main_demo():
    make_demo()
    ko_p = os.path.join(HERE, 'demo_ko.json')
    r_en = check(ko_p, os.path.join(HERE, 'demo_en.json'))
    r_ja = check(ko_p, os.path.join(HERE, 'demo_ja.json'))
    r_en2 = check(ko_p, os.path.join(HERE, 'demo_en.json'))

    # ① 누락 3 정확(common.next·yes·help)
    ok1 = (r_en['missing'] == ['common.help', 'common.next', 'common.yes'])
    # ② 고아 2 정확(order.legacy·common.extra)
    ok2 = (r_en['orphan'] == ['common.extra', 'order.legacy'])
    # ③ 미번역 4 정확(retry·search·loading·more — brand.name은 허용 목록으로 제외)
    ok3 = (r_en['untranslated'] == ['auth.retry', 'common.loading', 'common.more', 'common.search']
           and 'brand.name' not in r_en['untranslated'])
    # ④ ★플레이스홀더 불일치 2 정확(welcome {name} 소실 · count 여분 {cnt})
    ph_keys = sorted(k for k, _, _ in r_en['ph_mismatch'])
    ok4 = (ph_keys == ['auth.welcome', 'cart.count'])
    # ⑤ 빈 값 1(cart.empty)
    ok5 = (r_en['empty'] == ['cart.empty'])
    # ⑥ 클린 언어(ja) 오탐 0 + 완성도 100 + 재현성
    clean = not any((r_ja['missing'], r_ja['orphan'], r_ja['untranslated'],
                     r_ja['empty'], r_ja['ph_mismatch']))
    ok6 = (clean and r_ja['pct'] == 100.0 and r_en == r_en2)

    L = [f'# 번역 정합 검사 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = ko 30키 기준 · en에 결함 5종 심음(누락3·고아2·미번역4[허용1 제외]·플레이스홀더2·빈값1) · ja = 클린',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 누락 키 3 정확(키 이름까지) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 고아 키 2 정확 | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 미번역 4 정확 + 허용 목록(brand.name) 제외 | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ ★플레이스홀더 불일치 2({{name}} 소실·여분 {{cnt}}) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 빈 값 1 | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 클린 언어 오탐 0(완성도 100%) + 재현성 | {"PASS" if ok6 else "★FAIL"} |',
         '', '## 리포트 실물', '```', report_text('en', r_en), '', report_text('ja', r_ja), '```',
         '', '- ※ 포맷 = JSON(중첩 지원). properties·CSV·PO 등은 어댑터 1회 맞춤. 허용 목록·패턴 = 설정값.']
    rep = os.path.join(HERE, 'i18n_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) >= 3 and sys.argv[1].endswith('.json'):
        # 실사용: python i18n_check.py 기준.json 대상1.json [대상2.json …]
        bad = False
        for t in sys.argv[2:]:
            r = check(sys.argv[1], t)
            print(report_text(os.path.basename(t), r))
            bad = bad or any((r['missing'], r['ph_mismatch'], r['empty']))
        sys.exit(1 if bad else 0)
    ok = main_demo()
    sys.exit(0 if ok else 1)
