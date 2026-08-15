"""
core/face_matcher.py
Redis HNSW vector index for fast face similarity search.

Cosine DISTANCE in Redis = 1 - cosine_similarity.
  similarity >= 0.70  →  distance <= 0.30  →  MATCH
"""
from typing import Optional

import numpy as np
import redis as redis_lib
from redis.commands.search.field import VectorField, TextField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.query import Query

import config as cfg

_DISTANCE_THRESHOLD = 1.0 - cfg.SIMILARITY_THRESHOLD   # 0.30
_KEY_PREFIX = "door_person:"


class FaceMatcher:
    _r = None

    @classmethod
    def _redis(cls):
        if cls._r is None:
            cls._r = redis_lib.Redis(
                host=cfg.REDIS_HOST,
                port=cfg.REDIS_PORT,
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
    def search(cls, embedding: np.ndarray) -> Optional[dict]:
        """Returns {person_id, name, similarity} or None if no match above threshold."""
        r = cls._redis()
        # Index vectors arrive L2-normalized; normalize the probe too so the
        # cosine distance in Redis is a plain dot product.
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm
        vec_bytes = embedding.astype(np.float32).tobytes()
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
            if not results.docs:
                return None
            doc = results.docs[0]
            distance = float(doc.score)
            if distance > _DISTANCE_THRESHOLD:
                return None
            return {
                "person_id": doc.person_id,
                "name": doc.name,
                "similarity": round(1.0 - distance, 4),
            }
        except Exception as e:
            print(f"[FaceMatcher] Search error: {e}")
            return None

    @classmethod
    def count(cls) -> int:
        try:
            info = cls._redis().ft(cfg.REDIS_INDEX_NAME).info()
            return int(info.get("num_docs", 0))
        except Exception:
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
