#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ml.py — 학습된 ML 백엔드 (ai.py의 3번째 티어, 2026-08)

ai.py 백엔드 = 규칙(무료·zero-shot) · LLM(유료·zero-shot). 여기 **학습된 ML**을 얹는다:
  고객이 준 **라벨 데이터로 분류기를 학습** → 예측은 무료·즉시. LLM보다 대량에서 싸고, 규칙보다 정확한 중간 티어.

라벨 데이터 → train_test_split → XGBClassifier → classification_report → joblib 저장의 범용 텍스트 분류 파이프라인.

  from ml import TrainedMLBackend
  be = TrainedMLBackend(target_field='intent')
  rep = be.fit(texts, labels)          # 라벨 데이터로 학습 + 홀드아웃 평가
  # ai.process 에 그대로 꽂힘(free=True):  from ai import process; process(new_texts, task, be)

★ai.py를 오염 안 시키려 무거운 임포트(sklearn/xgboost)는 이 모듈 안에서만·지연 로드.
★free=True = '예측 실행'이 무료(로컬)라는 뜻. 학습은 fit()에서 1회.
"""
import os, json

_HEAVY = {}


def _load():
    """sklearn/xgboost 지연 로드. xgboost 없으면 sklearn RandomForest로 폴백."""
    if _HEAVY:
        return _HEAVY
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import classification_report, accuracy_score
    import joblib
    _HEAVY.update(dict(Tfidf=TfidfVectorizer, split=train_test_split,
                       report=classification_report, acc=accuracy_score, joblib=joblib))
    try:
        from xgboost import XGBClassifier
        _HEAVY['make_clf'] = lambda n: XGBClassifier(
            n_estimators=100, max_depth=4, learning_rate=0.1, subsample=0.8,
            colsample_bytree=0.8, random_state=42, eval_metric='mlogloss')
        _HEAVY['clf_name'] = 'XGBoost'
    except Exception:
        from sklearn.ensemble import RandomForestClassifier
        _HEAVY['make_clf'] = lambda n: RandomForestClassifier(n_estimators=200, random_state=42)
        _HEAVY['clf_name'] = 'RandomForest(폴백)'
    return _HEAVY


class TrainedMLBackend:
    """텍스트 → 단일 라벨 분류기(TF-IDF + XGBoost). 고객 라벨 데이터로 학습해 납품.
    ai.py 백엔드 인터페이스(run_one, free) 준수 → ai.process()에 그대로 사용."""
    kind = 'ml'
    free = True   # 예측(run_one)은 무료·로컬. 학습은 fit()에서 별도 1회.

    def __init__(self, target_field, ngram=(2, 4), min_df=1, analyzer='char_wb'):
        # ★한국어 단문 기본 = 문자 n-gram(char_wb). 단어 TF-IDF는 조사·띄어쓰기로 희소해져 단문에 약함(실측).
        #   영어·긴 문서면 analyzer='word' 로 바꾸면 됨.
        self.target_field = target_field
        self.ngram = ngram
        self.min_df = min_df
        self.analyzer = analyzer
        self._vec = None
        self._clf = None
        self._labels = None
        self._le = None   # 라벨 문자열↔정수 인코딩(xgboost 요구)

    def fit(self, texts, labels, test_size=0.2, min_examples=8):
        """라벨 데이터로 학습 + 홀드아웃 평가. 반환 = 성능 리포트(정확도·클래스별)."""
        H = _load()
        if len(texts) < min_examples:
            raise ValueError(f"학습 예시 {len(texts)}개 < 최소 {min_examples}. 라벨 데이터가 더 필요합니다.")
        uniq = sorted(set(labels))
        self._labels = uniq
        idx = {l: i for i, l in enumerate(uniq)}
        y = [idx[l] for l in labels]
        self._le = uniq
        self._vec = H['Tfidf'](analyzer=self.analyzer, ngram_range=self.ngram, min_df=self.min_df, sublinear_tf=True)
        X = self._vec.fit_transform(texts)
        # 클래스별 표본이 2 미만이면 stratify 불가 → 일반 분할
        strat = y if all(y.count(c) >= 2 for c in set(y)) and len(uniq) > 1 else None
        Xtr, Xte, ytr, yte = H['split'](X, y, test_size=test_size, random_state=42, stratify=strat)
        self._clf = H['make_clf'](len(uniq))
        self._clf.fit(Xtr, ytr)
        yp = self._clf.predict(Xte)
        acc = float(H['acc'](yte, yp))
        rep = H['report'](yte, yp, target_names=[str(u) for u in uniq], output_dict=True, zero_division=0)
        return {'target_field': self.target_field, 'classifier': H['clf_name'],
                'n_train': len(ytr), 'n_test': len(yte), 'classes': uniq,
                'holdout_accuracy': round(acc, 3),
                'per_class_f1': {u: round(rep.get(str(u), {}).get('f1-score', 0), 3) for u in uniq}}

    def run_one(self, text, task):
        """예측 → task 스키마 모양 dict(target_field만 채움, 나머지 빈값)."""
        if self._clf is None:
            raise RuntimeError("학습되지 않음: 먼저 fit(texts, labels)를 호출하세요.")
        pred_i = int(self._clf.predict(self._vec.transform([text]))[0])
        out = task.empty() if task else {}
        out[self.target_field] = self._le[pred_i]
        return out

    def save(self, path):
        H = _load()
        H['joblib'].dump({'vec': self._vec, 'clf': self._clf, 'labels': self._labels,
                          'le': self._le, 'target_field': self.target_field}, path)
        return {'path': path, 'size_kb': round(os.path.getsize(path) / 1024, 1)}

    @classmethod
    def load(cls, path):
        H = _load()
        d = H['joblib'].load(path)
        be = cls(d['target_field'])
        be._vec, be._clf, be._labels, be._le = d['vec'], d['clf'], d['labels'], d['le']
        return be
