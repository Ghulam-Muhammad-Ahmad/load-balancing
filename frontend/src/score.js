// Local comparison rubric, not an industry benchmark. Keep targets fixed between runs.
export function scoreLoadTest(result) {
  const clamp = (value) => Math.max(0, Math.min(1, value));
  const success = clamp(1 - result.error_rate / 100);
  // Offered load is fixed by the target rate, so throughput scores how much of the
  // offered rate was actually sustained, not the raw number.
  const throughput = 30 * clamp(result.rps / result.target_rps);
  const latencyQuality = (key, target) => clamp(target / Math.max(target, result.latency_ms[key]));
  const latency = 40 * success * (
    .2 * latencyQuality("p50", 50) +
    .3 * latencyQuality("p95", 150) +
    .5 * latencyQuality("p99", 300)
  );
  const reliability = 30 * clamp(1 - result.error_rate / 5);
  return {
    total: result.requests > 0 ? Math.round(throughput + latency + reliability) : 0,
    throughput: Math.round(throughput),
    latency: Math.round(latency),
    reliability: Math.round(reliability),
  };
}
