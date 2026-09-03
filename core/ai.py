#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai.py — LLM 증강 레이어 (2026-08)

지저분한 데이터를 **구조화·분류·추출**한다. Task(출력 스키마) + 교체 가능한 백엔드 구조.

  from ai import Task, process, RulesBackend, MockBackend, AnthropicBackend

  task = Task('감정분류', system='...', schema={...})
  # 무료: 규칙/모의
  res, rep = process(rows, task, RulesBackend(my_rule_fn))
  # 유료 추정만(호출 0): 얼마 들지 먼저 본다
  res, rep = process(rows, task, AnthropicBackend(), dry_run=True)
  # 유료 실호출: 3중 게이트 전부 통과해야만
  res, rep = process(rows, task, AnthropicBackend(), confirm_spend=True, max_calls=50)

★비용 안전장치 (무료 골격의 핵심):
  ① 캐시 — 같은 입력·태스크는 재호출 안 함(재실행·중복 재과금 0).
  ② dry_run — 호출 없이 토큰·요금 추정만.
  ③ 3중 게이트 — 유료 백엔드는 (자격 有) ∧ (confirm_spend=True) ∧ (max_calls 지정) 전부라야 호출.
  실수로 돈이 나가는 경로가 없다.
"""
import os, sys, json, hashlib, time

# 대략 단가 ($/MTok, input/output) — 추정 전용. 정확값은 anthropic.com/pricing에서 갱신할 것.
PRICING = {
    'claude-haiku-4-5-20251001': (1.0, 5.0),
    'claude-sonnet-5': (3.0, 15.0),
    'claude-opus-5': (15.0, 75.0),
}
DEFAULT_MODEL = 'claude-haiku-4-5-20251001'   # 대량 분류/추출 = 비용 우선 = Haiku 기본


def est_tokens(s):
    """한/영 혼합 보수적 토큰 추정(정확 아님, 상한 감각용). 한글은 글자당≈토큰이라 3으로 나눔."""
    return max(1, int(len(str(s)) / 3))


class Task:
    """AI 작업 1종의 명세. schema = 출력 JSON 스키마(구조 고정)."""
    def __init__(self, name, system, schema, max_tokens=512, model=DEFAULT_MODEL, user_tmpl='{text}'):
        self.name = name
        self.system = system
        self.schema = schema
        self.max_tokens = max_tokens
        self.model = model
        self.user_tmpl = user_tmpl

    def empty(self):
        """스키마 모양의 빈 결과(실패·모의·거부 시 반환)."""
        out = {}
        for k, v in (self.schema.get('properties') or {}).items():
            out[k] = 0 if v.get('type') in ('integer', 'number') else ''
        return out

    def sig(self):
        return hashlib.sha256((self.name + '|' + self.system + '|' +
                               json.dumps(self.schema, sort_keys=True)).encode()).hexdigest()[:16]


class Cache:
    """입력×태스크 → 결과 JSON 파일 캐시. 재실행·중복은 호출 없이 즉시 반환."""
    def __init__(self, path):
        self.path = path
        self.d = {}
        if path and os.path.isfile(path):
            try:
                self.d = json.load(open(path, encoding='utf-8'))
            except Exception:
                self.d = {}

    def key(self, text, task):
        return hashlib.sha256((task.sig() + '|' + str(text)).encode()).hexdigest()[:24]

    def get(self, text, task):
        return self.d.get(self.key(text, task))

    def put(self, text, task, val):
        self.d[self.key(text, task)] = val

    def save(self):
        if self.path:
            tmp = self.path + '.tmp'
            json.dump(self.d, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
            os.replace(tmp, self.path)


# ── 백엔드 ──────────────────────────────────────────────────────────
class RulesBackend:
    """사용자 규칙 함수 fn(text)->dict. 무료·결정적·오프라인."""
    kind = 'rules'
    free = True

    def __init__(self, fn):
        self.fn = fn

    def run_one(self, text, task):
        return self.fn(text)


class MockBackend:
    """스키마 모양의 빈 결과. 무료 — 파이프라인 배선·테스트용(호출 0)."""
    kind = 'mock'
    free = True

    def run_one(self, text, task):
        return task.empty()


class AnthropicBackend:
    """실제 Claude 호출. 유료 — 3중 게이트 없이는 process()가 호출 자체를 거부한다."""
    kind = 'anthropic'
    free = False

    def __init__(self, model=None):
        self.model = model or DEFAULT_MODEL
        self._client = None

    def credentials(self):
        """자격 해석 여부만 확인(호출 없음). 없으면 False."""
        try:
            import anthropic
        except ImportError:
            return False
        try:
            c = anthropic.Anthropic()
            if getattr(c, 'api_key', None) or getattr(c, 'auth_token', None):
                self._client = c
                return True
        except Exception:
            pass
        return False

    def run_one(self, text, task):
        # C3 classify_llm.py와 동일한 structured-output 패턴(프로젝트 확립 형태)
        resp = self._client.messages.create(
            model=self.model, max_tokens=task.max_tokens, system=task.system,
            output_config={"format": {"type": "json_schema", "schema": task.schema}},
            messages=[{"role": "user", "content": task.user_tmpl.format(text=text)}],
        )
        if getattr(resp, 'stop_reason', None) == 'refusal':
            r = task.empty(); r['_refused'] = True; return r
        txt = next((b.text for b in resp.content if b.type == 'text'), '{}')
        return json.loads(txt)


# ── 파이프라인 ──────────────────────────────────────────────────────
def estimate(inputs, task):
    """호출 없이 토큰·요금 추정. (입력 토큰 = system+각 입력, 출력 = max_tokens 상한 가정)"""
    sys_tok = est_tokens(task.system)
    in_tok = sum(sys_tok + est_tokens(task.user_tmpl.format(text=t)) for t in inputs)
    out_tok = task.max_tokens * len(inputs)
    pin, pout = PRICING.get(task.model, (0, 0))
    usd = in_tok / 1e6 * pin + out_tok / 1e6 * pout
    return {'calls': len(inputs), 'in_tokens': in_tok, 'out_tokens_max': out_tok,
            'usd_approx': round(usd, 4), 'model': task.model}


def process(inputs, task, backend, cache=None, dry_run=False,
            confirm_spend=False, max_calls=None, log=None, on_progress=None):
    """inputs(문자열 리스트) → 결과 dict 리스트 + 리포트.
    유료 백엔드는 dry_run=False일 때 (자격) ∧ (confirm_spend) ∧ (max_calls) 전부라야 호출."""
    def _log(m):
        (log.info(m) if log else print(m))

    est = estimate(inputs, task)
    if dry_run:
        _log(f"[dry-run] {task.name}: {est['calls']}건 · 입력~{est['in_tokens']:,}tok · "
             f"출력상한~{est['out_tokens_max']:,}tok · 추정 ${est['usd_approx']} ({est['model']})")
        return [None] * len(inputs), {'mode': 'dry_run', **est}

    # 유료 게이트
    if not getattr(backend, 'free', False):
        if not confirm_spend:
            raise PermissionError(f"유료 백엔드({backend.kind}) 호출 차단: confirm_spend=True 필요. "
                                  f"먼저 dry_run=True로 비용을 확인하세요. (추정 ${est['usd_approx']})")
        if max_calls is None:
            raise PermissionError("유료 백엔드는 max_calls(호출 상한)를 반드시 지정해야 합니다.")
        if len(inputs) > max_calls:
            raise PermissionError(f"입력 {len(inputs)}건 > max_calls {max_calls}. 상한을 넘습니다.")
        if not backend.credentials():
            raise PermissionError("API 자격 없음(ANTHROPIC_API_KEY 또는 ant auth login). 유료 호출 불가.")
        _log(f"[유료 승인됨] {task.name}: 최대 {len(inputs)}회 호출, 추정 ${est['usd_approx']}")

    results, n_cached, n_called, n_fail = [], 0, 0, 0
    for i, text in enumerate(inputs, 1):
        if cache is not None:
            hit = cache.get(text, task)
            if hit is not None:
                results.append(hit); n_cached += 1
                continue
        try:
            r = backend.run_one(text, task)
            n_called += 1
        except Exception as e:
            _log(f"  #{i} 실패 {type(e).__name__}: {str(e)[:60]}")
            r = task.empty(); r['_error'] = type(e).__name__; n_fail += 1
        if cache is not None:
            cache.put(text, task, r)
        results.append(r)
        if on_progress:
            on_progress(i, len(inputs), r)
    if cache is not None:
        cache.save()
    rep = {'mode': backend.kind, 'total': len(inputs), 'cached': n_cached,
           'called': n_called, 'failed': n_fail, 'est_usd_if_all_called': est['usd_approx']}
    _log(f"[완료] {task.name}: 총 {rep['total']} · 캐시 {n_cached} · 호출 {n_called} · 실패 {n_fail}")
    return results, rep
