import assert from 'node:assert/strict';
import test from 'node:test';
import { DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL, embeddingModelOptions } from '../../../../pages/AgentSettings/embeddingModels.js';

test('embedding catalog keeps existing custom selections while offering defaults', () => {
  assert.equal(DEFAULT_EMBEDDING_DIM, 1024);
  assert.equal(DEFAULT_EMBEDDING_MODEL, 'text-embedding-v3');
  assert.deepEqual(embeddingModelOptions().map(option => option.value), ['text-embedding-v3', 'text-embedding-v4']);
  assert.deepEqual(embeddingModelOptions('custom-model').map(option => option.value), ['custom-model', 'text-embedding-v3', 'text-embedding-v4']);
  assert.equal(embeddingModelOptions('text-embedding-v3').length, 2);
});
