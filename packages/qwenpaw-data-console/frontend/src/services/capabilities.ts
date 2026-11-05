import { getRuntimeConfig } from './runtimeConfig';

// Management pages stay disabled until their Host APIs are supported.
export const capabilities = getRuntimeConfig().capabilities;
