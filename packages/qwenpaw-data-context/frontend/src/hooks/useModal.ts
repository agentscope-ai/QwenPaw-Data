import React, { useState, useCallback, ComponentType } from 'react';

interface ModalProps {
  visible: boolean;
  onCancel: () => void;
}

export function useModal<T extends ModalProps>(
  ModalComponent: ComponentType<T>,
  initProps?: Partial<Omit<T, 'visible' | 'onCancel'>>
) {
  const [visible, setVisible] = useState<boolean>(false);
  const [modalProps, setModalProps] = useState<Partial<Omit<T, 'visible' | 'onCancel'>> | undefined>(initProps);
  const show = useCallback((nextModalProps?: Partial<Omit<T, 'visible' | 'onCancel'>>) => {
    setModalProps(prev => ({ ...(prev ?? {}), ...(nextModalProps ?? {}) }) as Partial<Omit<T, 'visible' | 'onCancel'>>);
    setVisible(true);
  }, []);
  const close = useCallback(() => {
    setVisible(false);
  }, []);
  const modal = visible ? React.createElement(ModalComponent, { ...(modalProps as T), visible, onCancel: close } as T) : null;
  return { modal, showModal: show };
}
