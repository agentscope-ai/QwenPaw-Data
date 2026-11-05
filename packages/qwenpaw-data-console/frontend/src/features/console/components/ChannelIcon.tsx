/**
 * Channel brand icon — renders the official colored logo for session.channel,
 * falling back to a letter avatar on load failure.
 */
import { useState } from 'react';
import type { CSSProperties } from 'react';
import { MessageOutlined } from '@ant-design/icons';

interface ChannelIconProps {
  channel: string;
  className?: string;
  style?: CSSProperties;
  size?: number;
}

/** Official logo URL per channel. */
const CHANNEL_ICON_URLS: Record<string, string> = {
  feishu:
    'https://gw.alicdn.com/imgextra/i4/O1CN01jsn08m225euyUoaFN_!!6000000007069-2-tps-400-400.png',
  dingtalk:
    'https://gw.alicdn.com/imgextra/i4/O1CN01g1u9vB1KdEreWzDdv_!!6000000001186-2-tps-400-400.png',
  wecom:
    'https://gw.alicdn.com/imgextra/i1/O1CN01HWtzmr1hkK9beQICJ_!!6000000004315-2-tps-400-400.png',
  wechat:
    'https://gw.alicdn.com/imgextra/i4/O1CN01GsAob11fkfDWVIb3R_!!6000000004045-2-tps-400-400.png',
};

/** Letter-avatar background colors (used when the logo fails to load). */
const LETTER_COLORS: Record<string, string> = {
  feishu: '#3370FF',
  dingtalk: '#3370FF',
  wecom: '#07C160',
  wechat: '#07C160',
  console: '#FF7F16',
};

const DEFAULT_COLOR = '#7a91ad';

export default function ChannelIcon({
  channel,
  className,
  style,
  size = 22,
}: ChannelIconProps) {
  const url = CHANNEL_ICON_URLS[channel];
  const [failed, setFailed] = useState(false);

  if (url && !failed) {
    return (
      <img
        className={className}
        style={{ borderRadius: size * 0.25, objectFit: 'cover', ...style }}
        src={url}
        alt={channel}
        width={size}
        height={size}
        onError={() => setFailed(true)}
      />
    );
  }

  // Channels without a logo (e.g. console): use a generic message icon.
  if (!url) {
    return <MessageOutlined className={className} style={style} />;
  }

  // Logo failed to load: fall back to a letter avatar.
  const bg = LETTER_COLORS[channel] ?? DEFAULT_COLOR;
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
