import type { ConsoleStreamEvent } from './types';

export function parseSseFrame(frame: string): ConsoleStreamEvent | null {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
    .join('\n');
  return data ? JSON.parse(data) as ConsoleStreamEvent : null;
}

export function drainSseFrames(
  input: string,
  flushTrailingFrame = false,
): { events: ConsoleStreamEvent[]; remainder: string } {
  let remainder = input.replace(/\r\n/g, '\n');
  const events: ConsoleStreamEvent[] = [];
  let boundary = remainder.indexOf('\n\n');

  while (boundary >= 0) {
    const event = parseSseFrame(remainder.slice(0, boundary));
    remainder = remainder.slice(boundary + 2);
    if (event) events.push(event);
    boundary = remainder.indexOf('\n\n');
  }

  if (flushTrailingFrame && remainder.trim()) {
    const event = parseSseFrame(remainder);
    if (event) events.push(event);
    remainder = '';
  }

  return { events, remainder };
}
