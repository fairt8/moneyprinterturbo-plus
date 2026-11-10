import { workflow, node, trigger, sticky, ifElse, expr } from '@n8n/workflow-sdk';

const submitHook = trigger({ type: 'n8n-nodes-base.webhook', version: 2.2, config: { name: 'POST mpt-clips/submit', position: [0, 0],
  parameters: { httpMethod: 'POST', path: 'mpt-clips/submit', authentication: 'headerAuth', responseMode: 'responseNode', options: {} },
  credentials: { httpHeaderAuth: { id: '6rSUorBTBE5iSv95', name: 'MoneyPrinter Upload Token' } } }, output: [{ body: { ref: 'habits', topic: 'money habits', scenes: [{ idx: 0, narration: 'Pay yourself first.', prompt: 'A jar filling with coins, cinematic' }] } }] });
const buildJob = node({ type: 'n8n-nodes-base.code', version: 2, config: { name: 'Submit: Build MPT Job', position: [220, 0],
  parameters: { mode: 'runOnceForAllItems', jsCode: "// MoneyPrinter scene prompts -> one Drive job in the shared GPU queue (same format the GPU Bridge claims).\nconst b = $json.body || $json;\nconst bad = (error, fallback) => [{ json: { ok: false, fallback: !!fallback, error } }];\nif (String($env.MPT_GPU_CLIPS || 'on').toLowerCase() === 'off') return bad('gpu_clips_off', true);\nconst scenes = Array.isArray(b.scenes) ? b.scenes : [];\nif (!scenes.length || scenes.length > 12) return bad('scenes must contain 1-12 items');\nconst MODELS = ['wan21-diffusers', 'ltx-t2v', 'wan22-5b-diffusers', 'hy15-i2v', 'hy15-t2v'];\nconst model = MODELS.includes(b.video_model) ? b.video_model : (MODELS.includes($env.MPT_CLIP_MODEL) ? $env.MPT_CLIP_MODEL : 'wan21-diffusers');\nconst ref = String(b.ref || 'short').replace(/[^A-Za-z0-9]/g, '').slice(0, 24) || 'short';\nconst ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 12);\nconst job_id = `mpt_${ref}_${ts}_${Math.random().toString(36).slice(2, 6)}`;\nconst W = Math.min(1280, Math.max(256, Number(b.width) || 480)), H = Math.min(1280, Math.max(256, Number(b.height) || 832));\nconst ttl = Math.min(720, Math.max(30, Number(b.deadline_min) || Number($env.MPT_CLIP_TTL_MIN) || 150));\nconst sc = scenes.map((s, i) => {\n  const o = { idx: Number.isInteger(s.idx) ? s.idx : i, narration: String(s.narration || '').slice(0, 600),\n              prompt: String(s.prompt || b.topic || 'personal finance concept, cinematic, soft light').slice(0, 1500), sfx: '' };\n  const img = String(s.image_b64 || '');\n  if (img && img.length < 400000 && /^[A-Za-z0-9+/=]+$/.test(img)) o.image_b64 = img;   // JPEG first frame for image-to-video\n  return o;\n});\nif (model === 'hy15-i2v' && sc.some(s => !s.image_b64)) return bad('hy15-i2v needs image_b64 (JPEG, <300 KB) on every scene');\nconst now = Date.now();\nconst job = { job_id, channel: 'moneyprinter', format: 'mpt_clips', source: 'moneyprinterturbo',\n  topic: String(b.topic || '').slice(0, 200), title: String(b.title || '').slice(0, 200), video_model: model,\n  video: { width: W, height: H }, scenes: sc, status: 'pending', created_at: new Date(now).toISOString(),\n  expires_at: new Date(now + ttl * 60000).toISOString(), cpu_fallback: false };\nreturn [{ json: { ok: true, job_id, scenes: sc.length, video_model: model, expires_at: job.expires_at },\n  binary: { data: { data: Buffer.from(JSON.stringify(job)).toString('base64'), mimeType: 'application/json', fileName: job_id + '.json' } } }];" } }, output: [{ ok: true, job_id: 'mpt_habits_202611100900_ab12', scenes: 1, video_model: 'wan21-diffusers', expires_at: '2026-11-10T10:30:00.000Z' }] });
const jobOk = ifElse({ version: 2.2, config: { name: 'Submit: Valid?', position: [440, 0], parameters: { conditions: {
  options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
  conditions: [{ leftValue: expr('{{ $json.ok }}'), rightValue: '', operator: { type: 'boolean', operation: 'true', singleValue: true } }], combinator: 'and' } } } });
const uploadJob = node({ type: 'n8n-nodes-base.googleDrive', version: 3, config: { name: 'Submit: Queue Job in Drive', position: [660, -100],
  parameters: { resource: 'file', operation: 'upload', name: expr('{{ $json.job_id }}.json'), inputDataFieldName: 'data',
    driveId: { __rl: true, mode: 'list', value: 'My Drive' }, folderId: { __rl: true, mode: 'id', value: expr("{{ $env.LONGCAT_JOBS_FOLDER_ID || '1UZuVceKoUZoUD86POYmhVcazn0in4GAG' }}") }, options: {} },
  credentials: { googleDriveOAuth2Api: { id: 'hWl3Fg2eiftqZane', name: 'Google Drive account' } } }, output: [{ id: 'fileid123', name: 'mpt_habits_202611100900_ab12.json' }] });
const respondQueued = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Submit: Respond Queued', position: [880, -100],
  parameters: { respondWith: 'json', responseBody: expr('{{ JSON.stringify({ ok: true, status: "queued", job_id: $("Submit: Build MPT Job").first().json.job_id, job_file_id: $json.id, video_model: $("Submit: Build MPT Job").first().json.video_model, expires_at: $("Submit: Build MPT Job").first().json.expires_at }) }}'), options: { responseCode: 200 } } } });
const respondRejected = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Submit: Respond Fallback/Error', position: [660, 100],
  parameters: { respondWith: 'json', responseBody: expr('{{ JSON.stringify({ ok: false, status: $json.fallback ? "fallback" : "error", error: $json.error }) }}'), options: { responseCode: 200 } } } });

const statusHook = trigger({ type: 'n8n-nodes-base.webhook', version: 2.2, config: { name: 'POST mpt-clips/status', position: [0, 400],
  parameters: { httpMethod: 'POST', path: 'mpt-clips/status', authentication: 'headerAuth', responseMode: 'responseNode', options: {} },
  credentials: { httpHeaderAuth: { id: '6rSUorBTBE5iSv95', name: 'MoneyPrinter Upload Token' } } }, output: [{ body: { job_id: 'mpt_habits_202611100900_ab12', job_file_id: 'fileid123', cancel: false } }] });
const statusValid = ifElse({ version: 2.2, config: { name: 'Status: Valid Id?', position: [220, 400], parameters: { conditions: {
  options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
  conditions: [{ leftValue: expr("{{ /^mpt_[A-Za-z0-9_\\-]+$/.test($json.body?.job_id || '') }}"), rightValue: '', operator: { type: 'boolean', operation: 'true', singleValue: true } },
               { leftValue: expr('{{ /^[A-Za-z0-9_\\-]+$/.test($json.body?.job_file_id || "") }}'), rightValue: '', operator: { type: 'boolean', operation: 'true', singleValue: true } }], combinator: 'and' } } } });
const findManifest = node({ type: 'n8n-nodes-base.googleDrive', version: 3, config: { name: 'Status: Find Manifest', position: [440, 340], alwaysOutputData: true,
  parameters: { resource: 'fileFolder', operation: 'search', searchMethod: 'query',
    queryString: expr("name = '{{ $json.body.job_id }}.done.json' and trashed = false and '{{ $env.LONGCAT_OUTPUTS_FOLDER_ID || '1oiVxpps11_xFyhkQ6eBjklA1HhR3_XJR' }}' in parents"), returnAll: false, limit: 1, filter: {}, options: { fields: ['id', 'name'] } },
  credentials: { googleDriveOAuth2Api: { id: 'hWl3Fg2eiftqZane', name: 'Google Drive account' } } }, output: [{ id: 'manifest123', name: 'mpt_habits_202611100900_ab12.done.json' }] });
const manifestFound = ifElse({ version: 2.2, config: { name: 'Status: Finished?', position: [660, 340], parameters: { conditions: {
  options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
  conditions: [{ leftValue: expr('{{ $json.id }}'), rightValue: '', operator: { type: 'string', operation: 'exists', singleValue: true } }], combinator: 'and' } } } });
const downloadManifest = node({ type: 'n8n-nodes-base.googleDrive', version: 3, config: { name: 'Status: Download Manifest', position: [880, 240],
  parameters: { resource: 'file', operation: 'download', fileId: { __rl: true, mode: 'id', value: expr('{{ $json.id }}') }, options: {} },
  credentials: { googleDriveOAuth2Api: { id: 'hWl3Fg2eiftqZane', name: 'Google Drive account' } } }, output: [{ name: 'manifest' }] });
const parseManifest = node({ type: 'n8n-nodes-base.code', version: 2, config: { name: 'Status: Read Clips', position: [1100, 240],
  parameters: { mode: 'runOnceForAllItems', jsCode: "const m = JSON.parse((await this.helpers.getBinaryDataBuffer(0, 'data')).toString('utf8'));\nconst req = $('POST mpt-clips/status').first().json.body || {};\nif (m.job_id !== req.job_id) throw new Error('manifest job_id mismatch');\nreturn [{ json: { status: 'done', job_id: m.job_id, gpu_backend: m.gpu_backend || '', sources: m.gpu_sources || {},\n  clips: (m.clips || []).map(c => ({ name: c.name, file_id: c.file_id })) } }];" } }, output: [{ status: 'done', job_id: 'mpt_x', clips: [{ name: 'scene_000.mp4', file_id: 'f1' }] }] });
const respondDone = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Status: Respond Done', position: [1320, 240],
  parameters: { respondWith: 'json', responseBody: expr('{{ JSON.stringify($json) }}'), options: { responseCode: 200 } } } });
const downloadJob = node({ type: 'n8n-nodes-base.googleDrive', version: 3, config: { name: 'Status: Download Job', position: [880, 440],
  parameters: { resource: 'file', operation: 'download', fileId: { __rl: true, mode: 'id', value: expr('{{ $("POST mpt-clips/status").first().json.body.job_file_id }}') }, options: {} },
  credentials: { googleDriveOAuth2Api: { id: 'hWl3Fg2eiftqZane', name: 'Google Drive account' } } }, output: [{ name: 'job' }] });
const decide = node({ type: 'n8n-nodes-base.code', version: 2, config: { name: 'Status: Decide', position: [1100, 440],
  parameters: { mode: 'runOnceForAllItems', jsCode: "// Not finished yet: decide queued / rendering / fallback. Fallback cancels the job so no worker (GPU or CPU) renders it later.\nconst req = $('POST mpt-clips/status').first().json.body || {};\nlet j;\ntry { j = JSON.parse((await this.helpers.getBinaryDataBuffer(0, 'data')).toString('utf8')); }\ncatch (e) { return [{ json: { action: 'respond', status: 'fallback', reason: 'job_file_unreadable' } }]; }\nif (!j || j.job_id !== req.job_id || !/^mpt_/.test(j.job_id)) return [{ json: { action: 'respond', status: 'error', reason: 'job_mismatch' } }];\nif (j.status === 'done') return [{ json: { action: 'respond', status: 'finishing' } }];\nif (j.status === 'cancelled' || j.status === 'failed') return [{ json: { action: 'respond', status: 'fallback', reason: j.cancel_reason || j.status } }];\nlet reason = '';\nif (req.cancel) reason = 'cancelled_by_caller';\nelse if (j.expires_at && Date.now() > Date.parse(j.expires_at)) reason = 'expired';\nelse if ((j.gpu_fail_count || 0) >= Number($env.GPU_MAX_FAILS || 2)) reason = 'gpu_failed';\nif (!reason) return [{ json: { action: 'respond', status: j.claimed_at ? 'rendering' : 'queued', claimed_by: j.claimed_by || null, expires_at: j.expires_at || null } }];\nconst nj = { ...j, status: 'cancelled', cancel_reason: reason, cancelled_at: new Date().toISOString(), claimed_at: null, claimed_by: null };\nreturn [{ json: { action: 'cancel', status: 'fallback', reason, job_file_id: req.job_file_id },\n  binary: { data: { data: Buffer.from(JSON.stringify(nj)).toString('base64'), mimeType: 'application/json', fileName: j.job_id + '.json' } } }];" } }, output: [{ action: 'respond', status: 'queued' }] });
const needCancel = ifElse({ version: 2.2, config: { name: 'Status: Cancel Job?', position: [1320, 440], parameters: { conditions: {
  options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
  conditions: [{ leftValue: expr('{{ $json.action }}'), rightValue: 'cancel', operator: { type: 'string', operation: 'equals' } }], combinator: 'and' } } } });
const saveCancelled = node({ type: 'n8n-nodes-base.googleDrive', version: 3, config: { name: 'Status: Save Cancelled Job', position: [1540, 380],
  parameters: { resource: 'file', operation: 'update', fileId: { __rl: true, mode: 'id', value: expr('{{ $json.job_file_id }}') }, changeFileContent: true, inputDataFieldName: 'data', updateFields: {} },
  credentials: { googleDriveOAuth2Api: { id: 'hWl3Fg2eiftqZane', name: 'Google Drive account' } } }, output: [{ id: 'fileid123' }] });
const respondFallback = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Status: Respond Fallback', position: [1760, 380],
  parameters: { respondWith: 'json', responseBody: expr('{{ JSON.stringify({ status: "fallback", reason: $("Status: Decide").first().json.reason }) }}'), options: { responseCode: 200 } } } });
const respondPending = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Status: Respond Pending', position: [1540, 520],
  parameters: { respondWith: 'json', responseBody: expr('{{ JSON.stringify({ status: $json.status, reason: $json.reason || null, claimed_by: $json.claimed_by || null, expires_at: $json.expires_at || null }) }}'), options: { responseCode: 200 } } } });
const respondBadStatus = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Status: Respond Bad Request', position: [440, 520],
  parameters: { respondWith: 'json', responseBody: '{"status":"error","reason":"bad job_id or job_file_id"}', options: { responseCode: 400 } } } });

const clipHook = trigger({ type: 'n8n-nodes-base.webhook', version: 2.2, config: { name: 'POST mpt-clips/clip', position: [0, 800],
  parameters: { httpMethod: 'POST', path: 'mpt-clips/clip', authentication: 'headerAuth', responseMode: 'responseNode', options: {} },
  credentials: { httpHeaderAuth: { id: '6rSUorBTBE5iSv95', name: 'MoneyPrinter Upload Token' } } }, output: [{ body: { job_id: 'mpt_habits_202611100900_ab12', file_id: 'f1' } }] });
const clipValid = ifElse({ version: 2.2, config: { name: 'Clip: Valid Request?', position: [220, 800], parameters: { conditions: {
  options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
  conditions: [{ leftValue: expr("{{ /^mpt_[A-Za-z0-9_\\-]+$/.test($json.body?.job_id || '') }}"), rightValue: '', operator: { type: 'boolean', operation: 'true', singleValue: true } },
               { leftValue: expr('{{ /^[A-Za-z0-9_\\-]+$/.test($json.body?.file_id || "") }}'), rightValue: '', operator: { type: 'boolean', operation: 'true', singleValue: true } }], combinator: 'and' } } } });
const downloadClip = node({ type: 'n8n-nodes-base.googleDrive', version: 3, config: { name: 'Clip: Download from Drive', position: [440, 740],
  parameters: { resource: 'file', operation: 'download', fileId: { __rl: true, mode: 'id', value: expr('{{ $json.body.file_id }}') }, options: {} },
  credentials: { googleDriveOAuth2Api: { id: 'hWl3Fg2eiftqZane', name: 'Google Drive account' } } }, output: [{ name: 'clip' }] });
const clipCheck = node({ type: 'n8n-nodes-base.code', version: 2, config: { name: 'Clip: Check It Belongs to Job', position: [660, 740],
  parameters: { mode: 'runOnceForAllItems', jsCode: "const b = $('POST mpt-clips/clip').first().json.body || {};\nconst it = $input.first(); const name = (it.binary && it.binary.data && it.binary.data.fileName) || '';\nif (!/^mpt_[A-Za-z0-9_\\-]+$/.test(b.job_id || '') || !name.startsWith(b.job_id + '.scene_') || !/\\.mp4$/.test(name))\n  throw new Error('file does not belong to this MoneyPrinter job');\nreturn [{ json: { name }, binary: { data: it.binary.data } }];" } }, output: [{ name: 'mpt_x.scene_000.mp4' }] });
const respondClip = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Clip: Respond MP4', position: [880, 740],
  parameters: { respondWith: 'binary', responseDataSource: 'set', inputFieldName: 'data', options: { responseCode: 200 } } } });
const respondBadClip = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Clip: Respond Bad Request', position: [440, 900],
  parameters: { respondWith: 'json', responseBody: '{"status":"error","reason":"bad job_id or file_id"}', options: { responseCode: 400 } } } });

const cleanHook = trigger({ type: 'n8n-nodes-base.webhook', version: 2.2, config: { name: 'POST mpt-clips/cleanup', position: [0, 1150],
  parameters: { httpMethod: 'POST', path: 'mpt-clips/cleanup', authentication: 'headerAuth', responseMode: 'responseNode', options: {} },
  credentials: { httpHeaderAuth: { id: '6rSUorBTBE5iSv95', name: 'MoneyPrinter Upload Token' } } }, output: [{ body: { job_id: 'mpt_habits_202611100900_ab12' } }] });
const cleanValid = ifElse({ version: 2.2, config: { name: 'Cleanup: Valid Id?', position: [220, 1150], parameters: { conditions: {
  options: { caseSensitive: true, leftValue: '', typeValidation: 'loose' },
  conditions: [{ leftValue: expr("{{ /^mpt_[A-Za-z0-9_\\-]+$/.test($json.body?.job_id || '') }}"), rightValue: '', operator: { type: 'boolean', operation: 'true', singleValue: true } }], combinator: 'and' } } } });
const findJobFiles = node({ type: 'n8n-nodes-base.googleDrive', version: 3, config: { name: 'Cleanup: Find Job Files', position: [440, 1100],
  parameters: { resource: 'fileFolder', operation: 'search', searchMethod: 'query',
    queryString: expr("name contains '{{ $json.body.job_id }}.' and trashed = false and ('{{ $env.LONGCAT_JOBS_FOLDER_ID || '1UZuVceKoUZoUD86POYmhVcazn0in4GAG' }}' in parents or '{{ $env.LONGCAT_OUTPUTS_FOLDER_ID || '1oiVxpps11_xFyhkQ6eBjklA1HhR3_XJR' }}' in parents)"), returnAll: true, filter: {}, options: { fields: ['id', 'name'] } },
  credentials: { googleDriveOAuth2Api: { id: 'hWl3Fg2eiftqZane', name: 'Google Drive account' } } }, output: [{ id: 'f1', name: 'mpt_x.scene_000.mp4' }] });
const keepJobFiles = node({ type: 'n8n-nodes-base.code', version: 2, config: { name: 'Cleanup: Only This Job', position: [660, 1100],
  parameters: { mode: 'runOnceForAllItems', jsCode: "const b = $('POST mpt-clips/cleanup').first().json.body || {};\nif (!/^mpt_[A-Za-z0-9_\\-]+$/.test(b.job_id || '')) return [];\nreturn $input.all().filter(i => i.json.id && String(i.json.name || '').startsWith(b.job_id + '.'));" } }, output: [{ id: 'f1', name: 'mpt_x.scene_000.mp4' }] });
const trashFiles = node({ type: 'n8n-nodes-base.googleDrive', version: 3, config: { name: 'Cleanup: Move to Drive Bin', position: [880, 1100],
  parameters: { resource: 'file', operation: 'deleteFile', fileId: { __rl: true, mode: 'id', value: expr('{{ $json.id }}') }, options: { deletePermanently: false } },
  credentials: { googleDriveOAuth2Api: { id: 'hWl3Fg2eiftqZane', name: 'Google Drive account' } } }, output: [{ fileId: 'f1', success: true }] });
const respondCleaned = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Cleanup: Respond', position: [1100, 1100], executeOnce: true,
  parameters: { respondWith: 'json', responseBody: expr('{{ JSON.stringify({ ok: true, trashed: $input.all().length }) }}'), options: { responseCode: 200 } } } });
const respondBadClean = node({ type: 'n8n-nodes-base.respondToWebhook', version: 1.5, config: { name: 'Cleanup: Respond Bad Request', position: [440, 1250],
  parameters: { respondWith: 'json', responseBody: '{"ok":false,"reason":"bad job_id"}', options: { responseCode: 400 } } } });

const note = sticky('## MPT HunyuanVideo Clip Generator (inactive)\nMoneyPrinterTurbo -> shared GPU Drive queue -> existing GPU Bridge/Router (Kaggle 2xT4 batch, no tunnel).\nThis workflow never starts Kaggle. Jobs are prefixed mpt_ so YouTube Auto B/S2 ignore them.\nFallback: status returns "fallback" (expired, GPU failed, gpu_clips_off) and the job is cancelled so nothing renders it later; MPT then uses its FLUX images + Ken Burns.\nAuth: header token = credential "MoneyPrinter Upload Token". Env: MPT_GPU_CLIPS=on|off, MPT_CLIP_MODEL, MPT_CLIP_TTL_MIN.', [], { color: 5, position: [-40, -360], width: 900, height: 220 });

export default workflow('mpt-hunyuan-clip', 'MPT HunyuanVideo Clip Generator')
  .add(submitHook).to(buildJob).to(jobOk.onTrue(uploadJob.to(respondQueued)).onFalse(respondRejected))
  .add(statusHook).to(statusValid
    .onTrue(findManifest.to(manifestFound
      .onTrue(downloadManifest.to(parseManifest).to(respondDone))
      .onFalse(downloadJob.to(decide).to(needCancel.onTrue(saveCancelled.to(respondFallback)).onFalse(respondPending)))))
    .onFalse(respondBadStatus))
  .add(clipHook).to(clipValid.onTrue(downloadClip.to(clipCheck).to(respondClip)).onFalse(respondBadClip))
  .add(cleanHook).to(cleanValid.onTrue(findJobFiles.to(keepJobFiles).to(trashFiles).to(respondCleaned)).onFalse(respondBadClean))
  .add(note);

