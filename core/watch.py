#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watch.py — 변경 감시 공용 엔진 (2026-09)

패턴: **스냅샷 → diff → 중복제거 → 발송(재시도) → 실패는 보류(유실 0) → 스냅샷 전진.**
여러 감시 도구에서 같은 패턴이 반복 확인된 뒤 공용 모듈로 추출.

보장(노션봇 시나리오 8종으로 검증된 의미론 그대로):
  ① 첫 실행 = 스냅샷만, 알림 0 (알림 폭탄 방지)
  ② 같은 변경 재관측 = 중복 0 (sent 키 + pending 키)
  ③ 발송 실패 = 보류(pending) 후 다음 틱 재송 — 스냅샷은 전진해도 메시지 유실 0
  ④ 상태 파일 손상 = 안전 재초기화(재스냅샷, 폭탄 0)

사용:
  from watch import Watcher
  w = Watcher('state.json')
  sent, held = w.tick(cur_items,
                      differ=fn(prev_snapshot, cur_items) -> [(dedup_key, payload)],
                      sender=fn(payload) -> None(예외=실패),
                      snapshot=fn(cur_items) -> {id: 추적필드dict})
  w.save()
"""
import os, json, time


class Watcher:
    def __init__(self, state_path, retries=2, sent_cap=500):
        self.path = state_path
        self.retries = retries
        self.sent_cap = sent_cap
        self.state = self._load()

    # ── 상태 ──
    def _load(self):
        try:
            d = json.load(open(self.path, encoding='utf-8'))
            if not isinstance(d.get('snapshot'), dict) or not isinstance(d.get('sent'), list):
                raise ValueError('형식 불일치')
            d.setdefault('pending', [])
            d.setdefault('init', False)
            return d
        except FileNotFoundError:
            return {'snapshot': {}, 'sent': [], 'pending': [], 'init': False}
        except Exception:                       # 손상 → 재스냅샷(폭탄 방지)
            return {'snapshot': {}, 'sent': [], 'pending': [], 'init': False, '_recovered': True}

    def save(self):
        tmp = self.path + '.tmp'
        json.dump(self.state, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
        os.replace(tmp, self.path)

    # ── 엔진 ──
    def _try_send(self, sender, payload):
        for _ in range(self.retries + 1):
            try:
                sender(payload)
                return True
            except Exception:
                time.sleep(0.01)
        return False

    def tick(self, cur_items, differ, sender, snapshot):
        st = self.state
        sent = held = 0
        # 0) 보류분 재송 먼저 (유실 0)
        still = []
        for key, payload in st.get('pending', []):
            if self._try_send(sender, payload):
                st['sent'].append(key); sent += 1
            else:
                still.append([key, payload]); held += 1
        st['pending'] = still
        # 1) 신규 diff (첫 실행은 스냅샷만)
        if st.get('init'):
            pend_keys = {k for k, _ in st['pending']}
            for key, payload in differ(st['snapshot'], cur_items):
                if key in st['sent'] or key in pend_keys:
                    continue
                if self._try_send(sender, payload):
                    st['sent'].append(key); sent += 1
                else:
                    st['pending'].append([key, payload]); held += 1
        # 2) 스냅샷 전진
        st['snapshot'] = snapshot(cur_items)
        st['init'] = True
        st['sent'] = st['sent'][-self.sent_cap:]
        return sent, held
