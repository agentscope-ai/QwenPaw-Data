import type { Plan, PlanTask, TaskState, TraceMessage } from '../api/types';

const PLAN_TOOL_NAMES = new Set([
  'PlanCreate',
  'PlanUpdate',
  'TaskStateUpdate',
]);

type PlanChangeAction = 'add' | 'revise' | 'delete';

interface PlanCreateTaskInput {
  id: string;
  subject: string;
  description: string;
  owner?: string | null;
  blocked_by?: string[];
}

interface PlanTaskRevision {
  subject: string;
  description: string;
  owner?: string | null;
  blocked_by?: string[];
}

interface PlanTaskChange {
  task_id: string;
  action: PlanChangeAction;
  task?: PlanTaskRevision | null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function parseArguments(raw: unknown): Record<string, unknown> | null {
  if (typeof raw === 'string') {
    try {
      return asRecord(JSON.parse(raw));
    } catch {
      return null;
    }
  }
  return asRecord(raw);
}

function pluginPayload(message: TraceMessage): Record<string, unknown> | null {
  for (const block of message.content) {
    if (block.type === 'data') {
      const data = asRecord(block.data);
      if (data) return data;
    }
  }
  return null;
}

function normalizeTasks(tasks: PlanTask[]): PlanTask[] {
  const normalized = tasks.map((task) => ({
    ...task,
    blocked_by: [...task.blocked_by],
    blocks: [] as string[],
    metadata: { ...task.metadata },
  }));
  const byId = new Map<string, PlanTask>();
  for (const task of normalized) {
    if (byId.has(task.id)) {
      throw new Error(`duplicate task id: ${task.id}`);
    }
    byId.set(task.id, task);
  }
  for (const task of normalized) {
    const blockedBy: string[] = [];
    for (const dependencyId of task.blocked_by) {
      if (!byId.has(dependencyId)) {
        throw new Error(
          `task ${task.id} references unknown dependency: ${dependencyId}`,
        );
      }
      if (!blockedBy.includes(dependencyId)) blockedBy.push(dependencyId);
    }
    task.blocked_by = blockedBy;
  }
  for (const task of normalized) {
    for (const dependencyId of task.blocked_by) {
      byId.get(dependencyId)?.blocks.push(task.id);
    }
  }
  return normalized;
}

function toPlan(tasks: PlanTask[]): Plan | null {
  if (tasks.length === 0) return null;
  return { tasks };
}

function taskFromCreateInput(input: PlanCreateTaskInput): PlanTask {
  return {
    id: input.id,
    subject: input.subject,
    description: input.description,
    metadata: {},
    state: 'pending',
    owner: input.owner ?? null,
    blocks: [],
    blocked_by: [...(input.blocked_by ?? [])],
  };
}

export function applyPlanCreate(args: Record<string, unknown>): Plan | null {
  const tasksRaw = args.tasks;
  if (!Array.isArray(tasksRaw)) {
    throw new Error('PlanCreate requires tasks');
  }
  const tasks = tasksRaw.map((item) => {
    const row = asRecord(item);
    if (!row || typeof row.id !== 'string' || typeof row.subject !== 'string'
      || typeof row.description !== 'string') {
      throw new Error('invalid PlanCreate task');
    }
    return taskFromCreateInput({
      id: row.id,
      subject: row.subject,
      description: row.description,
      owner: typeof row.owner === 'string' ? row.owner : null,
      blocked_by: Array.isArray(row.blocked_by)
        ? row.blocked_by.filter((value): value is string => typeof value === 'string')
        : [],
    });
  });
  return toPlan(normalizeTasks(tasks));
}

export function applyPlanUpdate(
  current: Plan | null,
  args: Record<string, unknown>,
): Plan | null {
  if (!current?.tasks.length) {
    throw new Error('no current plan');
  }
  const changesRaw = args.changes;
  if (!Array.isArray(changesRaw)) {
    throw new Error('PlanUpdate requires changes');
  }
  const changes = changesRaw.map((item) => {
    const row = asRecord(item);
    if (!row || typeof row.task_id !== 'string' || typeof row.action !== 'string') {
      throw new Error('invalid PlanUpdate change');
    }
    const action = row.action as PlanChangeAction;
    if (action !== 'add' && action !== 'revise' && action !== 'delete') {
      throw new Error(`invalid PlanUpdate action: ${action}`);
    }
    const taskRow = asRecord(row.task);
    const change: PlanTaskChange = {
      task_id: row.task_id,
      action,
      task: taskRow && typeof taskRow.subject === 'string'
        && typeof taskRow.description === 'string'
        ? {
            subject: taskRow.subject,
            description: taskRow.description,
            owner: typeof taskRow.owner === 'string' ? taskRow.owner : null,
            blocked_by: Array.isArray(taskRow.blocked_by)
              ? taskRow.blocked_by.filter(
                (value): value is string => typeof value === 'string',
              )
              : [],
          }
        : null,
    };
    return change;
  });

  const changeIds = changes.map((change) => change.task_id);
  if (new Set(changeIds).size !== changeIds.length) {
    throw new Error('duplicate task_id in PlanUpdate changes');
  }

  let tasks = current.tasks.map((task) => ({
    ...task,
    blocked_by: [...task.blocked_by],
    blocks: [...task.blocks],
    metadata: { ...task.metadata },
  }));

  for (const change of changes) {
    const index = tasks.findIndex((task) => task.id === change.task_id);
    if (change.action === 'add') {
      if (index >= 0) throw new Error(`task already exists: ${change.task_id}`);
      if (!change.task) throw new Error(`task is required for add: ${change.task_id}`);
      tasks.push(taskFromCreateInput({
        id: change.task_id,
        subject: change.task.subject,
        description: change.task.description,
        owner: change.task.owner,
        blocked_by: change.task.blocked_by,
      }));
    } else if (change.action === 'revise') {
      if (index < 0) throw new Error(`task not found: ${change.task_id}`);
      if (!change.task) {
        throw new Error(`task is required for revise: ${change.task_id}`);
      }
      const previous = tasks[index];
      tasks[index] = {
        ...previous,
        subject: change.task.subject,
        description: change.task.description,
        owner: change.task.owner ?? null,
        blocked_by: [...(change.task.blocked_by ?? [])],
        state: 'pending',
      };
    } else {
      if (index < 0) throw new Error(`task not found: ${change.task_id}`);
      tasks = tasks.filter((task) => task.id !== change.task_id);
      for (const task of tasks) {
        task.blocked_by = task.blocked_by.filter(
          (dependencyId) => dependencyId !== change.task_id,
        );
      }
    }
  }
  return toPlan(normalizeTasks(tasks));
}

export function applyTaskStateUpdate(
  current: Plan | null,
  args: Record<string, unknown>,
): Plan | null {
  if (!current?.tasks.length) {
    throw new Error('no current plan');
  }
  const taskId = args.task_id;
  const state = args.state;
  if (typeof taskId !== 'string' || typeof state !== 'string') {
    throw new Error('TaskStateUpdate requires task_id and state');
  }
  if (state !== 'pending' && state !== 'in_progress' && state !== 'completed') {
    throw new Error(`invalid task state: ${state}`);
  }
  const tasks = current.tasks.map((task) => (
    task.id === taskId
      ? { ...task, state: state as TaskState }
      : { ...task, blocked_by: [...task.blocked_by], blocks: [...task.blocks] }
  ));
  if (!tasks.some((task) => task.id === taskId)) {
    throw new Error(`task not found: ${taskId}`);
  }
  return toPlan(normalizeTasks(tasks));
}

function isFailedToolOutput(payload: Record<string, unknown>): boolean {
  const state = payload.state;
  if (typeof state === 'string' && state.toLowerCase() === 'error') {
    return true;
  }
  return false;
}

export function applyPlanToolOutput(
  current: Plan | null,
  messages: TraceMessage[],
  outputMessage: TraceMessage,
): Plan | null {
  if (outputMessage.type !== 'plugin_call_output') return current;
  const output = pluginPayload(outputMessage);
  if (!output || isFailedToolOutput(output)) return current;

  const toolName = typeof output.name === 'string' ? output.name : null;
  if (!toolName || !PLAN_TOOL_NAMES.has(toolName)) return current;

  const callId = outputMessage.source_id
    ?? (typeof output.call_id === 'string' ? output.call_id : null);
  if (!callId) return current;

  const callMessage = [...messages]
    .reverse()
    .find((message) => (
      message.type === 'plugin_call' && message.source_id === callId
    ));
  if (!callMessage) return current;

  const callPayload = pluginPayload(callMessage);
  const args = parseArguments(callPayload?.arguments);
  if (!args) return current;

  try {
    if (toolName === 'PlanCreate') return applyPlanCreate(args);
    if (toolName === 'PlanUpdate') return applyPlanUpdate(current, args);
    return applyTaskStateUpdate(current, args);
  } catch {
    return current;
  }
}

/** Rebuild plan from historical Plan* tool outputs when chat.plan is empty. */
export function replayPlanFromMessages(messages: TraceMessage[]): Plan | null {
  const ordered = [...messages].sort((left, right) => left.sequence - right.sequence);
  let plan: Plan | null = null;
  for (const message of ordered) {
    plan = applyPlanToolOutput(plan, ordered, message);
  }
  return plan;
}
