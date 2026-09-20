"""
core/face_matcher.py
Redis HNSW vector index for fast face similarity search.

Cosine DISTANCE in Redis = 1 - cosine_similarity.
  similarity >= threshold  →  MATCH (threshold varies by IR/RGB mode)
"""
from typing import Optional

import numpy as np
import redis as redis_lib
from redis.commands.search.field import VectorField, TextField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.query import Query

import config as cfg

_KEY_PREFIX = "door_person:"


class FaceMatcherError(Exception):
    """Redis is unreachable or unusable during a matcher operation.

    Raised (instead of returning a "no match" result) so callers can tell a
    genuine no-match from an outage — an outage must never be treated as
    "everyone is unknown" (which would deny known users and fake the alert
    chain)."""


class FaceMatcher:
    _r = None

    @classmethod
    def _redis(cls):
        if cls._r is None:
            cls._r = redis_lib.Redis(
                host=cfg.REDIS_HOST,
                port=cfg.REDIS_PORT,
                db=getattr(cfg, "REDIS_DB", 0),
                decode_responses=False,
            )
            cls._ensure_index()
        return cls._r

    @classmethod
    def _ensure_index(cls):
        try:
            cls._r.ft(cfg.REDIS_INDEX_NAME).info()
            print(f"[FaceMatcher] Redis index '{cfg.REDIS_INDEX_NAME}' already exists")
        except Exception:
            cls._r.ft(cfg.REDIS_INDEX_NAME).create_index(
                [
                    TextField("person_id"),
                    TextField("name"),
                    VectorField("embedding", "HNSW", {
                        "TYPE": "FLOAT32",
                        "DIM": cfg.VECTOR_DIM,
                        "DISTANCE_METRIC": "COSINE",
                        "INITIAL_CAP": 200,
                    }),
                ],
                definition=IndexDefinition(
                    prefix=[_KEY_PREFIX],
                    index_type=IndexType.HASH,
                ),
            )
            print(f"[FaceMatcher] Created Redis HNSW index '{cfg.REDIS_INDEX_NAME}'")

    @classmethod
    def upsert(cls, key: str, person_id: str, name: str, embedding: np.ndarray):
        """Insert/update one embedding.

        Args:
            key: unique index key (Cloud embedding `key`, e.g. "T002:normal").
                 One person may have several variants → several keys.
            person_id: the person this embedding belongs to (search returns this)
            name: display name of the person
            embedding: 512-dim float32 vector
        """
        r = cls._redis()
        r.hset(f"{_KEY_PREFIX}{key}", mapping={
            "person_id": person_id,
            "name": name,
            "embedding": embedding.astype(np.float32).tobytes(),
        })

    @classmethod
    def search(cls, embedding: np.ndarray, similarity_threshold: float = None) -> Optional[dict]:
        """Returns {person_id, name, similarity} or None if no match above threshold.

        similarity_threshold: override the default cfg.SIMILARITY_THRESHOLD
        (e.g. use a lower threshold in IR mode where embeddings are less
        discriminative).
        """
        thr = similarity_threshold if similarity_threshold is not None else cfg.SIMILARITY_THRESHOLD
        dist_thr = 1.0 - thr
        r = cls._redis()
        # Index vectors arrive L2-normalized; normalize the probe too so the
        # cosine distance in Redis is a plain dot product.
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm
        vec_bytes = embedding.astype(np.float32).tobytes()
        for attempt in (1, 2):
            try:
                q = (
                    Query("*=>[KNN 1 @embedding $vec AS score]")
                    .sort_by("score")
                    .return_fields("person_id", "name", "score")
                    .dialect(2)
                )
                results = r.ft(cfg.REDIS_INDEX_NAME).search(
                    q, query_params={"vec": vec_bytes}
                )
                break
            except Exception as e:
                if attempt == 1:
                    # Likely a lost index (Redis restarted / FLUSHALL). The
                    # index is normally created once when the client is first
                    # built; rebuild it lazily instead of denying forever.
                    cls._ensure_index()
                    continue
                raise FaceMatcherError(f"Redis search failed: {e}") from e
        if not results.docs:
            return None
        doc = results.docs[0]
        distance = float(doc.score)
        # NaN-safe: a degenerate (all-zero/NaN) probe yields NaN distance; the
        # natural `>` comparison would be False and wrongly accept it as a
        # match. Only a genuinely small distance matches.
        if not (distance <= dist_thr):
            return None
        return {
            "person_id": doc.person_id,
            "name": doc.name,
            "similarity": round(1.0 - distance, 4),
        }

    @classmethod
    def count(cls) -> int:
        for attempt in (1, 2):
            try:
                info = cls._redis().ft(cfg.REDIS_INDEX_NAME).info()
                return int(info.get("num_docs", 0))
            except Exception as e:
                if attempt == 1:
                    try:
                        cls._ensure_index()
                    except Exception:
                        pass
                    continue
                raise FaceMatcherError(f"Redis count failed: {e}") from e
        return 0

    @classmethod
    def clear(cls):
        r = cls._redis()
        keys = r.keys(f"{_KEY_PREFIX}*")
        if keys:
            r.delete(*keys)
        print("[FaceMatcher] Index cleared")

    @classmethod
    def delete(cls, person_id: str):
        """Delete a specific person from Redis."""
        r = cls._redis()
        key = f"{_KEY_PREFIX}{person_id}"
        r.delete(key)
        print(f"[FaceMatcher] Deleted {person_id}")
