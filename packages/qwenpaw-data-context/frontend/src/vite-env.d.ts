/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Sanitized setup accounts, injected only by the local development server. */
  readonly VITE_DEV_LOGIN_ACCOUNTS?: string;
  /**
   * 登录页联调账号快填条的口令，仅本地 .env 配置、仅 dev server 生效；
   * vite.config.ts 在 `vite build` 时通过 define 强制置为空串（口令不进构建产物）。
   */
  readonly VITE_DEV_LOGIN_PASSWORD?: string;
  /**
   * 个别账号的口令覆盖，格式 `user:pass,user2:pass2`；同样仅 dev server 生效、
   * 构建时被置空。用于密码与统一口令不一致的账号。
   */
  readonly VITE_DEV_LOGIN_PASSWORD_OVERRIDES?: string;
}
