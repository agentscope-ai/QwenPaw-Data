import { Suspense } from 'react';
import { Outlet } from 'react-router-dom';
import { Spin } from 'antd';
import styles from './AuthLayout.module.css';

export default function AuthLayout() {
  return (
    <div className={styles.container}>
      <div className={styles.logoContainer}>
        <span className={styles.logoMark}>
          <img
            className={styles.logoImage}
            src={`${import.meta.env.BASE_URL}qwenpaw-data-wordmark.png?v=20260711b`}
            alt="QwenPaw-Data"
          />
          <img
            className={`${styles.logoImage} ${styles.logoImageContrast}`}
            src={`${import.meta.env.BASE_URL}qwenpaw-data-wordmark.png?v=20260711b`}
            alt=""
            aria-hidden="true"
          />
        </span>
        <span className={styles.subtitle}>Authentication & Authorization</span>
      </div>
      <div className={styles.card}>
        <Suspense
          fallback={
            <div
              style={{
                display: 'flex',
                justifyContent: 'center',
                alignItems: 'center',
                minHeight: 200,
              }}
            >
              <Spin size="large" />
            </div>
          }
        >
          <Outlet />
        </Suspense>
      </div>
    </div>
  );
}
