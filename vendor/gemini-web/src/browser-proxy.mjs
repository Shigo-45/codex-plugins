export function browserProxyArgs(env = process.env) {
  const value = env.GEMINI_WEB_BRIDGE_PROXY?.trim();
  if (!value) return [];
  if (value === "direct") return ["--no-proxy-server"];
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error("GEMINI_WEB_BRIDGE_PROXY must be direct or an HTTP, HTTPS, SOCKS4 or SOCKS5 proxy URL.");
  }
  if (!['http:', 'https:', 'socks4:', 'socks5:'].includes(url.protocol) ||
      !url.hostname || url.username || url.password ||
      url.pathname !== '/' && url.pathname !== '' || url.search || url.hash) {
    throw new Error("GEMINI_WEB_BRIDGE_PROXY must be a proxy origin without credentials, path or query.");
  }
  return [`--proxy-server=${url.protocol}//${url.host}`];
}
