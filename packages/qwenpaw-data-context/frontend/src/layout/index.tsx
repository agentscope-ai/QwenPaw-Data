import { useCallback, useEffect, useMemo, useState, Suspense } from 'react';
import { CloseOutlined, LeftOutlined, MenuOutlined, RightOutlined } from '@ant-design/icons';
import {
  Outlet,
  Link,
  useLocation,
  useMatches,
  useNavigate,
  useSearchParams,
} from 'react-router-dom';
import { Button, ProLayout, Spin, InfoCircleOutlined } from '@/design';
import { dataPawLayout } from '@qwenpaw-data/ui-tokens';
import {
  asideMenuConfig,
  menuPathKeys,
  menuPaths,
  menuKeyAncestors,
  topGroupKeys,
} from '@/router/menuConfig';
import type { BreadcrumbHandle } from '@/router/index';
import { useAppI18n } from '@/i18n/useAppI18n';
import { useAuthStore } from '@/store/authStore';
import BackToHostButton from './BackToHostButton';
import SiderAccountPanel from './SiderAccountPanel';
import styles from './index.module.css';

const TOP_GROUP_KEY_SET = new Set(topGroupKeys);

const CONTEXT_SIDER_WIDTH = dataPawLayout.siderWidth;
const NARROW_LAYOUT_QUERY = '(max-width: 760px)';

function usePageHeaderMeta(): Pick<
  BreadcrumbHandle,
  'titleKey' | 'descriptionKey' | 'headerAction'
> | null {
  const matches = useMatches();
  const [searchParams] = useSearchParams();

  // 获取当前路由的 handle 配置
  const currentMatch = matches.find((match) => match.handle);
  if (!currentMatch) {
    return null;
  }

  const handle = currentMatch.handle as BreadcrumbHandle;

  // 优先使用动态标题
  if (handle.dynamicTitle) {
    const dynamicResult = handle.dynamicTitle(searchParams);
    if (dynamicResult) {
      return {
        titleKey: dynamicResult.titleKey,
        descriptionKey: handle.descriptionKey,
        headerAction: handle.headerAction,
      };
    }
  }

  return {
    titleKey: handle.titleKey,
    descriptionKey: handle.descriptionKey,
    headerAction: handle.headerAction,
  };
}

export default function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { t } = useAppI18n();
  const pageHeaderMeta = usePageHeaderMeta();
  const pageTitle = pageHeaderMeta?.titleKey ? t(pageHeaderMeta.titleKey) : '';
  const pageDescription = pageHeaderMeta?.descriptionKey ? t(pageHeaderMeta.descriptionKey) : '';
  const appTitle = t('common.appTitle');
  const [siderCollapsed, setSiderCollapsed] = useState(false);
  const authEnabled = useAuthStore((state) => state.authEnabled);
  const role = useAuthStore((state) => state.role);
  const activeWorkspaceRole = useAuthStore((state) => state.activeWorkspaceRole);
  const capabilities = useAuthStore((state) => state.capabilities);
  const effectiveRole = role === 'platform_admin' ? role : activeWorkspaceRole;

  useEffect(() => {
    const mediaQuery = window.matchMedia(NARROW_LAYOUT_QUERY);
    const handleViewportChange = (event: MediaQueryListEvent) => {
      setSiderCollapsed(event.matches);
    };

    setSiderCollapsed(mediaQuery.matches);
    mediaQuery.addEventListener('change', handleViewportChange);
    return () => mediaQuery.removeEventListener('change', handleViewportChange);
  }, []);

  const menuData = useMemo(() => {
    type MenuDataItem = (typeof asideMenuConfig)[number] & {
      name: string;
      children?: MenuDataItem[];
    };
    const translateMenu = (items: typeof asideMenuConfig): MenuDataItem[] =>
      items
        .filter((item) => {
          if (item.requiredRoles) {
            if (!authEnabled || !item.requiredRoles.includes(effectiveRole)) {
              return false;
            }
          }
          if (!authEnabled) return true;
          if (!item.requiredPermission) return true;
          return capabilities.includes(item.requiredPermission);
        })
        .map((item) => ({
          ...item,
          name: `${t(item.nameKey)}${authEnabled && effectiveRole === 'member' && item.memberReadOnly ? ` (${t('common.readOnly', { defaultValue: '只读' })})` : ''}`,
          children: item.children ? translateMenu(item.children) : undefined,
        }))
        // 空组剪枝：子项全部被权限过滤后，分组整体隐藏
        .filter((item) => !item.children || item.children.length > 0);
    return translateMenu(asideMenuConfig);
  }, [authEnabled, capabilities, effectiveRole, t]);

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

    if (pathname.startsWith('/graph-management/')) {
      return [getMenuKey('/graph-management')];
    }

    if (pathname === '/graph-center' || pathname.startsWith('/graph-center/')) {
      return [getMenuKey('/graph-center/global')];
    }

    if (pathname.startsWith('/user-center/account')) {
      return [getMenuKey('/user-center/account')];
    }

    if (pathname.startsWith('/data-profiling/')) {
      return [getMenuKey('/data-profiling')];
    }

    if (pathname === '/data-connection' || pathname.startsWith('/data-connection/')) {
      return [getMenuKey('/')];
    }

    // 默认：尝试按当前路径本身匹配
    return [getMenuKey(pathname)];
  }, [location.pathname]);

  // 受控 openKeys：默认仅展开顶层分组（二级可见），三级子组收起；
  // 路由切入某子组内页面时追加其祖先链，不回收用户手动展开/收起的状态
  const [openKeys, setOpenKeys] = useState<string[]>(topGroupKeys);

  const handleOpenChange = useCallback((keys: string[]) => {
    const secondaryOpenKeys = keys.filter((key) => !TOP_GROUP_KEY_SET.has(key));
    setOpenKeys([...topGroupKeys, ...secondaryOpenKeys]);
  }, []);

  useEffect(() => {
    const ancestors = menuKeyAncestors[selectedKeys[0]] ?? [];
    setOpenKeys((prev) => {
      const missing = ancestors.filter((key) => !prev.includes(key));
      return missing.length ? [...prev, ...missing] : prev;
    });
  }, [selectedKeys]);

  const isReadOnlyForMember = useMemo(() => {
    if (!authEnabled || effectiveRole !== 'member') return false;
    const pathname = location.pathname;
    const subtreeHasPath = (item: (typeof asideMenuConfig)[number]): boolean =>
      Boolean(
        item.path === pathname ||
        (item.path && pathname.startsWith(`${item.path}/`)) ||
        item.children?.some(subtreeHasPath),
      );
    const hasReadOnlyMatch = (items: typeof asideMenuConfig): boolean =>
      items.some((item) => {
        if (item.memberReadOnly) return subtreeHasPath(item);
        return item.children ? hasReadOnlyMatch(item.children) : false;
      });
    return hasReadOnlyMatch(asideMenuConfig);
  }, [authEnabled, effectiveRole, location.pathname]);

  return (
    <ProLayout
      selectedKeys={selectedKeys}
      openKeys={openKeys}
      onOpenChange={(keys) => handleOpenChange(Array.isArray(keys) ? (keys as string[]) : [])}
      menu={{ autoClose: false }}
      className={`${styles.layout} ${siderCollapsed ? styles.layoutSiderCollapsed : ''}`}
      logo={false}
      title={false}
      pageTitleRender={() => appTitle}
      headerRender={false}
      fixSiderbar
      breakpoint={false}
      collapsed={siderCollapsed}
      onCollapse={setSiderCollapsed}
      collapsedButtonRender={false}
      menuHeaderRender={() => (
        <div className={styles.siderHeader}>
          <button
            className={styles.mobileSiderClose}
            type="button"
            aria-label="关闭左侧菜单"
            title="关闭左侧菜单"
            onClick={() => setSiderCollapsed(true)}
          >
            <CloseOutlined />
          </button>
          <Link
            className={styles.brandHeader}
            to={effectiveRole === 'platform_admin' ? '/user-center/tenants' : '/'}
            aria-label="QwenPaw Data"
          >
            <span className={styles.brandWordmark}>
              <img
                className={styles.logoWordmark}
                src={`${import.meta.env.BASE_URL}qwenpaw-data-brand.png`}
                alt="QwenPaw-Data"
              />
              <img
                className={`${styles.logoWordmark} ${styles.logoWordmarkContrast}`}
                src={`${import.meta.env.BASE_URL}qwenpaw-data-brand.png`}
                alt=""
                aria-hidden="true"
              />
            </span>
          </Link>
          <BackToHostButton variant="sider" />
        </div>
      )}
      siderWidth={siderCollapsed ? 0.01 : CONTEXT_SIDER_WIDTH}
      location={{
        pathname: location.pathname,
      }}
      layout="side"
      contentStyle={{ background: 'transparent', margin: 0, padding: 0 }}
      token={{
        bgLayout: 'transparent',
        pageContainer: {
          colorBgPageContainer: 'transparent',
        },
        sider: {
          colorMenuBackground: 'transparent',
          colorBgMenuItemSelected: 'rgba(219, 234, 254, 0.82)',
          colorBgMenuItemHover: 'rgba(255, 255, 255, 0.72)',
          colorTextMenu: '#5D6B7D',
          colorTextMenuActive: '#0B5FC6',
          colorTextMenuSelected: '#0B5FC6',
          colorTextMenuItemHover: '#0B5FC6',
          colorTextMenuTitle: '#8A96A6',
        },
      }}
      menuDataRender={() => menuData}
      menuFooterRender={() => {
        return (
          <div className={styles.siderFooter}>
            {authEnabled && effectiveRole === 'member' ? (
              <div className={styles.memberHint}>
                <InfoCircleOutlined style={{ flexShrink: 0, marginTop: 2 }} />
                <span>{t('layout.memberHint')}</span>
              </div>
            ) : null}
            <SiderAccountPanel />
          </div>
        );
      }}
      menuItemRender={(item, defaultDom) => {
        if (!item.path) {
          return defaultDom;
        }
        return (
          <Link
            className={styles.secondaryMenuLink}
            to={item.path}
            onClick={() => {
              if (window.matchMedia(NARROW_LAYOUT_QUERY).matches) {
                setSiderCollapsed(true);
              }
            }}
          >
            {item.icon ? <span className={styles.secondaryMenuIcon}>{item.icon}</span> : null}
            <span className={styles.secondaryMenuText}>{item.name}</span>
          </Link>
        );
      }}
      subMenuItemRender={(item, defaultDom) => {
        if (!item.path) {
          return defaultDom;
        }
        return (
          <Link
            className={styles.secondaryMenuLink}
            to={item.path}
            onClick={() => {
              if (window.matchMedia(NARROW_LAYOUT_QUERY).matches) {
                setSiderCollapsed(true);
              }
            }}
          >
            {item.icon ? <span className={styles.secondaryMenuIcon}>{item.icon}</span> : null}
            <span className={styles.secondaryMenuText}>{item.name}</span>
          </Link>
        );
      }}
      actionsRender={false}
    >
      <div className={styles.pageShell}>
        <button
          className={`${styles.siderCollapseButton} ${siderCollapsed ? styles.siderCollapseButtonCollapsed : ''}`}
          type="button"
          aria-expanded={!siderCollapsed}
          aria-label={siderCollapsed ? '展开左侧菜单' : '向左收起菜单'}
          title={siderCollapsed ? '展开左侧菜单' : '向左收起菜单'}
          onClick={() => setSiderCollapsed((collapsed) => !collapsed)}
        >
          {siderCollapsed ? <RightOutlined /> : <LeftOutlined />}
        </button>
        <div className={styles.pageHeaderGlass}>
          <header className={styles.pageHeader}>
            <Button
              className={styles.mobileMenu}
              type="text"
              icon={<MenuOutlined />}
              aria-label="打开左侧菜单"
              onClick={() => setSiderCollapsed(false)}
            />
            <div className={styles.pageTitleArea}>
              {pageHeaderMeta?.headerAction ? (
                <Button
                  className={styles.pageHeaderAction}
                  type="text"
                  size="small"
                  icon={<LeftOutlined />}
                  onClick={() => navigate(pageHeaderMeta.headerAction?.path ?? '/')}
                >
                  {t(pageHeaderMeta.headerAction.labelKey)}
                </Button>
              ) : null}
              {pageTitle ? (
                <h1 className={styles.pageTitle}>
                  {pageTitle}
                  {isReadOnlyForMember ? (
                    <span className={styles.viewOnlyTag}>
                      {t('common.viewOnly', { defaultValue: '仅查看' })}
                    </span>
                  ) : null}
                </h1>
              ) : null}
              {pageDescription ? (
                <span className={styles.pageDescription} title={pageDescription}>
                  {pageDescription}
                </span>
              ) : null}
            </div>
          </header>
        </div>
        <div
          className={`${styles.pageBody} ${
            location.pathname === '/graph-center' || location.pathname.startsWith('/graph-center/')
              ? styles.pageBodyFill
              : ''
          }`}
        >
          <Suspense
            fallback={
              <div className={styles.pageLoading}>
                <Spin size="large" />
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </div>
      </div>
    </ProLayout>
  );
}
