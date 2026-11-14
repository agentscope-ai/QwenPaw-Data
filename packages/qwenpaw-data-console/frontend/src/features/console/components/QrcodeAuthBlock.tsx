/**
 * QR-code authorization block — fetch a QR code, display it, and poll until
 * the user scans & confirms; on success the returned credentials are passed
 * back via onSuccess (the parent writes them into the form).
 *
 * Ported from QwenPaw's QrcodeAuthBlock; adapted to the QwenPawData
 * useChannelQrcode hook + design-system imports.
 */
import { Button } from '@agentscope-ai/design';
import { Form, Spin } from 'antd';
import { useChannelQrcode } from './useChannelQrcode';
import type { ChannelQrcodeConfig } from './useChannelQrcode';

interface QrcodeAuthBlockProps extends ChannelQrcodeConfig {
  /** Form.Item label text */
  label: string;
  /** Button text */
  buttonText: string;
  /** Alt text for the QR code image */
  imageAlt: string;
  /** Hint text shown below the QR code */
  hintText: string;
}

export default function QrcodeAuthBlock({
  label,
  buttonText,
  imageAlt,
  hintText,
  ...qrcodeConfig
}: QrcodeAuthBlockProps) {
  const qrcode = useChannelQrcode(qrcodeConfig);

  return (
    <Form.Item label={label}>
      <Button type="primary" block loading={qrcode.loading} onClick={qrcode.fetchQrcode}>
        {buttonText}
      </Button>
      {qrcode.loading && (
        <div style={{ textAlign: 'center', marginTop: 12 }}>
          <Spin />
        </div>
      )}
      {qrcode.qrcodeImg && !qrcode.loading && (
        <div style={{ textAlign: 'center', marginTop: 12 }}>
          <img
            src={`data:image/png;base64,${qrcode.qrcodeImg}`}
            alt={imageAlt}
            style={{ width: 200, height: 200 }}
          />
          <div
            style={{
              marginTop: 8,
              fontSize: 12,
              color: 'rgba(0,0,0,0.45)',
            }}
          >
            {hintText}
          </div>
        </div>
      )}
    </Form.Item>
  );
}
