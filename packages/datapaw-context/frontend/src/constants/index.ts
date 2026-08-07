
// 维度枚举  OLAP维度、键值列维度、级联维度、级联维度_普通、普通维度
export const DIMENSION_TYPE = [
  { value: 'OLAP维度', label: 'OLAP维度' },
  { value: '键值列维度', label: '键值列维度' },
  { value: '级联维度', label: '级联维度' },
  { value: '级联维度_普通', label: '级联维度_普通' },
  { value: '普通维度', label: '普通维度' },
  { value: '派生维度', label: '派生维度' },
]

export const DATA_TYPE_OPTIONS = [
    { label: 'STRING', value: 'STRING' },
    { label: 'INT', value: 'INT' },
    { label: 'BIGINT', value: 'BIGINT' },
    { label: 'DOUBLE', value: 'DOUBLE' },
    { label: 'FLOAT', value: 'FLOAT' },
    { label: 'BOOLEAN', value: 'BOOLEAN' },
    { label: 'DATE', value: 'DATE' },
    { label: 'DATETIME', value: 'DATETIME' },
    { label: 'TIMESTAMP', value: 'TIMESTAMP' },
    { label: 'DECIMAL', value: 'DECIMAL' },
    { label: 'VARCHAR', value: 'VARCHAR' },
  ];

// 步骤配置
export const STEPS = [
  { title: '从业务库拉取', key: 'fetch' },
  { title: '维度推理', key: 'dimension' },
  { title: '样本补全', key: 'sample' },
];


// 数据集类型
export const DATASET_TYPE_OPTIONS = [
  { label: 'ODS', value: 'ODS' },
  { label: 'DWD', value: 'DWD' },
  { label: 'DWS', value: 'DWS' },
  { label: 'OLAP', value: 'OLAP' },
];

// 列类型
export const COLUMN_TYPE_OPTIONS = [
  { label: '维度', value: '维度' },
  { label: '度量', value: '度量' },
  { label: '普通列', value: '普通列' },
];

// 指标时间范围
export const DATE_RANGE_OPTIONS = [
  { label: '日', value: '日' },
  { label: '周', value: '周' },
  { label: '月', value: '月' },
  { label: '近30天', value: '近30天' },
];

// 编织模式
export const WEAVE_MODE_OPTIONS = [
  { label: 'FULL', value: 'FULL' },
];

export const TASK_STATUS_COLOR_MAP: Record<string, string> = {
  QUEUED: '#faad14',
  RUNNING: '#0D76FD',
  SUCCESS: '#52c41a',
  FAILED: '#ff4d4f',
  KILLED: '#999',
};