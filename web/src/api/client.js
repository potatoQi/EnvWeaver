let activePool = null;
export function setPool(id) {
  activePool = id;
}

export async function api(
  path,
  { method = "GET", body, raw = false, signal } = {},
) {
  const url = new URL(`/api${path}`, location.origin);
  if (activePool && !url.searchParams.has("pool_id"))
    url.searchParams.set("pool_id", activePool);
  if (body?.selection && !body.selection.pool_id)
    body = {
      ...body,
      selection: { ...body.selection, pool_id: activePool },
    };
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    method,
    signal,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      data = { detail: response.statusText };
    }
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  }
  return raw ? response.blob() : response.json();
}
