#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codebase_audit.py — 레거시 코드 인수 진단 킷: 소스 트리 → 인벤토리·의존성·위험 신호·엔트리포인트·DB·테스트 리포트 (2026-09-07)

"기존 시스템 유지보수·고도화" 의뢰에서 첫 주에 해야 하는 일 = 남의 코드를 지도로 만드는 것. 이 도구는 그 지도를 시간 단위로 뽑는다.
언어 무관(PHP·Java·JS/TS·Python·C# 등 확장자 기준), 표준 라이브러리만, 오프라인(코드가 밖으로 나가지 않음).

리포트 항목:
  ①인벤토리: 언어별 파일 수·LOC, 디렉터리 상위 지도, 대형 파일 top N (vendor/node_modules/build 등 생성물 제외)
  ②의존성: composer.json·package.json·requirements.txt·pom.xml·build.gradle 파싱 → 라이브러리 목록·버전 고정 여부
  ③★위험 신호(좌표 = 파일:행): 하드코딩 비밀(키·비밀번호·토큰) · SQL 문자열 결합(인젝션 의심) · eval/exec · 인코딩 혼재(UTF-8 vs CP949/EUC-KR) · BOM/CRLF 혼재 · 중복 파일(내용 해시) · TODO/FIXME 밀도
  ④엔트리포인트·라우트 추정: PHP(index.php·라우팅) · Spring(@RequestMapping류) · Express(app.get 등) · Flask/FastAPI 데코레이터 → 화면/API 수 감각
  ⑤DB 추정: .sql의 CREATE TABLE·테이블명 · ORM 엔티티(@Entity·models.Model) 수
  ⑥테스트: 테스트 디렉터리·파일 수 (0이면 인수 리스크 상향)
  ⑦인수 리스크 점수(0~100, 항목별 가중)와 1주차 착수 순서 제안
산출: audit_report.md + audit_findings.xlsx(좌표 시트) — 클라에게 "1주차 코드 분석 산출"로 바로 제출 가능.

검증(main_demo): 결함을 위치를 알고 심은 합성 레거시 트리(PHP+Java+JS+SQL, vendor 포함) —
  ①인벤토리 = 독립 계산(언어별 파일·LOC·vendor 제외) ②의존성 파싱(3 매니페스트·미고정 버전 검출)
  ③★심은 위험 8종 전수 검출(파일:행 좌표 정확, 초과 검출 0) ④클린 트리 오탐 0 ⑤라우트·DB·테스트 카운트 = 심은 수
  ⑥중복 파일 원본 지목 ⑦리스크 점수 단조성(클린 < 오염) ⑧재현성·엑셀 산출 정합
실행: python codebase_audit.py <소스폴더> [--out 리포트폴더]
※ 정적 휴리스틱(실행·컴파일 없음)이라 '의심'을 표시할 뿐 취약점 판정이 아니며, 보안 감사 대체가 아님(정직선).
"""
import os, sys, re, json, hashlib, datetime as dt, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook

LANG = {'.php': 'PHP', '.java': 'Java', '.kt': 'Kotlin', '.js': 'JavaScript', '.ts': 'TypeScript', '.jsx': 'JavaScript', '.tsx': 'TypeScript',
        '.py': 'Python', '.cs': 'C#', '.go': 'Go', '.rb': 'Ruby', '.sql': 'SQL', '.html': 'HTML', '.css': 'CSS', '.vue': 'Vue', '.jsp': 'JSP'}
SKIP_DIRS = {'vendor', 'node_modules', '.git', 'build', 'dist', 'target', '__pycache__', '.idea', '.vscode', 'bin', 'obj', 'out', '.next', 'coverage'}
TEST_HINT = re.compile(r'(^|/)(tests?|__tests__|spec)(/|$)|(_test|Test|\.test|\.spec)\.\w+$', re.I)

# ── 위험 신호 패턴 (파일:행 좌표) ──
SECRET = re.compile(r'(?i)(password|passwd|pwd|secret|api[_-]?key|token|access[_-]?key)\s*[:=]\s*["\'][^"\']{6,}["\']')
SECRET_SKIP = re.compile(r'(?i)(getenv|environ|process\.env|\$_ENV|\{\{|\$\{|<%|placeholder|example|changeme|your[_-])')
SQL_CONCAT = re.compile(r'(?i)(select|insert|update|delete)\s.{0,80}(\+\s*\$?\w|\.\s*\$\w|\$\w+\s*\.\s*["\']|"\s*\+\s*\w|\'\s*\.\s*\$)')
SQL_FSTR = re.compile(r'(?i)f["\'].{0,20}(select|insert|update|delete)\s')
EVAL = re.compile(r'(?<![\w.])(eval|exec|system|shell_exec|passthru|popen)\s*\(')
ROUTES = [('PHP', re.compile(r'Route::(get|post|put|delete|any)\s*\(|\$app->(get|post|put|delete)\s*\(|add_action\s*\(\s*[\'"]wp_ajax')),
          ('Java', re.compile(r'@(Request|Get|Post|Put|Delete|Patch)Mapping')),
          ('JavaScript', re.compile(r'\b(app|router)\.(get|post|put|delete|patch|use)\s*\(\s*[\'"`]/')),
          ('TypeScript', re.compile(r'\b(app|router)\.(get|post|put|delete|patch)\s*\(\s*[\'"`]/|@(Get|Post|Put|Delete|Patch)\s*\(')),
          ('Python', re.compile(r'@\w+\.(route|get|post|put|delete|patch)\s*\('))]
ENTITY = re.compile(r'@Entity\b|class\s+\w+\s*\(\s*(models\.Model|db\.Model)\s*\)|extends\s+Model\b')
CREATE_TABLE = re.compile(r'(?i)create\s+table\s+(?:if\s+not\s+exists\s+)?[`"\[]?([\w.]+)')
TODO = re.compile(r'(?i)\b(todo|fixme|hack|xxx)\b')


def rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, '/')


def read_text(path):
    """(text, encoding, bom, crlf) — UTF-8 실패 시 CP949 시도(한국 레거시 인코딩 혼재 검출)."""
    raw = open(path, 'rb').read()
    bom = raw.startswith(b'\xef\xbb\xbf')
    crlf = b'\r\n' in raw
    try:
        return raw.decode('utf-8'), 'utf-8', bom, crlf
    except UnicodeDecodeError:
        try:
            return raw.decode('cp949'), 'cp949', bom, crlf
        except UnicodeDecodeError:
            return raw.decode('latin-1', 'replace'), 'unknown', bom, crlf


def scan(root, top_n=20):
    root = os.path.abspath(root)
    files = []
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith('.')]
        for f in fs:
            ext = os.path.splitext(f)[1].lower()
            if ext in LANG or f in ('composer.json', 'package.json', 'requirements.txt', 'pom.xml', 'build.gradle', 'Gemfile', 'go.mod'):
                files.append(os.path.join(d, f))
    inv = collections.Counter(); loc = collections.Counter(); sizes = []; hashes = collections.defaultdict(list)
    findings = []; routes = collections.Counter(); entities = 0; tables = []; tests = 0; enc = collections.Counter(); todo = 0; dirmap = collections.Counter()
    for p in sorted(files):
        r = rel(p, root); ext = os.path.splitext(p)[1].lower(); lang = LANG.get(ext)
        text, e, bom, crlf = read_text(p); lines = text.split('\n')
        if lang:
            inv[lang] += 1; loc[lang] += len(lines); sizes.append((len(lines), r)); enc[e] += 1
            dirmap[r.split('/')[0] if '/' in r else '(root)'] += len(lines)
            hashes[hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()].append(r)
            if TEST_HINT.search(r): tests += 1
            if bom: findings.append(('인코딩', r, 1, 'UTF-8 BOM 포함 — PHP 등에서 출력 오염 원인'))
            if e == 'cp949': findings.append(('인코딩', r, 1, 'CP949/EUC-KR 인코딩 — UTF-8 파일과 혼재'))
            for i, l in enumerate(lines, 1):
                if SECRET.search(l) and not SECRET_SKIP.search(l): findings.append(('하드코딩 비밀', r, i, l.strip()[:90]))
                if lang != 'SQL' and (SQL_CONCAT.search(l) or SQL_FSTR.search(l)): findings.append(('SQL 문자열 결합(인젝션 의심)', r, i, l.strip()[:90]))
                if lang in ('PHP', 'Python', 'JavaScript', 'TypeScript', 'Ruby') and EVAL.search(l) and not l.strip().startswith(('//', '#', '*')): findings.append(('eval/exec/shell 호출', r, i, l.strip()[:90]))
                if TODO.search(l): todo += 1
            for lg, pat in ROUTES:
                if lang == lg: routes[lg] += len(pat.findall(text))
            entities += len(ENTITY.findall(text))
            if lang == 'SQL': tables += CREATE_TABLE.findall(text)
    dups = [(v[0], v[1:]) for v in hashes.values() if len(v) > 1]
    for orig, copies in dups:
        for c in copies: findings.append(('중복 파일', c, 1, f'{orig} 와 내용 동일'))
    deps = parse_deps(root)
    total_loc = sum(loc.values()) or 1
    score = risk_score(findings, tests, deps, todo, total_loc, enc)
    return dict(root=root, inv=dict(inv), loc=dict(loc), sizes=sorted(sizes, reverse=True)[:top_n], dirmap=dirmap.most_common(12), enc=dict(enc),
                findings=findings, routes=dict(routes), entities=entities, tables=tables, tests=tests, todo=todo, deps=deps, dups=dups, score=score, total_loc=total_loc, nfiles=len(files))


def parse_deps(root):
    """매니페스트 → [(파일, 라이브러리, 버전, 고정여부)]. 고정 = 정확 버전(^~>= 등 범위 아님)."""
    out = []
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in fs:
            p = os.path.join(d, f); r = rel(p, root)
            try:
                if f in ('composer.json', 'package.json'):
                    j = json.load(open(p, encoding='utf-8'))
                    for sec in ('require', 'require-dev', 'dependencies', 'devDependencies'):
                        for k, v in (j.get(sec) or {}).items():
                            if k == 'php': continue
                            out.append((r, k, str(v), bool(re.fullmatch(r'v?\d+(\.\d+)*', str(v)))))
                elif f == 'requirements.txt':
                    for l in open(p, encoding='utf-8'):
                        l = l.strip()
                        if l and not l.startswith('#'):
                            m = re.match(r'([\w\-\.\[\]]+)\s*(==|>=|<=|~=|>|<)?\s*([\w\.\*]+)?', l)
                            if m: out.append((r, m.group(1), (m.group(2) or '') + (m.group(3) or ''), m.group(2) == '=='))
                elif f == 'pom.xml':
                    t = open(p, encoding='utf-8', errors='replace').read()
                    for a, v in re.findall(r'<artifactId>([^<]+)</artifactId>\s*<version>([^<]+)</version>', t):
                        out.append((r, a, v, not (re.search(r'[\$\[\(,]', v) or v.strip() in ('LATEST', 'RELEASE'))))   # 2.3.4.RELEASE는 고정, 메타버전 LATEST/RELEASE·범위·속성치환은 미고정
                elif f == 'build.gradle':
                    t = open(p, encoding='utf-8', errors='replace').read()
                    for g, a, v in re.findall(r'[\'"]([\w\.\-]+):([\w\.\-]+):([\w\.\-\+]+)[\'"]', t):
                        out.append((r, f'{g}:{a}', v, '+' not in v))
            except Exception as e:
                out.append((r, f'(파싱 실패: {type(e).__name__})', '', False))
    return out


def risk_score(findings, tests, deps, todo, total_loc, enc):
    kinds = collections.Counter(k for k, *_ in findings)
    s = 0
    s += min(30, kinds.get('하드코딩 비밀', 0) * 10)
    s += min(25, kinds.get('SQL 문자열 결합(인젝션 의심)', 0) * 5)
    s += min(15, kinds.get('eval/exec/shell 호출', 0) * 5)
    s += 10 if kinds.get('인코딩', 0) else 0
    s += min(10, kinds.get('중복 파일', 0) * 2)
    s += 15 if tests == 0 else 0
    s += 5 if any(not fixed for *_, fixed in deps) else 0
    s += min(5, int(todo / max(total_loc / 1000, 1)))            # TODO 밀도(1,000줄당)
    return min(100, s)


def report_md(a):
    L = [f'# 코드 인수 진단 리포트 — {os.path.basename(a["root"])} ({dt.datetime.now():%Y-%m-%d %H:%M})',
         f'- 파일 {a["nfiles"]}개 · 코드 {a["total_loc"]:,}줄 · 인수 리스크 점수 **{a["score"]}/100** (정적 휴리스틱 — 의심 표시이지 취약점 판정 아님)', '',
         '## ① 인벤토리', '| 언어 | 파일 | 줄 |', '|---|---|---|'] + [f'| {k} | {a["inv"][k]} | {a["loc"][k]:,} |' for k in sorted(a['inv'], key=lambda k: -a['loc'][k])]
    L += ['', '디렉터리 상위(줄 기준): ' + ' · '.join(f'{d} {n:,}' for d, n in a['dirmap']), '', '대형 파일: ' + ' · '.join(f'{r}({n}줄)' for n, r in a['sizes'][:8]),
          f'\n인코딩: ' + ' · '.join(f'{k} {v}' for k, v in a['enc'].items()) + (' — **혼재**' if len([k for k in a['enc'] if k != 'unknown']) > 1 else '')]
    L += ['', f'## ② 의존성 ({len(a["deps"])}개, 미고정 {sum(1 for *_, f in a["deps"] if not f)}개)', '| 매니페스트 | 라이브러리 | 버전 | 고정 |', '|---|---|---|---|'] + [f'| {r} | {k} | {v} | {"✔" if f else "✘ 범위/미지정"} |' for r, k, v, f in a['deps'][:40]]
    kinds = collections.Counter(k for k, *_ in a['findings'])
    L += ['', f'## ③ 위험 신호 ({len(a["findings"])}건) — ' + ' · '.join(f'{k} {n}' for k, n in kinds.most_common()), '| 종류 | 파일 | 행 | 내용 |', '|---|---|---|---|'] + [f'| {k} | {r} | {i} | {esc_md(t)} |' for k, r, i, t in a['findings'][:80]]
    L += ['', '## ④ 엔트리포인트·라우트(추정)', ' · '.join(f'{k} {v}개' for k, v in a['routes'].items()) or '탐지 0 (라우팅 방식 수동 확인)',
          '', f'## ⑤ DB(추정): CREATE TABLE {len(a["tables"])}개 ' + (f'({", ".join(a["tables"][:12])})' if a['tables'] else '') + f' · ORM 엔티티 {a["entities"]}개',
          f'## ⑥ 테스트 파일: {a["tests"]}개' + (' — **없음: 변경 시 회귀 안전망 부재 → 1주차에 특성화 테스트부터**' if a['tests'] == 0 else ''),
          f'## ⑦ TODO/FIXME: {a["todo"]}건',
          '', '## 1주차 착수 순서(제안)', '1. 위험 신호 중 하드코딩 비밀 → 환경변수/비밀 저장소로 즉시 이관', '2. SQL 결합 의심 지점 → 파라미터 바인딩 여부 실확인(오탐 소거)',
          '3. 인코딩 혼재 → UTF-8 통일 계획(깨짐 재현 케이스 확보)', '4. 테스트 0이면 핵심 화면·API에 특성화 테스트(현재 동작 고정) 후 변경 착수', '5. 미고정 의존성 → 잠금 파일·버전 고정',
          '', '※ 이 리포트는 실행·컴파일 없이 소스만 정적으로 훑은 결과입니다. 코드는 외부로 전송되지 않습니다.']
    return '\n'.join(L)


def esc_md(t):
    return t.replace('|', '\\|')


def report_xlsx(a, path):
    write_workbook(path, {
        '위험 신호': (['종류', '파일', '행', '내용'], [list(x) for x in a['findings']]),
        '의존성': (['매니페스트', '라이브러리', '버전', '고정'], [[r, k, v, '고정' if f else '범위/미지정'] for r, k, v, f in a['deps']]),
        '대형 파일': (['줄', '파일'], [list(x) for x in a['sizes']]),
    }, summary={'대상': a['root'], '리스크 점수': f'{a["score"]}/100', '파일': a['nfiles'], '코드 줄': a['total_loc'], '테스트 파일': a['tests'], '라우트(추정)': sum(a['routes'].values())})


def run(root, out_dir=None):
    a = scan(root); out_dir = out_dir or HERE
    os.makedirs(out_dir, exist_ok=True)
    open(os.path.join(out_dir, 'audit_report.md'), 'w', encoding='utf-8').write(report_md(a))
    report_xlsx(a, os.path.join(out_dir, 'audit_findings.xlsx'))
    return a


# ── 데모: 결함을 심은 합성 레거시 트리 ──────────────────────────────
def make_demo(base):
    import shutil
    shutil.rmtree(base, ignore_errors=True)
    W = lambda p, s, enc='utf-8': (os.makedirs(os.path.dirname(os.path.join(base, p)), exist_ok=True), open(os.path.join(base, p), 'wb').write(s.encode(enc)))
    dirty = os.path.join(base, 'legacy_shop')
    W('legacy_shop/public/index.php', "<?php\nrequire '../app/bootstrap.php';\n$app->get('/', 'HomeController@index');\n$app->get('/products', 'ProductController@list');\n$app->post('/order', 'OrderController@create');\n")
    W('legacy_shop/app/config.php', "<?php\n$db_host = getenv('DB_HOST');\n$db_password = 'demo-password-1234';\n$api_key = \"DEMO-API-KEY-000000\";\n")
    W('legacy_shop/app/OrderController.php', "<?php\nclass OrderController {\n  function create($id) {\n    $sql = \"SELECT * FROM orders WHERE id = \" . $_GET['id'];\n    $q = 'DELETE FROM cart WHERE user=' . $user;\n    // TODO: 트랜잭션\n    eval($_POST['code']);\n  }\n}\n")
    W('legacy_shop/app/Util.php', "<?php\nfunction fmt($n) { return number_format($n); }\n// FIXME: 반올림\n", 'cp949')
    W('legacy_shop/app/Util_copy.php', "<?php\nfunction fmt($n) { return number_format($n); }\n// FIXME: 반올림\n", 'cp949')
    W('legacy_shop/api/src/main/java/com/shop/ProductApi.java', "package com.shop;\n@RestController\npublic class ProductApi {\n  @GetMapping(\"/api/products\") public List<Product> list() { return repo.findAll(); }\n  @PostMapping(\"/api/products\") public Product add(@RequestBody Product p) {\n    String q = \"UPDATE products SET name='\" + p.getName() + \"'\";\n    return repo.save(p);\n  }\n}\n")
    W('legacy_shop/api/src/main/java/com/shop/Product.java', "package com.shop;\n@Entity\npublic class Product { Long id; String name; }\n")
    W('legacy_shop/api/pom.xml', "<project><dependencies><dependency><artifactId>spring-boot-starter-web</artifactId><version>2.3.4.RELEASE</version></dependency><dependency><artifactId>mysql-connector-java</artifactId><version>${mysql.version}</version></dependency></dependencies></project>")
    W('legacy_shop/web/server.js', "const app = require('express')();\napp.get('/health', (req, res) => res.send('ok'));\napp.post('/webhook', handler);\nconst token = process.env.TOKEN;\n")
    W('legacy_shop/web/package.json', json.dumps({'dependencies': {'express': '^4.17.1', 'lodash': '4.17.21'}}))
    W('legacy_shop/composer.json', json.dumps({'require': {'php': '>=7.2', 'monolog/monolog': '2.0.0', 'guzzlehttp/guzzle': '^6.5'}}))
    W('legacy_shop/db/schema.sql', "CREATE TABLE users (id INT);\nCREATE TABLE IF NOT EXISTS orders (id INT);\ncreate table `products` (id INT);\n")
    W('legacy_shop/vendor/monolog/Logger.php', "<?php $password = 'vendor-secret-should-be-skipped';\n")
    W('legacy_shop/web/app.test.js', "test('health', () => {});\n")
    W('legacy_shop/app/Bom.php', "﻿<?php echo 'bom';\n")
    clean = os.path.join(base, 'clean_app')
    W('clean_app/app.py', "import os\nfrom flask import Flask\napp = Flask(__name__)\nDB_PASSWORD = os.environ.get('DB_PASSWORD')\n@app.route('/')\ndef home():\n    return 'ok'\n@app.route('/items')\ndef items():\n    cur.execute('SELECT * FROM items WHERE id = %s', (item_id,))\n    return 'ok'\n")
    W('clean_app/requirements.txt', "flask==2.3.2\nrequests==2.31.0\n")
    W('clean_app/tests/test_app.py', "def test_home():\n    assert True\n")
    return dirty, clean


PLANTED = [('하드코딩 비밀', 'app/config.php', 3), ('하드코딩 비밀', 'app/config.php', 4), ('SQL 문자열 결합(인젝션 의심)', 'app/OrderController.php', 4),
           ('SQL 문자열 결합(인젝션 의심)', 'app/OrderController.php', 5), ('eval/exec/shell 호출', 'app/OrderController.php', 7),
           ('SQL 문자열 결합(인젝션 의심)', 'api/src/main/java/com/shop/ProductApi.java', 6), ('인코딩', 'app/Util.php', 1), ('인코딩', 'app/Util_copy.php', 1),
           ('인코딩', 'app/Bom.php', 1), ('중복 파일', 'app/Util_copy.php', 1)]


def main_demo():
    base = os.path.join(HERE, 'demo_tree'); dirty, clean = make_demo(base)
    a = run(dirty, os.path.join(HERE, 'demo_out')); b = run(clean, os.path.join(HERE, 'demo_out_clean'))
    R = []
    # ① 인벤토리 = 독립 계산 (vendor 제외)
    exp_php = sum(1 for d, _, fs in os.walk(dirty) if 'vendor' not in d for f in fs if f.endswith('.php'))
    R.append(('① 인벤토리: PHP 파일 수 = 독립 계산(vendor 제외) · Java 2 · JS 2 · SQL 1 · 인코딩 utf-8/cp949 혼재 표시', a['inv'].get('PHP') == exp_php and a['inv'].get('Java') == 2 and a['inv'].get('JavaScript') == 2 and a['inv'].get('SQL') == 1 and set(a['enc']) >= {'utf-8', 'cp949'}))
    # ② 의존성
    names = {k for _, k, _, _ in a['deps']}; unfixed = {k for _, k, _, f in a['deps'] if not f}
    R.append(('② 의존성: 매니페스트 3종(composer·package·pom) 파싱 · 미고정 버전(^6.5·^4.17·${mysql.version}) 정확 검출', {'monolog/monolog', 'guzzlehttp/guzzle', 'express', 'lodash', 'spring-boot-starter-web', 'mysql-connector-java'} <= names and unfixed == {'guzzlehttp/guzzle', 'express', 'mysql-connector-java'}))
    # ③ 심은 위험 전수 · 좌표 · 초과 0
    got = {(k, r, i) for k, r, i, _ in a['findings']}
    R.append((f'③ ★심은 위험 {len(PLANTED)}종 전수 검출(파일:행 좌표 정확) · 초과 검출 0 · vendor 비밀은 제외', got == set(PLANTED)))
    # ④ 클린 트리 오탐 0 (환경변수 비밀·파라미터 바인딩 SQL은 정상)
    R.append(('④ 클린 트리 오탐 0: 환경변수 비밀·파라미터 바인딩 SQL·테스트 존재 → 위험 0건', len(b['findings']) == 0 and b['tests'] == 1))
    # ⑤ 라우트·DB·테스트 카운트
    R.append(('⑤ 라우트 PHP 3·Java 2·JS 2 · CREATE TABLE 3(users·orders·products) · 엔티티 1 · 테스트 1', a['routes'] == {'PHP': 3, 'Java': 2, 'JavaScript': 2} and sorted(a['tables']) == ['orders', 'products', 'users'] and a['entities'] == 1 and a['tests'] == 1))
    # ⑥ 중복 원본 지목
    dup = [t for k, r, i, t in a['findings'] if k == '중복 파일']
    R.append(('⑥ 중복 파일: 사본이 원본(app/Util.php)을 지목', dup == ['app/Util.php 와 내용 동일']))
    # ⑦ 리스크 점수 단조성
    R.append((f'⑦ 리스크 점수: 오염 {a["score"]} > 클린 {b["score"]} · 항목 가중 합산', a['score'] > b['score'] and b['score'] == 0))
    # ⑧ 재현성·엑셀
    import openpyxl
    a2 = scan(dirty); ws = openpyxl.load_workbook(os.path.join(HERE, 'demo_out', 'audit_findings.xlsx'))['위험 신호']
    R.append(('⑧ 재현성(재스캔 동일) · 엑셀 위험 시트 행수 = 리포트 건수', a2['findings'] == a['findings'] and ws.max_row - 1 == len(a['findings'])))
    L = [f'# 코드 인수 진단 킷 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})', '- 데모 = 결함을 위치를 알고 심은 합성 레거시 트리(PHP+Java+JS+SQL, vendor·테스트 포함) + 클린 트리(Flask)', '', '| 검증 | 결과 |', '|---|---|'] + [f'| {k} | {"PASS" if v else "★FAIL"} |' for k, v in R] + [
         '', '## 산출 실물', '- demo_out/audit_report.md · audit_findings.xlsx (오염 트리) · demo_out_clean/ (클린 트리)', f'- 오염 트리 리스크 {a["score"]}/100 · 위험 {len(a["findings"])}건 · 클린 트리 {b["score"]}/100 · 0건',
         '', '- ※ 정적 휴리스틱 = "의심 표시"이며 취약점 판정·보안 감사 대체가 아님. 코드는 외부 전송 0.']
    open(os.path.join(HERE, 'codebase_audit_verify.md'), 'w', encoding='utf-8').write('\n'.join(L)); print('\n'.join(L))
    return all(v for _, v in R)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
        out = sys.argv[sys.argv.index('--out') + 1] if '--out' in sys.argv else os.path.join(os.getcwd(), 'audit_out')
        a = run(sys.argv[1], out); print(report_md(a)); print(f'\n저장: {out}')
    else:
        sys.exit(0 if main_demo() else 1)
