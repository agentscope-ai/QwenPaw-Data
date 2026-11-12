import { Drawer, type DrawerProps } from 'antd';

export default function RouteDrawer({
  mask = true,
  rootClassName,
  rootStyle,
  ...props
}: DrawerProps) {
  return (
    <Drawer
      {...props}
      getContainer={false}
      mask={mask}
      rootClassName={['qwenpaw-data-route-drawer', rootClassName].filter(Boolean).join(' ')}
      rootStyle={{
        ...rootStyle,
        position: 'absolute',
      }}
    />
  );
}
