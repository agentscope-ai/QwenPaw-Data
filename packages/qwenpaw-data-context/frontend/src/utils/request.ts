import { message } from '@/design';
import axios, { AxiosInstance, AxiosRequestConfig } from 'axios';
import { isEmbedded, resolveCmUrl } from '../embedded/context';
import { transformKeysToSnake } from '@/utils';
import {
  ApiRequestError,
  EMBEDDING_REBUILD_IN_PROGRESS,
  REBUILD_IN_PROGRESS_MESSAGE,
  SERVICE_UNAVAILABLE_MESSAGE,
  parseApiError,
} from '@/services/apiError';
import { buildAuthHeaders } from '@/services/authHeaders';
import { getConsoleConfig, getContextBasePath } from '@/services/consoleConfig';

export interface FrontendAxiosRequestConfig extends AxiosRequestConfig {
  /** Route loads keep the legacy /403 redirect; in-page actions opt out. */
  redirectOnForbidden?: boolean;
  _authRetried?: boolean;
}

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

const axiosInstance: AxiosInstance = axios.create({
  baseURL: getConsoleConfig()?.api_base_url ?? apiBaseUrl,
  timeout: 100000,
  withCredentials: true,
  headers: { 'Content-Type': 'application/json' },
});

/** Response interceptors return response.data, not AxiosResponse. */
export const request = {
  get<T = unknown>(url: string, config?: FrontendAxiosRequestConfig): Promise<T> {
    return axiosInstance.get<T, T>(url, config) as Promise<T>;
  },
  post<T = unknown>(url: string, data?: unknown, config?: FrontendAxiosRequestConfig): Promise<T> {
    return axiosInstance.post<T, T>(url, data, config) as Promise<T>;
  },
  put<T = unknown>(url: string, data?: unknown, config?: FrontendAxiosRequestConfig): Promise<T> {
    return axiosInstance.put<T, T>(url, data, config) as Promise<T>;
  },
  delete<T = unknown>(url: string, config?: FrontendAxiosRequestConfig): Promise<T> {
    return axiosInstance.delete<T, T>(url, config) as Promise<T>;
  },
  interceptors: axiosInstance.interceptors,
};

request.interceptors.request.use((config) => {
  if (isEmbedded()) {
    config.baseURL = '';
    if (config.url) config.url = resolveCmUrl(config.url);
  }
  if (config.params) config.params = transformKeysToSnake(config.params);
  for (const [name, value] of Object.entries(buildAuthHeaders())) {
    if (!config.headers.has(name)) config.headers.set(name, value);
  }

  const isFormData = typeof FormData !== 'undefined' && config.data instanceof FormData;
  if (isFormData && config.headers) {
    config.headers.delete('Content-Type');
  }

  if (config.data && (config.method === 'post' || config.method === 'put') && !isFormData) {
    config.data = transformKeysToSnake(config.data);
  }
  return config;
});

request.interceptors.response.use(
  (response) => response.data,
  async (axiosError) => {
    const response = axiosError.response;
    if (!response) {
      const error = new ApiRequestError(axiosError.message || '网络错误，请检查网络连接', {
        status: 0,
      });
      message?.error(error.message);
      return Promise.reject(error);
    }

    const status = response.status as number;
    const requestConfig = axiosError.config as FrontendAxiosRequestConfig | undefined;

    if (status === 401 && requestConfig && !requestConfig._authRetried) {
      requestConfig._authRetried = true;
      const { refreshToken } = await import('@/auth');
      const refreshed = await refreshToken();
      if (refreshed) return axiosInstance(requestConfig);
    }

    const error = parseApiError(response.data, status, response.statusText);

    if (status === 401) {
      const { buildLocalLoginUrl } = await import('@/store/authStore');
      window.location.assign(buildLocalLoginUrl());
      return Promise.reject(error);
    }

    switch (status) {
      case 400:
        message?.error(error.message || '请求参数错误');
        break;
      case 403: {
        message?.error(error.message || '没有权限访问');
        const forbiddenPath = `${getContextBasePath().replace(/\/$/, '')}/403`;
        if (
          !isEmbedded() && requestConfig?.redirectOnForbidden !== false &&
          window.location.pathname !== forbiddenPath
        ) {
          window.location.assign(forbiddenPath);
        }
        break;
      }
      case 404:
        message?.error(error.message || '请求的资源不存在');
        break;
      case 422:
        message?.error(error.message || '请求参数校验未通过');
        break;
      case 500:
        message?.error(error.message || '服务器内部错误');
        break;
      case 423:
        message?.warning(error.message || REBUILD_IN_PROGRESS_MESSAGE);
        break;
      case 503:
        if (error.code === EMBEDDING_REBUILD_IN_PROGRESS) {
          message?.warning(error.message || REBUILD_IN_PROGRESS_MESSAGE);
        } else {
          message?.error(error.message || SERVICE_UNAVAILABLE_MESSAGE);
        }
        break;
      default:
        message?.error(error.message || `请求失败，状态码: ${status}`);
    }

    return Promise.reject(error);
  },
);

export default request;
