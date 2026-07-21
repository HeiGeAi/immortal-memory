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

export async function mutate(path, body, options = {}) {
  const requestId = crypto.randomUUID();
  return api(path, {
    method: "POST",
    signal: options.signal,
    headers: {
      "Content-Type": "application/json",
      "X-Immortal-Request-Id": requestId,
      "Idempotency-Key": requestId,
      "If-Match": String(body.expected_version ?? ""),
      ...(options.headers || {}),
    },
    body: JSON.stringify(body),
  });
}

export function explainApiError(error) {
  const labels = {
    version_conflict: "数据已经变化，请刷新后重新确认。",
    stale_preview: "这次预览已经过期或来源发生变化，请重新预览。",
    invalid_transition: "当前状态不允许这项操作，页面将重新读取最新状态。",
    idempotency_conflict: "同一次提交的内容发生了变化，请重新发起操作。",
    derived_update_pending: "纠正已经保存，自我档案仍在生成，请稍后刷新。",
  };
  return labels[error?.code] || error?.message || "操作失败，请稍后重试。";
}
