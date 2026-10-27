// All API modules supply /api/... paths. Axios needs the plugin mount root,
// including when an older deployment config names the API directory itself.
export function apiRoot(configured, mount = '/') {
  return (configured?.trim() || mount).replace(/\/+$/, '').replace(/\/api$/, '') || '/'
}
