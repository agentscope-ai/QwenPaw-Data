import { message } from '@/design';
import axios, { AxiosInstance, AxiosRequestConfig } from 'axios';
import { transformKeysToSnake } from '@/utils';
import { clearAuthToken, getApiToken } from '@/services/config';

// 创建axios实例
const axiosInstance: AxiosInstance = axios.create({
  baseURL: SERVICE_BASE_URL,
  timeout: 100000,
  headers: { 'Content-Type': 'application/json' },
});

/** 响应拦截器返回 response.data，所以 request.get/post 等直接返回数据而非 AxiosResponse */
export const request = {
  get<T = unknown>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return axiosInstance.get<T, T>(url, config) as Promise<T>;
  },
  post<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return axiosInstance.post<T, T>(url, data, config) as Promise<T>;
  },
  put<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return axiosInstance.put<T, T>(url, data, config) as Promise<T>;
  },
  delete<T = unknown>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return axiosInstance.delete<T, T>(url, config) as Promise<T>;
  },
  interceptors: axiosInstance.interceptors,
};

// 请求拦截器
request.interceptors.request.use(
  (config) => {
    const token = getApiToken();
    if (token && !config.headers.has('Authorization')) {
      config.headers.set('Authorization', `Bearer ${token}`);
    }

    if (config.params) {
      config.params = transformKeysToSnake(config.params);
    }

    if (typeof FormData !== 'undefined' && config.data instanceof FormData && config.headers) {
      const headers = config.headers as { delete?: (name: string) => void } & Record<string, unknown>;
      if (typeof headers.delete === 'function') {
        headers.delete('Content-Type');
        headers.delete('content-type');
      } else {
        delete headers['Content-Type'];
        delete headers['content-type'];
      }
    }

    // 对 POST/PUT 请求体进行 camelCase → snake_case 转换
    // FormData（文件上传）等非普通对象需跳过，否则会被破坏成空对象
    if (
      config.data &&
      (config.method === 'post' || config.method === 'put') &&
      !(typeof FormData !== 'undefined' && config.data instanceof FormData)
    ) {
      config.data = transformKeysToSnake(config.data);
    }
    return config;
  },
  (error) => {
    console.error('请求错误:', error);
    return Promise.reject(error);
  }
);



// 响应拦截器
request.interceptors.response.use(
  (response) => {
    const { data, status } = response;
    if (status === 200) {
      return data;
    }
    return response;
  },
  (error) => {
    console.error('响应错误:', error);
    if (error.response) {
      const { status } = error.response;
      const data = error.response?.data;
      const serverMessage = data?.detail || data?.message || error.message;
      console.error('响应状态码:', error);
      switch (status) {
        case 401:
          clearAuthToken();
          message?.error(serverMessage || '认证已失效，请重新输入访问令牌');
          break;
        case 400:
          message?.error(serverMessage || '请求参数错误');
          break;
        case 403:
          message?.error(serverMessage || '没有权限访问');
          break;
        case 404:
          message?.error(serverMessage || '请求的资源不存在');
          break;
        case 500:
          message?.error(serverMessage || '服务器内部错误');
          break;
        default:
          message?.error(serverMessage || `请求失败，状态码: ${status}`);
      }
    } else if (error.request) {
      message?.error(error.message || '网络错误，请检查网络连接');
    } else {
      message?.error('请求配置错误');
    }

    return Promise.reject(error);
  }
);

export default request;
