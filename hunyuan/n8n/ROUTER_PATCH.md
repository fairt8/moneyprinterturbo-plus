# Proposed change to the YouTube Auto "GPU Router" workflow (for YouTube Auto / the owner to apply)

**Why:** MoneyPrinter jobs (`job_id` starts with `mpt_`) carry `cpu_fallback: false` and an `expires_at` time.
MoneyPrinter already has its own fallback (FLUX stills + Ken Burns). Without this change, the Router's CPU fallback
would also render MoneyPrinter jobs on the n8n server's CPU when Kaggle is unavailable, which wastes CPU for clips
nobody will use. With the change, expired jobs no longer count as GPU work either, so they don't start Kaggle.

Nothing changes for YouTube Auto's own jobs, because they have no `cpu_fallback` or `expires_at` field.

## Node "Assess Queue" (Code): replace the last four lines with

```js
const now = Date.now(), maxFails = Number($env.GPU_MAX_FAILS || 2), ttl = Number($env.GPU_CLAIM_TTL_MIN || 180) * 60000;
const after = Number($env.GPU_FALLBACK_AFTER_MIN || 240) * 60000;
const live = j => !(j.expires_at && now > Date.parse(j.expires_at));          // MoneyPrinter jobs expire
const pending = out.map(o => o.job).filter(j => j.status === 'pending' && live(j));
const free = pending.filter(j => !j.claimed_at || now - Date.parse(j.claimed_at) > ttl);
const cpuOk = free.filter(j => j.cpu_fallback !== false);                       // MoneyPrinter falls back on its own
const cpu = cpuOk.filter(j => (j.gpu_fail_count || 0) >= maxFails || now - Date.parse(j.created_at || 0) > after).map(j => j.job_id);
return [{ json: { pending: pending.length, free: free.length, gpu_wanted: free.some(j => !cpu.includes(j.job_id) && (j.gpu_fail_count || 0) < maxFails),
  cpu_jobs: cpu, all_free: cpuOk.map(j => j.job_id) } }];
```

`Pick Fallback Job` needs no change, because `all_free` now leaves out MoneyPrinter jobs.
Apply it in the n8n editor (open workflow → node → paste → Save), or have YouTube Auto apply it. Back up the workflow first.
