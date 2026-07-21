export function formatTimestamp(value, options = {}) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const formatOptions = {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
    timeZoneName: "short",
  };
  if (options.timeZone) formatOptions.timeZone = options.timeZone;
  return new Intl.DateTimeFormat(options.locale || "zh-CN", formatOptions).format(date);
}
