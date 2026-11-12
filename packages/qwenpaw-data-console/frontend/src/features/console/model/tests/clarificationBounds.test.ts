import assert from 'node:assert/strict';
import test from 'node:test';
import type { TraceMessage } from '../../api/types.js';
import { parseAskUserQuestion } from '../chat/askUserQuestion.js';

function call(optionCount: number): TraceMessage {
  return {
    type: 'plugin_call',
    status: 'completed',
    chat_id: 'chat-test',
    content: [
      {
        type: 'data',
        data: {
          name: 'ask_user_question',
          call_id: 'call-test',
          arguments: {
            title: '确认范围',
            questions: [
              {
                question: '选择范围',
                multiSelect: false,
                options: Array.from({ length: optionCount }, (_, index) => ({
                  label: `范围${index}`,
                })),
              },
            ],
          },
        },
      },
    ],
  } as unknown as TraceMessage;
}

test('clarification accepts the backend option bounds of one through four', () => {
  for (const count of [1, 2, 4]) {
    assert.equal(parseAskUserQuestion(call(count))?.questions[0].options.length, count);
  }
  for (const count of [0, 5, 6]) {
    assert.equal(parseAskUserQuestion(call(count)), null);
  }
});
