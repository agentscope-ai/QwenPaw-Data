/**
 * Render a local letter avatar for known channels without external requests.
 */
import type { CSSProperties } from 'react';
import { MessageTwoTone } from '@ant-design/icons';

interface ChannelIconProps {
  channel: string;
  className?: string;
  style?: CSSProperties;
  size?: number;
}

/** Letter-avatar background colors. */
const LETTER_COLORS: Record<string, string> = {
  feishu: '#3370FF',
  dingtalk: '#3370FF',
  wecom: '#07C160',
  wechat: '#07C160',
  console: '#FF7F16',
};

export default function ChannelIcon({ channel, className, style, size = 22 }: ChannelIconProps) {
  const bg = Object.prototype.hasOwnProperty.call(LETTER_COLORS, channel)
    ? LETTER_COLORS[channel]
    : undefined;
  if (!bg || channel === 'console') {
    return <MessageTwoTone className={className} style={style} />;
  }

  return (
    <div
      className={className}
      style={{
        width: size,
        height: size,
        borderRadius: size * 0.25,
        backgroundColor: bg,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: '#fff',
        fontSize: size * 0.45,
        fontWeight: 600,
        userSelect: 'none',
        ...style,
      }}
      title={channel}
    >
      {channel.charAt(0).toUpperCase()}
    </div>
  );
}
