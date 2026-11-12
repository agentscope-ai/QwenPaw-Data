import type { Plan, PlanTask } from '../../api/types';

function cloneTask(task: PlanTask): PlanTask {
  return {
    ...task,
    metadata: { ...task.metadata },
    blocks: [...task.blocks],
    blocked_by: [...task.blocked_by],
  };
}

export function createPlanDraft(plan: Plan): PlanTask[] {
  return plan.tasks.map(cloneTask);
}

export function updatePlanTaskSubject(
  tasks: PlanTask[],
  taskId: string,
  subject: string,
): PlanTask[] {
  return tasks.map((task) => (task.id === taskId ? { ...task, subject } : task));
}

export function appendPlanTask(tasks: PlanTask[]): PlanTask[] {
  const existingIds = new Set(tasks.map((task) => task.id));
  let suffix = tasks.length + 1;
  while (existingIds.has(`user_${suffix}`)) suffix += 1;

  return [
    ...tasks,
    {
      id: `user_${suffix}`,
      subject: '',
      description: '',
      metadata: {},
      state: 'pending',
      owner: null,
      blocks: [],
      blocked_by: [],
    },
  ];
}

export function removePlanTask(tasks: PlanTask[], taskId: string): PlanTask[] {
  if (!tasks.some((task) => task.id === taskId)) return tasks;

  const kept = tasks.filter((task) => task.id !== taskId);
  const keptIds = new Set(kept.map((task) => task.id));
  return kept.map((task) => ({
    ...task,
    blocks: task.blocks.filter((id) => keptIds.has(id)),
    blocked_by: task.blocked_by.filter((id) => keptIds.has(id)),
  }));
}

export function buildPlanSubmission(tasks: PlanTask[]): Plan {
  const taskIds = new Set(tasks.map((task) => task.id));
  return {
    tasks: tasks.map((task) => {
      const subject = task.subject.trim();
      return {
        ...cloneTask(task),
        subject,
        description: task.description.trim() || subject,
        blocks: [],
        blocked_by: task.blocked_by.filter((id) => taskIds.has(id)),
      };
    }),
  };
}
