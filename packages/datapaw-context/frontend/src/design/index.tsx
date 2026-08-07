/* eslint-disable react-refresh/only-export-components -- public design-system barrel */
import { createContext, forwardRef, useContext, useMemo } from "react";
import type { ComponentProps, CSSProperties, HTMLAttributes, JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  Empty as AgentscopeEmpty,
  getCommonConfig,
  Spinner,
  Table as AgentscopeTable,
  Upload as AgentscopeUpload,
} from "@agentscope-ai/design";
import {
  EditableProTable as AntEditableProTable,
  ProTable as AntProTable,
  type EditableProTableProps,
  type ProTableProps,
} from "@ant-design/pro-components";
import {
  Tooltip as AntTooltip,
  type GetRef,
  type TooltipProps as AntTooltipProps,
} from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import type { TableProps } from "antd";
import classNames from "classnames";

export * from "@agentscope-ai/design";
export * from "@ant-design/icons";
// 上游设计体系的公开中性别名，统一以 baseTheme 对内暴露。
export { purpleTheme as baseTheme } from "@agentscope-ai/design";
export {
  PageContainer,
  ProLayout,
  type ActionType,
  type EditableFormInstance,
  type ProColumns,
  type ProFormInstance,
} from "@ant-design/pro-components";
export type { EditableProTableProps, ProTableProps } from "@ant-design/pro-components";
export type { TableProps } from "antd";
export type UploadProps = ComponentProps<typeof AgentscopeUpload>;

type SparkTooltipProps = AntTooltipProps & {
  mode?: "dark" | "light";
  maxHeight?: number | string;
};

export const Tooltip = forwardRef<GetRef<typeof AntTooltip>, SparkTooltipProps>(function Tooltip(
  {
    mode = "dark",
    maxHeight = "90vh",
    styles,
    classNames: tooltipClassNames,
    arrow = false,
    getPopupContainer,
    overlayClassName,
    ...rest
  },
  ref,
) {
  const { antPrefix, sparkPrefix } = getCommonConfig();
  const rootClassName = classNames(
    tooltipClassNames?.root,
    overlayClassName,
    mode === "light" && `${sparkPrefix}-tooltip-light`,
  );

  return (
    <AntTooltip
      {...rest}
      ref={ref}
      arrow={arrow}
      styles={{
        ...styles,
        body: {
          maxHeight,
          overflow: "auto",
          ...styles?.body,
        },
      }}
      classNames={{
        ...tooltipClassNames,
        root: rootClassName,
      }}
      getPopupContainer={
        getPopupContainer ??
        ((triggerNode) => triggerNode.closest(`.${antPrefix}-app`) ?? document.body)
      }
    />
  );
});

type AccessibleFormLabelProps = {
  label: string;
  description: string;
};

export function AccessibleFormLabel({ label, description }: AccessibleFormLabelProps) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <span>{label}</span>
      <Tooltip title={description} trigger={["hover", "focus"]}>
        <button
          type="button"
          aria-label={`${label}: ${description}`}
          onClick={(event) => event.preventDefault()}
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 14,
            height: 14,
            padding: 0,
            color: "rgba(0, 0, 0, 0.45)",
            fontSize: 14,
            lineHeight: 1,
            background: "transparent",
            border: 0,
            cursor: "help",
          }}
        >
          <QuestionCircleOutlined aria-hidden="true" />
        </button>
      </Tooltip>
    </span>
  );
}

type FixedColumn = {
  hidden?: boolean;
  hideInTable?: boolean;
  title?: unknown;
  fixed?: unknown;
};

function hasHorizontalScroll(scroll: unknown): boolean {
  return (
    typeof scroll === "object" &&
    scroll !== null &&
    "x" in scroll &&
    (scroll as { x?: unknown }).x !== undefined &&
    (scroll as { x?: unknown }).x !== false
  );
}

function isVisibleTableColumn(column: FixedColumn): boolean {
  return column.hidden !== true && column.hideInTable !== true;
}

function getColumnTitleText(title: unknown): string {
  if (typeof title === "string" || typeof title === "number") {
    return String(title).trim();
  }
  return "";
}

function isMeaninglessIdColumn(column: FixedColumn): boolean {
  const title = getColumnTitleText(column.title);
  return title.toLowerCase() === "id" || title.endsWith("ID") || /\sid$/i.test(title);
}

function withFixedEdgeColumns<Column extends FixedColumn>(
  columns: readonly Column[] | undefined,
  scroll: unknown,
): Column[] | undefined {
  if (!columns) {
    return undefined;
  }

  const columnsWithoutId = columns.filter((column) => !isMeaninglessIdColumn(column));

  if (columnsWithoutId.length < 2 || !hasHorizontalScroll(scroll)) {
    return [...columnsWithoutId];
  }

  const visibleIndexes = columnsWithoutId
    .map((column, index) => (isVisibleTableColumn(column) ? index : -1))
    .filter((index) => index >= 0);

  if (visibleIndexes.length < 2) {
    return [...columnsWithoutId];
  }

  const firstIndex = visibleIndexes[0];
  const lastIndex = visibleIndexes[visibleIndexes.length - 1];

  return columnsWithoutId.map((column, index) => {
    if (index === firstIndex && column.fixed == null) {
      return { ...column, fixed: "left" };
    }
    if (index === lastIndex && column.fixed == null) {
      return { ...column, fixed: "right" };
    }
    return column;
  });
}

function useFixedEdgeColumns<Column extends FixedColumn>(
  columns: readonly Column[] | undefined,
  scroll: unknown,
): Column[] | undefined {
  return useMemo(() => withFixedEdgeColumns(columns, scroll), [columns, scroll]);
}

export function Table<RecordType = unknown>(props: TableProps<RecordType>) {
  const columns = useFixedEdgeColumns(
    props.columns as readonly FixedColumn[] | undefined,
    props.scroll,
  );

  return (
    <AgentscopeTable<RecordType>
      {...props}
      columns={columns as TableProps<RecordType>["columns"]}
    />
  );
}

export function ProTable<
  DataSource extends object,
  Params extends object = Record<string, unknown>,
  ValueType = "text",
>(props: ProTableProps<DataSource, Params, ValueType>) {
  const { t } = useTranslation();
  const columns = useFixedEdgeColumns(
    props.columns as readonly FixedColumn[] | undefined,
    props.scroll,
  );
  const search =
    props.search === false
      ? false
      : {
          defaultCollapsed: false,
          searchText: t("common.search"),
          resetText: t("common.reset"),
          ...(typeof props.search === "object" ? props.search : {}),
        };

  return (
    <AntProTable<DataSource, Params, ValueType>
      {...props}
      columns={columns as ProTableProps<DataSource, Params, ValueType>["columns"]}
      search={search}
    />
  );
}

export function EditableProTable<
  // 上游 EditableProTableProps 泛型约束即为 Record<string, any>，无法收窄
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  DataSource extends Record<string, any>,
  Params extends object = Record<string, unknown>,
  ValueType = "text",
>(props: EditableProTableProps<DataSource, Params, ValueType>) {
  const columns = useFixedEdgeColumns(
    props.columns as readonly FixedColumn[] | undefined,
    props.scroll,
  );

  return (
    <AntEditableProTable<DataSource, Params, ValueType>
      {...props}
      columns={columns as EditableProTableProps<DataSource, Params, ValueType>["columns"]}
    />
  );
}

type Size = "small" | "middle" | "large" | number;

function resolveGap(size: Size | [Size, Size] | undefined): string {
  const map = { small: 8, middle: 16, large: 24 } as const;
  const value = Array.isArray(size) ? size[0] : size ?? "small";
  return `${typeof value === "number" ? value : map[value]}px`;
}

export function Space({
  children,
  direction = "horizontal",
  size = "small",
  align,
  wrap,
  style,
  ...rest
}: HTMLAttributes<HTMLDivElement> & {
  direction?: "horizontal" | "vertical";
  size?: Size | [Size, Size];
  align?: CSSProperties["alignItems"];
  wrap?: boolean;
}) {
  return (
    <div
      {...rest}
      style={{
        display: "flex",
        flexDirection: direction === "vertical" ? "column" : "row",
        alignItems: align,
        flexWrap: wrap ? "wrap" : undefined,
        gap: resolveGap(size),
        ...style,
      }}
    >
      {children}
    </div>
  );
}

const RowColumnGapContext = createContext(0);

export function Row({
  children,
  gutter,
  align,
  style,
  ...rest
}: HTMLAttributes<HTMLDivElement> & {
  gutter?: number | [number, number];
  align?: CSSProperties["alignItems"];
}) {
  const columnGap = Array.isArray(gutter) ? gutter[0] : gutter ?? 0;
  const rowGap = Array.isArray(gutter) ? gutter[1] : gutter ?? 0;
  return (
    <RowColumnGapContext.Provider value={columnGap}>
      <div
        {...rest}
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: align,
          columnGap,
          rowGap,
          ...style,
        }}
      >
        {children}
      </div>
    </RowColumnGapContext.Provider>
  );
}

export function Col({
  children,
  span,
  flex,
  style,
  ...rest
}: HTMLAttributes<HTMLDivElement> & {
  span?: number;
  flex?: CSSProperties["flex"];
  xs?: number;
  sm?: number;
  md?: number;
  lg?: number;
}) {
  const columnGap = useContext(RowColumnGapContext);
  const spanRatio = span ? span / 24 : 0;
  // Percentage columns plus a flex gap would otherwise overflow and wrap.
  const width = span
    ? columnGap > 0
      ? `calc(${spanRatio * 100}% - ${(1 - spanRatio) * columnGap}px)`
      : `${spanRatio * 100}%`
    : undefined;
  return (
    <div
      {...rest}
      style={{
        flex: flex ?? (width ? `0 0 ${width}` : "1 1 0"),
        maxWidth: width,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

function Title({
  children,
  level = 1,
  style,
}: {
  children?: ReactNode;
  level?: 1 | 2 | 3 | 4 | 5;
  style?: CSSProperties;
}) {
  const Tag: keyof JSX.IntrinsicElements = `h${level}`;
  return <Tag style={style}>{children}</Tag>;
}

function Text({
  children,
  type,
  ellipsis,
  strong,
  code,
  className,
  style,
}: {
  children?: ReactNode;
  type?: "secondary" | "success" | "warning" | "danger";
  ellipsis?: boolean;
  strong?: boolean;
  code?: boolean;
  className?: string;
  style?: CSSProperties;
}) {
  const colorMap: Record<string, string> = {
    secondary: "rgba(0, 0, 0, 0.45)",
    success: "#389e0d",
    warning: "#d48806",
    danger: "#cf1322",
  };
  const Tag = code ? "code" : "span";
  return (
    <Tag
      className={className}
      title={typeof children === "string" && ellipsis ? children : undefined}
      style={{
        color: type ? colorMap[type] : undefined,
        fontWeight: strong ? 600 : undefined,
        overflow: ellipsis ? "hidden" : undefined,
        textOverflow: ellipsis ? "ellipsis" : undefined,
        whiteSpace: ellipsis ? "nowrap" : undefined,
        ...style,
      }}
    >
      {children}
    </Tag>
  );
}

export const Typography = { Text, Title };

export function Divider({ style }: { style?: CSSProperties }) {
  return (
    <div
      style={{
        borderTop: "1px solid rgba(5, 5, 5, 0.06)",
        margin: "16px 0",
        ...style,
      }}
    />
  );
}

export function Spin({
  children,
  spinning = true,
  tip,
}: {
  children?: ReactNode;
  spinning?: boolean;
  tip?: ReactNode;
  size?: "small" | "default" | "large";
}) {
  if (children) {
    return (
      <div style={{ position: "relative" }}>
        {spinning ? (
          <div
            style={{
              position: "absolute",
              inset: 0,
              zIndex: 1,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              gap: 8,
              background: "rgba(255, 255, 255, 0.6)",
            }}
          >
            <Spinner />
            {tip ? <span>{tip}</span> : null}
          </div>
        ) : null}
        {children}
      </div>
    );
  }
  return spinning ? <Spinner /> : null;
}

export const Empty = Object.assign(AgentscopeEmpty, {
  // AgentscopeEmpty treats `image` as a URL; undefined keeps its default illustration.
  PRESENTED_IMAGE_SIMPLE: undefined,
});

export function Result({
  title,
  subTitle,
  extra,
}: {
  status?: string;
  title?: ReactNode;
  subTitle?: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <div
      style={{
        minHeight: 280,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 16,
        textAlign: "center",
      }}
    >
      {title ? <h2 style={{ margin: 0 }}>{title}</h2> : null}
      {subTitle ? (
        <div style={{ color: "rgba(0, 0, 0, 0.45)" }}>{subTitle}</div>
      ) : null}
      {extra ? <div>{extra}</div> : null}
    </div>
  );
}
