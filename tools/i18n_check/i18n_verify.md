# 번역 정합 검사 검증 리포트 (2026-09-04 13:00)
- 데모 = ko 30키 기준 · en에 결함 5종 심음(누락3·고아2·미번역4[허용1 제외]·플레이스홀더2·빈값1) · ja = 클린

| 검증 | 결과 |
|---|---|
| ① 누락 키 3 정확(키 이름까지) | PASS |
| ② 고아 키 2 정확 | PASS |
| ③ 미번역 4 정확 + 허용 목록(brand.name) 제외 | PASS |
| ④ ★플레이스홀더 불일치 2({name} 소실·여분 {cnt}) | PASS |
| ⑤ 빈 값 1 | PASS |
| ⑥ 클린 언어 오탐 0(완성도 100%) + 재현성 | PASS |

## 리포트 실물
```
[en] 완성도 65.5% (기준 29키)
  ★누락 키(런타임 노출 위험) 3: common.help, common.next, common.yes
  ★고아 키(죽은 번역) 2: common.extra, order.legacy
  ★미번역(기준과 동일) 4: auth.retry, common.loading, common.more, common.search
  ★빈 값 1: cart.empty
  ★플레이스홀더 불일치 [auth.welcome]: 기준 ['{name}'] vs 대상 []
  ★플레이스홀더 불일치 [cart.count]: 기준 ['{count}'] vs 대상 ['{cnt}', '{count}']

[ja] 완성도 100.0% (기준 29키)
  문제 없음
```

- ※ 포맷 = JSON(중첩 지원). properties·CSV·PO 등은 어댑터 1회 맞춤. 허용 목록·패턴 = 설정값.