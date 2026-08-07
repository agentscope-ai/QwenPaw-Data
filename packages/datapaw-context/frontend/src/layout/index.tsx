import { useMemo, Suspense } from 'react';
import { Outlet, Link, useLocation, useMatches, useSearchParams } from 'react-router';
import { ProLayout } from '@/design';
import { Spin } from '@/design';
import { asideMenuConfig, menuPathKeys, menuPaths } from '@/router/menuConfig';
import type { BreadcrumbHandle } from '@/router/index';
import { useAppI18n } from '@/i18n/useAppI18n';
import { BRAND_PRIMARY, BRAND_PRIMARY_ACTIVE, BRAND_PRIMARY_BG } from '@/brand';
import LanguageSwitcher from './LanguageSwitcher';
import ModelConfigButton from './ModelConfigButton';
import styles from './index.module.css';

const LAYOUT_BACKGROUND = '#f9fafd';
const APP_TITLE = '数据语义管理';

function usePageTitleKey(): string {
  const matches = useMatches();
  const [searchParams] = useSearchParams();

  // 获取当前路由的 handle 配置
  const currentMatch = matches.find((match) => match.handle);
  if (!currentMatch) {
    return '';
  }

  const handle = currentMatch.handle as BreadcrumbHandle;

  // 优先使用动态标题
  if (handle.dynamicTitle) {
    const dynamicResult = handle.dynamicTitle(searchParams);
    if (dynamicResult) {
      return dynamicResult.titleKey;
    }
  }

  return handle.titleKey;
}

export default function Layout() {
  const location = useLocation();
  const { t, language } = useAppI18n();
  const pageTitleKey = usePageTitleKey();
  const pageTitle = pageTitleKey ? t(pageTitleKey) : '';
  const siderWidth = language === 'en' ? 320 : 260;

  const menuData = useMemo(() => {
    type MenuDataItem = (typeof asideMenuConfig)[number] & {
      name: string;
      children?: MenuDataItem[];
    };
    const translateMenu = (items: typeof asideMenuConfig): MenuDataItem[] =>
      items.map((item) => ({
        ...item,
        name: t(item.nameKey),
        children: item.children ? translateMenu(item.children) : undefined,
      }));
    return translateMenu(asideMenuConfig);
  }, [t]);

  // 精确匹配 selectedKeys，修复前缀匹配导致多个菜单项同时高亮的问题
  const selectedKeys = useMemo(() => {
    const pathname = location.pathname;
    const getMenuKey = (path: string) => menuPathKeys[path] || path;

    // 精确匹配（路径列表从 menuConfig 派生，避免硬编码）
    if (menuPaths.includes(pathname)) {
      return [getMenuKey(pathname)];
    }

    // 子页面匹配到父级菜单
    if (pathname.startsWith('/metric-lib/')) {
      return [getMenuKey('/metric-lib')];
    }

    if (pathname === '/data-connection' || pathname.startsWith('/data-connection/')) {
      return [getMenuKey('/data-source')];
    }

    // 默认：尝试按当前路径本身匹配
    return [getMenuKey(pathname)];
  }, [location.pathname]);

  return (
    <ProLayout
      selectedKeys={selectedKeys}
      menu={{ defaultOpenAll: true, autoClose: false }}
      defaultCollapsed={false}
      className={styles.layout}
      logo={false}
      title={false}
      pageTitleRender={() => APP_TITLE}
      headerTitleRender={() => (
        <div className={styles.brandHeader}>
          <img
            className={styles.logoWordmark}
            src="/qwenpaw-data-wordmark.png?v=20260711b"
            alt="QwenPaw-Data"
          />
        </div>
      )}
      siderWidth={siderWidth}
      location={{
        pathname: location.pathname,
      }}
      layout="mix"
      contentStyle={{ background: LAYOUT_BACKGROUND }}
      token={{
        bgLayout: LAYOUT_BACKGROUND,
        pageContainer: {
          colorBgPageContainer: LAYOUT_BACKGROUND,
        },
        sider: {
          colorTextMenuSelected: BRAND_PRIMARY_ACTIVE,
          colorBgMenuItemSelected: BRAND_PRIMARY_BG,
          colorTextMenuItemHover: BRAND_PRIMARY,
          colorBgMenuItemHover: BRAND_PRIMARY_BG,
          colorTextMenu: 'rgba(0, 0, 0, 0.88)',
          colorTextMenuActive: BRAND_PRIMARY_ACTIVE,
        },
      }}
      menuDataRender={() => menuData}
      menuItemRender={(item, defaultDom) => {
        if (!item.path) {
          return defaultDom;
        }
        return <Link to={item.path}>{defaultDom}</Link>;
      }}
      subMenuItemRender={(item, defaultDom) => {
        if (!item.path) {
          return defaultDom;
        }
        return <Link to={item.path}>{defaultDom}</Link>;
      }}
      headerContentRender={() => <></>}
      actionsRender={() => [
        <ModelConfigButton key="model-config" />,
        <LanguageSwitcher key="language" />,
      ]}
    >
      {pageTitle ? <h1 className={styles.pageTitle}>{pageTitle}</h1> : null}
      <Suspense fallback={<div className={styles.pageLoading}><Spin size="large" /></div>}>
        <Outlet />
      </Suspense>
    </ProLayout>
  );
}
