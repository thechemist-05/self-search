# tests/unit/test_embeddings.py
"""Tests for the embeddings utility (src/embeddings/embeddings.py)."""

import json
from unittest.mock import patch

import pytest

from src.embeddings.embeddings import get_embedding, embed_batch

# Mock response payload that Ollama would return
MOCK_RESPONSE = {
    "embedding": [0.1, 0.2, 0.3, 0.4],
}

@patch("src.embeddings.embeddings.requests.post")
def test_get_embedding_success(mock_post):
    mock_post.return_value.json.return_value = MOCK_RESPONSE
    mock_post.return_value.raise_for_status.return_value = None

    vec = get_embedding("test text")
    assert isinstance(vec, list)
    assert vec == MOCK_RESPONSE["embedding"]
    # Ensure the request body contains the correct model and prompt
    args, kwargs = mock_post.call_args
    payload = kwargs["json"]
    assert "model" in payload
    assert payload["prompt"] == "test text"

@patch("src.embeddings.embeddings.requests.post")
def test_get_embedding_failure(mock_post):
    mock_post.side_effect = Exception("network error")
    with pytest.raises(RuntimeError) as exc:
        get_embedding("boom")
    assert "Ollama request" in str(exc.value)

@patch("src.embeddings.embeddings.get_embedding")
def test_embed_batch_calls_get_embedding(mock_get):
    mock_get.side_effect = lambda txt: [len(txt), len(txt) + 1]
    texts = ["a", "bb", "ccc"]
    result = embed_batch(texts)
    assert result == [[1, 2], [2, 3], [3, 4]]
    assert mock_get.call_count == len(texts)
