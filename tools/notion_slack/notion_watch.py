#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notion_watch.py — 노션 변경 감지 → 슬랙/콘솔 알림 커넥터 (2026-09)

노션 데이터베이스를 주기 폴링해 **새 페이지·속성 변경·보관**을 감지하고 알림을 보낸다.
엔진 = `core/watch.py` Watcher(스냅샷·중복제거·보류 재송 공용 엔진).

정직성 설계(어댑터 분리):
  - 엔진(변경탐지·중복제거·재시도·상태관리) = FixtureSource로 **시나리오 전수 검증** ← 검증 동봉
  - NotionSource = 실제 Notion API(v1 databases/query, 2022-06-28) 스키마로 작성. 실계정 연결은 토큰만.
  - 알림 폭탄 방지: 첫 실행 스냅샷만. 재관측 중복 0. 발송 실패 유실 0(보류→재송).

사용:
  python notion_watch.py --verify              # 픽스처 시나리오 전수 검증 + 리포트
  NOTION_TOKEN=.. NOTION_DB=.. SLACK_WEBHOOK=.. python notion_watch.py --run   # 실운영 1회 폴링
"""
import os, sys, json, urllib.request, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from watch import Watcher

STATE = os.path.join(HERE, 'state.json')


# ── 소스 어댑터 ─────────────────────────────────────────────────────
class NotionSource:
    """실제 Notion API. fetch() -> [{id,title,status,edited,url,archived}]"""
    def __init__(self, token, dbid, status_prop='상태'):
        self.token, self.dbid, self.status_prop = token, dbid, status_prop

    def fetch(self):
        pages, cursor = [], None
        while True:
            body = {'page_size': 100}
            if cursor:
                body['start_cursor'] = cursor
            req = urllib.request.Request(
                f'https://api.notion.com/v1/databases/{self.dbid}/query',
                data=json.dumps(body).encode(), method='POST',
                headers={'Authorization': f'Bearer {self.token}',
                         'Notion-Version': '2022-06-28', 'Content-Type': 'application/json'})
            d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
            for p in d.get('results', []):
                props = p.get('properties', {})
                title = ''
                for v in props.values():
                    if v.get('type') == 'title':
                        title = ''.join(t.get('plain_text', '') for t in v.get('title', []))
                st = props.get(self.status_prop, {})
                status = (st.get('select') or {}).get('name', '') if st.get('type') == 'select' \
                    else (st.get('status') or {}).get('name', '') if st.get('type') == 'status' else ''
                pages.append({'id': p['id'], 'title': title or '(제목 없음)', 'status': status,
                              'edited': p.get('last_edited_time', ''), 'url': p.get('url', ''),
                              'archived': bool(p.get('archived'))})
            cursor = d.get('next_cursor')
            if not d.get('has_more'):
                return pages


class FixtureSource:
    """검증용: 호출마다 다음 사이클의 페이지 목록을 반환."""
    def __init__(self, cycles):
        self.cycles, self.i = cycles, 0

    def fetch(self):
        c = self.cycles[min(self.i, len(self.cycles) - 1)]
        self.i += 1
        return [dict(p) for p in c]


# ── 싱크 어댑터 ─────────────────────────────────────────────────────
class SlackSink:
    """Slack Incoming Webhook. 실패 시 예외(엔진이 재시도)."""
    def __init__(self, url):
        self.url = url

    def send(self, text):
        req = urllib.request.Request(self.url, data=json.dumps({'text': text}).encode(),
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)


class ConsoleSink:
    def __init__(self, fail_first=0):        # fail_first>0 = 재시도 검증용 인위 실패
        self.sent, self.fails_left = [], fail_first

    def send(self, text):
        if self.fails_left > 0:
            self.fails_left -= 1
            raise ConnectionError('sink 실패(검증용)')
        self.sent.append(text)


# ── diff 규칙 (Watcher의 differ) ─────────────────────────────────────
def diff_alerts(prev, pages):
    """이전 스냅샷 대비 변경 → (dedup_key, 알림문). 추적 = 신규/상태변경/보관."""
    out = []
    for p in pages:
        old = prev.get(p['id'])
        if old is None:
            out.append((f"new|{p['id']}|{p['edited']}", f"🆕 새 페이지: {p['title']} (상태 {p['status'] or '-'})\n{p['url']}"))
        else:
            if p['archived'] and not old.get('archived'):
                out.append((f"arch|{p['id']}|{p['edited']}", f"🗄 보관됨: {p['title']}"))
            elif p['status'] != old.get('status'):
                out.append((f"st|{p['id']}|{old.get('status')}→{p['status']}|{p['edited']}",
                            f"🔄 상태 변경: {p['title']} — {old.get('status') or '-'} → {p['status'] or '-'}\n{p['url']}"))
    return out


def snap(pages):
    return {p['id']: {'status': p['status'], 'archived': p['archived']} for p in pages}


def run_once(source, sink, w):
    return w.tick(source.fetch(), differ=diff_alerts, sender=sink.send, snapshot=snap)


# ── 검증 시나리오 (엔진 전수 — core Watcher 위에서 동일 의미론 재검증) ──
def P(i, title, status, edited='T1', archived=False):
    return {'id': f'pg{i}', 'title': title, 'status': status, 'edited': edited,
            'url': f'https://notion.so/pg{i}', 'archived': archived}


def run_verify():
    rows = []
    vstate = os.path.join(HERE, '_vstate.json')

    def case(name, expect_sent, expect_held, cycles, sink=None, note=''):
        if os.path.exists(vstate):
            os.remove(vstate)
        w = Watcher(vstate)
        src, sk = FixtureSource(cycles), (sink or ConsoleSink())
        tot_s = tot_h = 0
        for _ in cycles:
            s, h = run_once(src, sk, w)
            tot_s += s; tot_h += h
        ok = (tot_s == expect_sent and tot_h == expect_held)
        rows.append((name, expect_sent, tot_s, expect_held, tot_h, ok, note))

    base = [P(1, '주문서 검토', '진행중'), P(2, '견적 회신', '대기')]
    chg = [P(1, '주문서 검토', '완료', 'T2'), P(2, '견적 회신', '대기')]

    case('S1 첫 실행 스냅샷', 0, 0, [base])
    case('S2 신규+상태변경', 2, 0,
         [base, [P(1, '주문서 검토', '완료', 'T2'), P(2, '견적 회신', '대기'), P(3, '신규 문의', '접수', 'T2')]])
    case('S3 무변화 0건', 0, 0, [base, base])
    case('S4 재관측 중복 0', 1, 0, [base, chg, chg])
    many0 = [P(i, f'작업{i}', '대기') for i in range(10)]
    many1 = [P(i, f'작업{i}', '완료', 'T2') for i in range(10)]
    case('S5 대량 10건', 10, 0, [many0, many1])
    case('S6 보관 감지', 1, 0, [base, [P(1, '주문서 검토', '진행중', 'T2', archived=True), P(2, '견적 회신', '대기')]])
    case('S7 싱크 실패→보류→재송', 1, 1, [base, chg, chg], sink=ConsoleSink(fail_first=3), note='보류 후 다음 틱 발송')
    # S8 상태파일 손상 → 안전 재초기화(재스냅샷, 알림 폭탄 0)
    open(vstate, 'w').write('{broken')
    w8 = Watcher(vstate)
    recovered = w8.state.get('_recovered', False) and not w8.state['init']
    s8, _ = run_once(FixtureSource([base]), ConsoleSink(), w8)
    rows.append(('S8 상태손상 안전복구', 0, s8, 0, 0, recovered and s8 == 0, '재스냅샷·폭탄 0'))
    if os.path.exists(vstate):
        os.remove(vstate)

    ok_all = all(r[5] for r in rows)
    now = dt.datetime.now()
    L = [f'# 노션→슬랙 커넥터 검증 리포트 ({now:%Y-%m-%d %H:%M}) — core/watch.py Watcher 기반',
         '- 엔진(변경탐지·중복제거·재시도·상태관리) = `core/watch.py` 공용 Watcher + 픽스처 시나리오 전수.',
         '- 실계정 연결 = NotionSource/SlackSink 어댑터(실 API 스키마 작성, 토큰만 꽂으면 됨).',
         '', '| 시나리오 | 기대발송 | 실제 | 기대보류 | 실제 | 판정 | 비고 |', '|---|---|---|---|---|---|---|']
    for name, es, s, eh, h, ok, note in rows:
        L.append(f'| {name} | {es} | {s} | {eh} | {h} | {"PASS" if ok else "★FAIL"} | {note} |')
    L += ['', f'## 종합: **{sum(1 for r in rows if r[5])}/{len(rows)} PASS**' + ('' if ok_all else ' ★실패 있음'),
          '- 설계 보장: 첫 실행 알림 0(폭탄 방지) · 재관측 중복 0 · 싱크 실패 시 유실 0(보류→재송) · 상태 손상 시 재스냅샷.',
          '- ※ 정직 각주: 엔진은 전수 검증, 실계정 왕복은 고객 토큰/웹훅 연결 시 스모크 테스트로 확인.']
    report = os.path.join(HERE, 'verify_report.md')
    open(report, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok_all


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--run' in sys.argv:
        tok, db, hook = os.environ.get('NOTION_TOKEN'), os.environ.get('NOTION_DB'), os.environ.get('SLACK_WEBHOOK')
        if not (tok and db):
            raise SystemExit('NOTION_TOKEN, NOTION_DB 환경변수 필요 (SLACK_WEBHOOK 없으면 콘솔 출력)')
        w = Watcher(STATE)
        sink = SlackSink(hook) if hook else ConsoleSink()
        s, h = run_once(NotionSource(tok, db), sink, w)
        w.save()
        if isinstance(sink, ConsoleSink):
            print('\n'.join(sink.sent))
        print(f'폴링 완료: 발송 {s} · 보류 {h}')
    else:
        ok = run_verify()
        sys.exit(0 if ok else 1)
