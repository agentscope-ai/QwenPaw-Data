import { Line, type LineConfig } from '@ant-design/plots';
import { Empty, theme } from 'antd';
import type { UsageTimeseries } from '@/features/console/api/usageTypes';
import { formatUsageNumber } from '@/features/console/model/usage/usagePresentation';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

type TrendPoint = { label: string; series: string; tokens: number; tooltipTitle: string };

export default function UsageTrend({ data }: { data: UsageTimeseries }) {
  const { t, language } = useAppI18n();
  const { token } = theme.useToken();
  const points: TrendPoint[] = data.points.flatMap((point) => {
    const common = {
      label: `${point.label}${point.is_partial ? '*' : ''}`,
      tooltipTitle: `${point.label}${point.is_partial ? ` · ${t('usage.partialBucket')}` : ''}`,
    };
    return [
      { ...common, series: t('usage.input'), tokens: point.generation.input_tokens },
      { ...common, series: t('usage.output'), tokens: point.generation.output_tokens },
      { ...common, series: 'Embedding', tokens: point.embedding.tokens },
    ];
  });
  if (!points.length)
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('usage.trend.empty')} />;
  const config: LineConfig = {
    data: points,
    xField: 'label',
    yField: 'tokens',
    colorField: 'series',
    height: 300,
    autoFit: true,
    animation: false,
    scale: {
      color: { range: [token.colorPrimary, token.colorWarning, token.colorSuccess] },
      y: { domainMin: 0, nice: true },
    },
    axis: {
      x: { title: false, labelFill: token.colorTextSecondary, labelAutoHide: true },
      y: {
        title: false,
        labelFill: token.colorTextSecondary,
        // G2 defaults to 10% opacity, which washes out the theme's light border color.
        grid: true,
        gridStroke: token.colorBorder,
        gridStrokeOpacity: 1,
        gridLineWidth: 1,
        gridLineDash: [4, 4],
        labelFormatter: (value: number) => formatUsageNumber(Number(value), language),
      },
    },
    legend: {
      color: {
        position: 'top',
        layout: { justifyContent: 'flex-end' },
        itemLabelFill: token.colorTextSecondary,
      },
    },
    point: { sizeField: 3, tooltip: false },
    style: { lineWidth: 2 },
    tooltip: {
      title: 'tooltipTitle',
      items: [
        (point: TrendPoint) => ({
          name: point.series,
          value: formatUsageNumber(point.tokens, language, false),
        }),
      ],
    },
    interaction: { legendFilter: true, tooltip: { series: true, crosshairs: true } },
  };
  return (
    <div>
      <div className={styles.chart} role="img" aria-label={t('usage.trend.title')}>
        <Line {...config} />
      </div>
      {data.points.some((point) => point.is_partial) ? (
        <p className={styles.note}>* {t('usage.partialBucket')}</p>
      ) : null}
    </div>
  );
}
