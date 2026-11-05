import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { drainSseFrames } from '../../api/sseFrames.js';
import type { ConsoleChat, ConsoleStreamEvent } from '../../api/types.js';
import { createChatStreamProjection, projectStreamEvent } from '../projectStream.js';

test('Console replays a real official Host stream and restores its answer', { skip: !process.env.QWENPAW_DATA_CONSOLE_SMOKE_PATH }, () => {
  const { chat, events } = JSON.parse(readFileSync(process.env.QWENPAW_DATA_CONSOLE_SMOKE_PATH!, 'utf8')) as {chat: ConsoleChat; events: ConsoleStreamEvent[]};
  // A completed snapshot already has the terminal watermark. Replay from zero
  // as a fresh subscriber, rather than discarding all events as duplicates.
  let projection = createChatStreamProjection({ ...chat, last_sequence_number: -1 });
  const stream = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('');
  let buffer = '';
  // Exercise arbitrary network chunk boundaries, not just one event per read.
  for (let i = 0; i < stream.length; i += 73) {
    const result = drainSseFrames(buffer + stream.slice(i, i + 73));
    buffer = result.remainder;
    for (const event of result.events) projection = projectStreamEvent(projection, event);
  }
  assert.equal(buffer, '');
  assert.equal(projection.responseStatus, 'completed');
  assert.equal(projection.chat.status, 'completed');
  assert.equal(projection.streamError, null);
  assert.ok(projection.messages.some(m => m.type === 'message' && m.role === 'assistant' && m.content.some(c => c.type === 'text' && Boolean(c.text?.trim()))));
});
