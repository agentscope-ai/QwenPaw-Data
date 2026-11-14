import { Drawer } from '@/design';
import type { ReactNode } from 'react';
import styles from '../DataProfiling.module.less';

interface StageAiDrawerProps {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
}

export default function StageAiDrawer({ open, title, onClose, children }: StageAiDrawerProps) {
  return (
    <Drawer
      placement="right"
      width={980}
      open={open}
      title={title}
      onClose={onClose}
      className={styles.stageAiDrawer}
    >
      <div className={styles.stageAiDrawerContent}>{children}</div>
    </Drawer>
  );
}
