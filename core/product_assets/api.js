export class ApiError extends Error {
  constructor(code, message, options = {}) {
    super(message || "请求失败");
    this.name = "ApiError";
    this.code = code || "request_failed";
    this.status = options.status || 0;
    this.retryable = Boolean(options.retryable);
    this.detail = options.detail || "";
  }
}

export async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("network_error", "无法连接本机记忆服务", { retryable: true });
  }
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new ApiError("invalid_response", "服务返回了无法读取的响应", {
      status: response.status,
      retryable: response.status >= 500,
    });
  }
  if (!response.ok) {
    const error = payload?.error || {};
    throw new ApiError(error.code, error.message, {
      status: response.status,
      retryable: error.retryable,
      detail: error.detail,
    });
  }
  return payload;
}
