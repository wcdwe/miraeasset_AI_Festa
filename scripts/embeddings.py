"""
연금 Agent 과제 - 임베딩 프로바이더

Vector DB 적재/검색이 특정 임베딩 구현에 묶이지 않도록 인터페이스를 분리한다.

- TfidfEmbeddingProvider (기본값): 문자 n-gram TF-IDF + TruncatedSVD(LSA)로
  코퍼스 전체를 저차원 밀집 벡터로 투영. 사전학습 모델 다운로드가 전혀
  필요 없어(개발 환경 네트워크 정책상 huggingface.co가 막혀 있어 사전학습
  모델을 받을 수 없었다) 지금 바로 오프라인으로 쓸 수 있는 기본 프로바이더다.
  문자 n-gram이라 형태소 분석기 없이도 한국어 부분 문자열 유사도를 어느
  정도 잡아낸다. 코퍼스에 fit이 필요한 상태 저장형 프로바이더라 fit/save/load를
  구현한다.
- LocalEmbeddingProvider: sentence-transformers 다국어 모델(로컬 추론, API 키
  불필요). huggingface.co 접근이 가능한 환경으로 옮기면 이 프로바이더로
  바꾸는 것을 권장 (TF-IDF보다 의미 검색 품질이 좋음). 이 개발 환경에서는
  모델 다운로드 자체가 막혀 있어 검증하지 못했다.
- HyperClovaEmbeddingProvider: 네이버 클로바스튜디오 임베딩 API. 키가 없어
  아직 검증하지 못했으므로, 키가 발급되면 엔드포인트/응답 필드를 실제
  응답으로 검증할 것 (README TODO 참고).

get_provider(name)로 이름 기반 선택. 기본은 환경변수 EMBEDDING_PROVIDER,
없으면 "tfidf".
"""

import os
import pickle
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    name: str

    @abstractmethod
    def embed(self, texts, is_query=False):
        """texts(list[str]) -> list[list[float]]. is_query는 query/passage
        프리픽스를 구분하는 모델(e5 계열 등)을 위한 힌트."""
        raise NotImplementedError

    @property
    @abstractmethod
    def dimension(self):
        raise NotImplementedError

    # 상태 저장형 프로바이더(TF-IDF 등)를 위한 훅. 기본은 no-op.
    def fit(self, texts):
        pass

    def save(self, path):
        pass

    def load(self, path):
        pass


class TfidfEmbeddingProvider(EmbeddingProvider):
    """문자 n-gram TF-IDF + TruncatedSVD(LSA) 기반 로컬 임베딩 (기본 프로바이더).

    사전학습 가중치 다운로드 없이 코퍼스 자체에 fit해서 쓴다. build_vector_store.py가
    전체 청크 텍스트로 fit()한 뒤 save()하고, search.py는 load()해서 질의를 같은
    벡터 공간에 투영한다. fit 전에 embed()를 호출하면 에러를 낸다.
    """

    name = "tfidf"
    N_COMPONENTS = int(os.environ.get("TFIDF_SVD_COMPONENTS", "256"))

    def __init__(self):
        self._vectorizer = None
        self._svd = None

    def fit(self, texts):
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import TruncatedSVD

        self._vectorizer = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(2, 4), max_features=50000, sublinear_tf=True,
        )
        matrix = self._vectorizer.fit_transform(texts)

        n_components = min(self.N_COMPONENTS, matrix.shape[0] - 1, matrix.shape[1] - 1)
        self._svd = TruncatedSVD(n_components=n_components, random_state=42)
        self._svd.fit(matrix)

    def _require_fitted(self):
        if self._vectorizer is None or self._svd is None:
            raise RuntimeError(
                "TfidfEmbeddingProvider가 fit되지 않았습니다. "
                "build_vector_store.py로 적재하거나 load()로 저장된 상태를 불러오세요."
            )

    def embed(self, texts, is_query=False):
        self._require_fitted()
        import numpy as np

        sparse = self._vectorizer.transform(texts)
        dense = self._svd.transform(sparse)
        norms = np.linalg.norm(dense, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (dense / norms).tolist()

    @property
    def dimension(self):
        self._require_fitted()
        return self._svd.n_components

    def save(self, path):
        self._require_fitted()
        with open(path, "wb") as f:
            pickle.dump({"vectorizer": self._vectorizer, "svd": self._svd}, f)

    def load(self, path):
        with open(path, "rb") as f:
            state = pickle.load(f)
        self._vectorizer = state["vectorizer"]
        self._svd = state["svd"]


class LocalEmbeddingProvider(EmbeddingProvider):
    """sentence-transformers 다국어 모델 (huggingface.co 접근 가능 환경에서만 동작).

    이 개발 환경은 huggingface.co로의 아웃바운드가 정책상 차단되어 있어
    모델 다운로드를 검증하지 못했다. 접근 가능한 환경으로 옮기면
    EMBEDDING_PROVIDER=sentence_transformers 로 전환해서 쓸 것.
    """

    name = "sentence_transformers"

    MODEL_NAME = os.environ.get("LOCAL_EMBEDDING_MODEL", "intfloat/multilingual-e5-small")

    _model = None

    def _load_model(self):
        if LocalEmbeddingProvider._model is None:
            from sentence_transformers import SentenceTransformer
            LocalEmbeddingProvider._model = SentenceTransformer(self.MODEL_NAME)
        return LocalEmbeddingProvider._model

    def embed(self, texts, is_query=False):
        model = self._load_model()
        prefix = "query: " if is_query else "passage: "
        prefixed = [f"{prefix}{t}" for t in texts]
        vectors = model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return vectors.tolist()

    @property
    def dimension(self):
        return 384


class HyperClovaEmbeddingProvider(EmbeddingProvider):
    """
    네이버 클로바스튜디오 임베딩 API 프로바이더 (미검증 - API 키 발급 후 검증 필요).

    필요 환경변수:
    - NCP_CLOVASTUDIO_API_KEY : Bearer 토큰
    - NCP_CLOVASTUDIO_APIGW_KEY : APIGW 키 (일부 상품/버전에서 필요)
    - NCP_CLOVASTUDIO_EMBEDDING_URL : 임베딩 엔드포인트
      (기본값은 공개 문서 기준 v2 embedding endpoint 형태이며, 실제 발급받은
      앱 ID/버전에 맞게 .env로 덮어쓸 것)
    """

    name = "hyperclova"

    DEFAULT_URL = "https://clovastudio.stream.ntruss.com/testapp/v1/api-tools/embedding/v2"

    def __init__(self):
        self.api_key = os.environ.get("NCP_CLOVASTUDIO_API_KEY")
        self.apigw_key = os.environ.get("NCP_CLOVASTUDIO_APIGW_KEY")
        self.url = os.environ.get("NCP_CLOVASTUDIO_EMBEDDING_URL", self.DEFAULT_URL)
        if not self.api_key:
            raise RuntimeError(
                "NCP_CLOVASTUDIO_API_KEY가 설정되지 않았습니다. "
                "HyperCLOVA X 임베딩 API 키 발급 후 .env에 설정하세요."
            )

    def _headers(self):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.apigw_key:
            headers["X-NCP-APIGW-API-KEY"] = self.apigw_key
        return headers

    def embed(self, texts, is_query=False):
        import requests

        vectors = []
        for text in texts:
            resp = requests.post(
                self.url,
                headers=self._headers(),
                json={"text": text},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
            # 실제 응답 스키마는 키 발급 후 확인 필요. 공개 문서 기준 추정값.
            vectors.append(body["result"]["embedding"])
        return vectors

    @property
    def dimension(self):
        return int(os.environ.get("NCP_CLOVASTUDIO_EMBEDDING_DIM", "1024"))


PROVIDERS = {
    "tfidf": TfidfEmbeddingProvider,
    "sentence_transformers": LocalEmbeddingProvider,
    "hyperclova": HyperClovaEmbeddingProvider,
}


def get_provider(name=None):
    name = name or os.environ.get("EMBEDDING_PROVIDER", "tfidf")
    if name not in PROVIDERS:
        raise ValueError(f"알 수 없는 임베딩 프로바이더: {name} (선택지: {list(PROVIDERS)})")
    return PROVIDERS[name]()
