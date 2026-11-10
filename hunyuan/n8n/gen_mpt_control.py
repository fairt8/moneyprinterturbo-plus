"""SDK code for 'MPT Video App Control' (n8n Form, INACTIVE). Manual buttons for a non-developer:
status+quota, run GPU batch now (idempotent 'ensure'), HunyuanVideo selftest (refuses while the batch kernel runs),
fetch selftest result, queue a test clip. Runs only on the n8n server (scripts at /data/yt-scripts/gpu)."""
import json
J = json.dumps
JOBS = "{{ $env.LONGCAT_JOBS_FOLDER_ID || '1UZuVceKoUZoUD86POYmhVcazn0in4GAG' }}"
K = "cd /data/yt-scripts/gpu/kaggle && "
CMD_STATUS = K + "echo QUOTA; python3 kaggle_launch.py quota; echo BATCH_KERNEL; python3 kaggle_launch.py status --mode drive; echo SELFTEST_KERNEL; python3 kaggle_launch.py status --mode selftest"
CMD_RUN = K + "python3 kaggle_launch.py ensure --mode drive"
CMD_SELF = (K + "S=$(python3 kaggle_launch.py status --mode drive); echo \"$S\"; "
            "if echo \"$S\" | grep -Eq '\"state\": \"(running|queued)\"'; then echo 'BUSY: the batch kernel is running, try again when it finishes'; "
            "else KAGGLE_VIDEO_MODEL=\"${MPT_SELFTEST_MODEL:-hy15-i2v}\" KAGGLE_PARALLEL=1 python3 kaggle_launch.py ensure --mode selftest; fi")
CMD_FETCH = K + "rm -rf /tmp/mpt_selftest && python3 kaggle_launch.py fetch --mode selftest -o /tmp/mpt_selftest; cat /tmp/mpt_selftest/*.json 2>/dev/null; rm -rf /tmp/mpt_selftest"
QUEUE = r"""const f = $('Video App Control Form').first().json;
const prompt = String(f['Test prompt'] || 'A golden coin spinning slowly on a dark wooden desk, soft rim light, cinematic, vertical framing').slice(0, 1500);
const model = ['wan21-diffusers', 'ltx-t2v', 'wan22-5b-diffusers', 'hy15-t2v'].includes(f['Model']) ? f['Model'] : 'wan21-diffusers';
const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 12);
const job_id = `mpt_manual_${ts}_${Math.random().toString(36).slice(2, 6)}`;
const now = Date.now();
const job = { job_id, channel: 'moneyprinter', format: 'mpt_clips', source: 'manual-form', topic: 'manual test', title: 'manual test',
  video_model: model, video: { width: 480, height: 832 }, scenes: [{ idx: 0, narration: 'test clip', prompt, sfx: '' }],
  status: 'pending', created_at: new Date(now).toISOString(), expires_at: new Date(now + 720 * 60000).toISOString(), cpu_fallback: false };
return [{ json: { job_id, model }, binary: { data: { data: Buffer.from(JSON.stringify(job)).toString('base64'), mimeType: 'application/json', fileName: job_id + '.json' } } }];"""

def rule(i, label):
    return ("{ outputKey: %s, conditions: { options: { caseSensitive: true, leftValue: '', typeValidation: 'strict' }, "
            "conditions: [{ leftValue: expr('{{ $json.Action }}'), operator: { type: 'string', operation: 'equals' }, rightValue: %s }], combinator: 'and' } }") % (J("a%d" % i), J(label))
ACTIONS = ["Status and quota", "Run GPU batch now", "Run HunyuanVideo selftest", "Get selftest result", "Queue a test clip"]
def cmd(var, name, c, y):
    return f"const {var} = node({{ type: 'n8n-nodes-base.executeCommand', version: 1, config: {{ name: {J(name)}, position: [560, {y}], onError: 'continueRegularOutput', parameters: {{ command: {J(c + ' 2>&1 | tail -n 40')} }} }}, output: [{{ stdout: '{{\"ok\": true}}', exitCode: 0 }}] }});"
def done(var, title, y, msg=None):
    m = msg or "{{ String($json.stdout || $json.error || 'no output').slice(-3000) }}"
    return f"const {var} = node({{ type: 'n8n-nodes-base.form', version: 2.5, config: {{ name: {J('Show: ' + title)}, position: [800, {y}], parameters: {{ operation: 'completion', respondWith: 'text', completionTitle: {J(title)}, completionMessage: expr({J(m)}) }} }} }});"

code = f"""import {{ workflow, node, trigger, sticky, switchCase, newCredential, expr }} from '@n8n/workflow-sdk';

const form = trigger({{ type: 'n8n-nodes-base.formTrigger', version: 2.6, config: {{ name: 'Video App Control Form', position: [0, 300],
  parameters: {{ authentication: 'basicAuth', formTitle: 'MoneyPrinter Video App Control',
    formDescription: 'Free Kaggle GPU clips for MoneyPrinter. One action per submit. The batch kernel is shared with YouTube Auto (30 GPU h/week).',
    formFields: {{ values: [
      {{ fieldLabel: 'Action', fieldType: 'dropdown', requiredField: true, fieldOptions: {{ values: {J([{"option": a} for a in ACTIONS])} }} }},
      {{ fieldLabel: 'Test prompt', fieldType: 'textarea', placeholder: 'Only for Queue a test clip' }},
      {{ fieldLabel: 'Model', fieldType: 'dropdown', fieldOptions: {{ values: {J([{"option": m} for m in ["wan21-diffusers", "ltx-t2v", "wan22-5b-diffusers", "hy15-t2v"]])} }} }}
    ] }}, options: {{ path: 'mpt-video-control', buttonLabel: 'Run', appendAttribution: false }} }},
  credentials: {{ httpBasicAuth: newCredential('MPT Video Control Login') }} }},
  output: [{{ Action: 'Status and quota', 'Test prompt': '', Model: 'wan21-diffusers' }}] }});
const route = switchCase({{ version: 3.2, config: {{ name: 'Which Action?', position: [260, 300], parameters: {{ rules: {{ values: [
  {rule(0, ACTIONS[0])}, {rule(1, ACTIONS[1])}, {rule(2, ACTIONS[2])}, {rule(3, ACTIONS[3])}, {rule(4, ACTIONS[4])} ] }}, options: {{}} }} }} }});
{cmd('cStatus', 'Kaggle: Quota + Status', CMD_STATUS, 0)}
{cmd('cRun', 'Kaggle: Ensure Batch Running', CMD_RUN, 150)}
{cmd('cSelf', 'Kaggle: Selftest (only if idle)', CMD_SELF, 300)}
{cmd('cFetch', 'Kaggle: Fetch Selftest Result', CMD_FETCH, 450)}
const qBuild = node({{ type: 'n8n-nodes-base.code', version: 2, config: {{ name: 'Build Test Job', position: [560, 600], parameters: {{ mode: 'runOnceForAllItems', jsCode: {J(QUEUE)} }} }}, output: [{{ job_id: 'mpt_manual_x', model: 'wan21-diffusers' }}] }});
const qUpload = node({{ type: 'n8n-nodes-base.googleDrive', version: 3, config: {{ name: 'Queue Test Job in Drive', position: [700, 600],
  parameters: {{ resource: 'file', operation: 'upload', name: expr('{{{{ $json.job_id }}}}.json'), inputDataFieldName: 'data',
    driveId: {{ __rl: true, mode: 'list', value: 'My Drive' }}, folderId: {{ __rl: true, mode: 'id', value: expr({J(JOBS)}) }}, options: {{}} }},
  credentials: {{ googleDriveOAuth2Api: {{ id: 'hWl3Fg2eiftqZane', name: 'Google Drive account' }} }} }}, output: [{{ id: 'f1', name: 'mpt_manual_x.json' }}] }});
{done('dStatus', 'Status and quota', 0)}
{done('dRun', 'Run GPU batch now', 150)}
{done('dSelf', 'HunyuanVideo selftest', 300)}
{done('dFetch', 'Selftest result', 450)}
{done('dQueue', 'Test clip queued', 600, "Queued job {{ $('Build Test Job').first().json.job_id }} ({{ $('Build Test Job').first().json.model }}). The GPU Router starts Kaggle within 20 minutes if it is active, or use Run GPU batch now. The clip lands in the Drive outputs folder as <job_id>.scene_000.mp4.")}

const note = sticky('## MPT Video App Control (inactive)\\nBookmark the production form URL (/form/mpt-video-control). Login = credential MPT Video Control Login (create it before activating).\\nRun GPU batch now is safe to press twice: ensure never starts a second kernel. Selftest refuses while the batch kernel runs.', [], {{ color: 4, position: [-40, -260], width: 760, height: 160 }});

export default workflow('mpt-video-control', 'MPT Video App Control')
  .add(form).to(route
    .onCase(0, cStatus.to(dStatus))
    .onCase(1, cRun.to(dRun))
    .onCase(2, cSelf.to(dSelf))
    .onCase(3, cFetch.to(dFetch))
    .onCase(4, qBuild.to(qUpload).to(dQueue)))
  .add(note);
"""
print(code)
