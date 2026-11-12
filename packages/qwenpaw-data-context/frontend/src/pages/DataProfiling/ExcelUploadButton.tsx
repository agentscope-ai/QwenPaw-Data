import { Button, Modal, message } from '@/design';
import type { ButtonProps } from 'antd';
import { useRef } from 'react';
import { useTranslation } from 'react-i18next';

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

interface ExcelUploadButtonProps extends Omit<ButtonProps, 'onClick'> {
  onUpload: (file: File) => Promise<void> | void;
}

export default function ExcelUploadButton({
  onUpload,
  loading,
  children,
  ...buttonProps
}: ExcelUploadButtonProps) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);

  const validateAndConfirm = (file: File) => {
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      message.error(t('dataProfiling.upload.xlsxOnly'));
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      message.error(t('dataProfiling.upload.maxSize'));
      return;
    }
    Modal.confirm({
      title: t('dataProfiling.upload.confirmTitle'),
      content: t('dataProfiling.upload.confirmContent'),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      onOk: () => onUpload(file),
    });
  };

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = '';
          if (file) validateAndConfirm(file);
        }}
      />
      <Button {...buttonProps} loading={loading} onClick={() => inputRef.current?.click()}>
        {children}
      </Button>
    </>
  );
}
