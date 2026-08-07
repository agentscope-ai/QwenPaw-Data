// ── Node color mapping ────────────────────────────────────────────────────────
// Each node type has a distinct color for clear visual differentiation.
export const NODE_TYPE_COLORS: Record<string, string> = {
  "*": "#78909c",
  Metric: "#4caf50",
  Dimension: "#e91e63",
  Caliber: "#8d6e63",
  Claim: "#a1887f",
  Column: "#ef5350",
  Database: "#7b1fa2",
  Dataset: "#1565c0",
  DatasetColumn: "#42a5f5",
  DataSource: "#00897b",
  DimensionValue: "#ab47bc",
  Domain: "#26a69a",
  Entity: "#5c6bc0",
  Event: "#ff8f00",
  Experience: "#6d4c41",
  Formula: "#00acc1",
  MGShadow: "#66bb6a",
  Operator: "#546e7a",
  Schema: "#29b6f6",
  Step: "#ffa726",
  Strategy: "#ff7043",
  Table: "#26c6da",
  Tag: "#ec407a",
  Task: "#7e57c2",
  ToolCall: "#009688",
  Trace: "#f06292",
  Turn: "#3949ab",
  User: "#8e24aa",
};

/**
 * Dynamic color palette for node types not predefined above.
 * When a type is encountered that has no entry in NODE_TYPE_COLORS,
 * pick the next color from this palette (cycled).
 */
export const DYNAMIC_PALETTE = [
  "#e53935", "#8e24aa", "#5e35b1", "#3949ab", "#1e88e5",
  "#039be5", "#00acc1", "#00897b", "#43a047", "#7cb342",
  "#c0ca33", "#fdd835", "#ffb300", "#fb8c00", "#f4511e",
  "#6d4c41", "#757575", "#546e7a", "#d81b60", "#1565c0",
];

let _dynamicIndex = 0;
/** Get a color for a node type, falling back to the dynamic palette. */
export function getNodeColor(type: string): string {
  if (NODE_TYPE_COLORS[type]) return NODE_TYPE_COLORS[type];
  // Assign a new color from the palette and cache it
  const color = DYNAMIC_PALETTE[_dynamicIndex % DYNAMIC_PALETTE.length];
  NODE_TYPE_COLORS[type] = color;
  _dynamicIndex++;
  return color;
}
