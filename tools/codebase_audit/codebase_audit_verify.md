# 코드 인수 진단 킷 검증 리포트 (2026-09-07 13:20)
- 데모 = 결함을 위치를 알고 심은 합성 레거시 트리(PHP+Java+JS+SQL, vendor·테스트 포함) + 클린 트리(Flask)

| 검증 | 결과 |
|---|---|
| ① 인벤토리: PHP 파일 수 = 독립 계산(vendor 제외) · Java 2 · JS 2 · SQL 1 · 인코딩 utf-8/cp949 혼재 표시 | PASS |
| ② 의존성: 매니페스트 3종(composer·package·pom) 파싱 · 미고정 버전(^6.5·^4.17·${mysql.version}) 정확 검출 | PASS |
| ③ ★심은 위험 10종 전수 검출(파일:행 좌표 정확) · 초과 검출 0 · vendor 비밀은 제외 | PASS |
| ④ 클린 트리 오탐 0: 환경변수 비밀·파라미터 바인딩 SQL·테스트 존재 → 위험 0건 | PASS |
| ⑤ 라우트 PHP 3·Java 2·JS 2 · CREATE TABLE 3(users·orders·products) · 엔티티 1 · 테스트 1 | PASS |
| ⑥ 중복 파일: 사본이 원본(app/Util.php)을 지목 | PASS |
| ⑦ 리스크 점수: 오염 60 > 클린 0 · 항목 가중 합산 | PASS |
| ⑧ 재현성(재스캔 동일) · 엑셀 위험 시트 행수 = 리포트 건수 | PASS |

## 산출 실물
- demo_out/audit_report.md · audit_findings.xlsx (오염 트리) · demo_out_clean/ (클린 트리)
- 오염 트리 리스크 60/100 · 위험 10건 · 클린 트리 0/100 · 0건

- ※ 정적 휴리스틱 = "의심 표시"이며 취약점 판정·보안 감사 대체가 아님. 코드는 외부 전송 0.