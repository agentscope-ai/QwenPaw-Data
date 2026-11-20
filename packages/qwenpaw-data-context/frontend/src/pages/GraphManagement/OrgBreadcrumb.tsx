import { Fragment } from 'react';
import { Link } from 'react-router-dom';
import { ApartmentOutlined } from '@/design';
import styles from './index.module.less';

export interface OrgCrumb {
  /** Display label for this organization level. */
  label: string;
  /** Route to navigate back to; omit for the current (last) level. */
  path?: string;
}

/** Top-left organization breadcrumb: tenant / workspace / member. */
export default function OrgBreadcrumb({ items }: { items: OrgCrumb[] }) {
  if (items.length === 0) return null;
  return (
    <nav className={styles.orgBreadcrumb} aria-label="organization">
      <ApartmentOutlined className={styles.orgBreadcrumbIcon} />
      {items.map((item, index) => {
        const isLast = index === items.length - 1;
        return (
          <Fragment key={`${item.label}-${index}`}>
            {index > 0 && <span className={styles.orgBreadcrumbSeparator}>/</span>}
            {item.path && !isLast ? (
              <Link to={item.path} className={styles.orgBreadcrumbLink}>
                {item.label}
              </Link>
            ) : (
              <span className={isLast ? styles.orgBreadcrumbCurrent : styles.orgBreadcrumbText}>
                {item.label}
              </span>
            )}
          </Fragment>
        );
      })}
    </nav>
  );
}
