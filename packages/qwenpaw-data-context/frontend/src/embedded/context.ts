let embedded = false;
export function configureEmbedding() {
  embedded = true;
}
export function isEmbedded() {
  return embedded;
}

/** Only CM business traffic is remapped. Auth and Host APIs retain their ownership. */
export function resolveCmUrl(path: string): string {
  if (!embedded || !path.startsWith('/')) return path;
  if (path.startsWith('/web/')) return `/cm-web/${path.slice(5)}`;
  if (
    path.startsWith('/api/') &&
    !/^\/api\/(auth|platform|users|roles|workspaces|tenants|access-requests)(\/|$)/.test(path)
  ) {
    return `/cm-api/${path.slice(5)}`;
  }
  return path;
}
