# 코드 인수 진단 리포트 — legacy_shop (2026-09-07 13:20)
- 파일 14개 · 코드 56줄 · 인수 리스크 점수 **60/100** (정적 휴리스틱 — 의심 표시이지 취약점 판정 아님)

## ① 인벤토리
| 언어 | 파일 | 줄 |
|---|---|---|
| PHP | 6 | 31 |
| Java | 2 | 14 |
| JavaScript | 2 | 7 |
| SQL | 1 | 4 |

디렉터리 상위(줄 기준): app 25 · api 14 · web 7 · public 6 · db 4

대형 파일: app/OrderController.php(10줄) · api/src/main/java/com/shop/ProductApi.java(10줄) · public/index.php(6줄) · web/server.js(5줄) · app/config.php(5줄) · db/schema.sql(4줄) · app/Util_copy.php(4줄) · app/Util.php(4줄)

인코딩: utf-8 9 · cp949 2 — **혼재**

## ② 의존성 (6개, 미고정 3개)
| 매니페스트 | 라이브러리 | 버전 | 고정 |
|---|---|---|---|
| composer.json | monolog/monolog | 2.0.0 | ✔ |
| composer.json | guzzlehttp/guzzle | ^6.5 | ✘ 범위/미지정 |
| api/pom.xml | spring-boot-starter-web | 2.3.4.RELEASE | ✔ |
| api/pom.xml | mysql-connector-java | ${mysql.version} | ✘ 범위/미지정 |
| web/package.json | express | ^4.17.1 | ✘ 범위/미지정 |
| web/package.json | lodash | 4.17.21 | ✔ |

## ③ 위험 신호 (10건) — SQL 문자열 결합(인젝션 의심) 3 · 인코딩 3 · 하드코딩 비밀 2 · eval/exec/shell 호출 1 · 중복 파일 1
| 종류 | 파일 | 행 | 내용 |
|---|---|---|---|
| SQL 문자열 결합(인젝션 의심) | api/src/main/java/com/shop/ProductApi.java | 6 | String q = "UPDATE products SET name='" + p.getName() + "'"; |
| 인코딩 | app/Bom.php | 1 | UTF-8 BOM 포함 — PHP 등에서 출력 오염 원인 |
| SQL 문자열 결합(인젝션 의심) | app/OrderController.php | 4 | $sql = "SELECT * FROM orders WHERE id = " . $_GET['id']; |
| SQL 문자열 결합(인젝션 의심) | app/OrderController.php | 5 | $q = 'DELETE FROM cart WHERE user=' . $user; |
| eval/exec/shell 호출 | app/OrderController.php | 7 | eval($_POST['code']); |
| 인코딩 | app/Util.php | 1 | CP949/EUC-KR 인코딩 — UTF-8 파일과 혼재 |
| 인코딩 | app/Util_copy.php | 1 | CP949/EUC-KR 인코딩 — UTF-8 파일과 혼재 |
| 하드코딩 비밀 | app/config.php | 3 | $db_password = 'demo-password-1234'; |
| 하드코딩 비밀 | app/config.php | 4 | $api_key = "DEMO-API-KEY-000000"; |
| 중복 파일 | app/Util_copy.php | 1 | app/Util.php 와 내용 동일 |

## ④ 엔트리포인트·라우트(추정)
Java 2개 · PHP 3개 · JavaScript 2개

## ⑤ DB(추정): CREATE TABLE 3개 (users, orders, products) · ORM 엔티티 1개
## ⑥ 테스트 파일: 1개
## ⑦ TODO/FIXME: 3건

## 1주차 착수 순서(제안)
1. 위험 신호 중 하드코딩 비밀 → 환경변수/비밀 저장소로 즉시 이관
2. SQL 결합 의심 지점 → 파라미터 바인딩 여부 실확인(오탐 소거)
3. 인코딩 혼재 → UTF-8 통일 계획(깨짐 재현 케이스 확보)
4. 테스트 0이면 핵심 화면·API에 특성화 테스트(현재 동작 고정) 후 변경 착수
5. 미고정 의존성 → 잠금 파일·버전 고정

※ 이 리포트는 실행·컴파일 없이 소스만 정적으로 훑은 결과입니다. 코드는 외부로 전송되지 않습니다.