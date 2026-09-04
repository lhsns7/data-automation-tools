# 로그 요약 검증 리포트 (2026-09-04 12:27)
- 데모 = 정답지 선작성: 1일차 2,476줄(에러 5그룹 476건+INFO 2,000) → 2일차 ★신규 1그룹·★급증(3→60) 심음

| 검증 | 결과 |
|---|---|
| ① 그룹핑 정확(5그룹 · 건수 [3,8,45,120,300] · 총수 보존) | PASS |
| ② 가변부 일반화(IP 120변형 = 1그룹 · 템플릿에 구체값 0) | PASS |
| ③ 노이즈 분리(INFO 2,000줄 → 집계 0) | PASS |
| ④ ★신규 검출(심은 null pointer만 NEW, 기존 5그룹 오탐 0) | PASS |
| ⑤ ★급증 감지(3→60건 20배만 SPIKE, 오탐 0) | PASS |
| ⑥ 첫/마지막 발생 시각 수기 + 재현성 | PASS |

## 2일차 요약 실물(도구 출력 그대로)
```
로그 2,545줄 → 에러 545건 → **그룹 6개** (한 화면)
★NEW 1 · ★SPIKE 1

  [  300] user {n} not found in session cache
         첫 2026-09-04 09:00:00 · 마지막 2026-09-04 20:59:53 · 예: user 10000 not found in session cache
  [  120] DB connection timeout host={n} retry={n}
         첫 2026-09-04 09:00:00 · 마지막 2026-09-04 20:59:53 · 예: DB connection timeout host=10.0.0.0 retry=0
  📈[   60] unexpected token in config line {n}
         첫 2026-09-04 09:00:00 · 마지막 2026-09-04 20:59:53 · 예: unexpected token in config line 1
  [   45] payment failed order={n} code={n}
         첫 2026-09-04 09:00:00 · 마지막 2026-09-04 20:35:05 · 예: payment failed order=7000 code=0
  🆕[   12] null pointer in cart module item={n}
         첫 2026-09-04 09:00:00 · 마지막 2026-09-04 20:11:17 · 예: null pointer in cart module item=0
  [    8] disk usage {n}% on /dev/sda{n}
         첫 2026-09-04 09:00:00 · 마지막 2026-09-04 16:07:49 · 예: disk usage 80% on /dev/sda0
```

- ※ 로그 포맷 정규식·레벨 필터·핑거프린트 규칙·급증 임계 = 설정값(고객 로그에 1회 맞춤).