import { test } from 'node:test';
import assert from 'node:assert/strict';
import { install, connect } from '../src/codinator/pi_extension.mjs';
import net from 'node:net';
import { Duplex } from 'node:stream';
import { syncBuiltinESMExports } from 'node:module';

function harness(overrides = {}) {
  const handlers = {}, commands = {}, tools = {}, messages = [], operations = [];
  let poll;
  const originalInterval = globalThis.setInterval, originalClear = globalThis.clearInterval;
  globalThis.setInterval = fn => { poll = fn; return 123; };
  globalThis.clearInterval = () => {};
  const pi = {
    on: (name, fn) => { handlers[name] = fn; },
    registerTool: tool => { tools[tool.name] = tool; },
    registerCommand: (name, fn) => { commands[name] = fn; },
    sendMessage: (message, options) => messages.push({ message, options }),
  };
  const ctx = {
    model: { provider: 'bonsai', id: 'bonsai2-27b' }, thinkingLevel: 'xhigh',
    ui: { setStatus() {}, setWidget() {}, notify() {} },
    isIdle: () => true, abort: async () => {}, waitForIdle: async () => {},
    sessionManager: { getSessionId: () => 'session-1', getBranch: () => [{ type: 'message', message: { role: 'assistant', content: [
      { type: 'toolCall', name: 'codex_submit_review', id: 'call-1' },
    ] } }] },
  };
  const state = { id: 'test', state: 'ready', round: 1, attempt: 0, evidence: '/external/round',
    handoff: '/repo/handoff.md', allowed_paths: ['product.py'], checks: [], feedback: '' };
  const request = async req => {
    operations.push(req);
    if (req.op === 'begin') state.state = 'implementing';
    if (req.op === 'pause') {
      if (overrides.pause) return overrides.pause(req, state);
      state.state = 'paused'; state.reason = req.reason;
    }
    if (req.op === 'resume') state.state = state.phase === 'feedback' ? 'needs_changes' : 'ready';
    if (req.op === 'submit') {
      if (overrides.submit) return overrides.submit(req, state);
      state.state = 'reviewing'; state.attempt++;
    }
    return structuredClone(state);
  };
  install(pi, request);
  return { ctx, state, handlers, commands, tools, messages, operations,
    poll: () => poll(), start: () => handlers.session_start({}, ctx),
    close: async () => {
      try { await handlers.session_shutdown(); }
      finally { globalThis.setInterval = originalInterval; globalThis.clearInterval = originalClear; }
    },
    reserve: () => handlers.tool_call({ toolName: 'codex_submit_review', toolCallId: 'call-1' }, ctx),
  };
}

test('complete automatic implementation, submit, needs_changes, resubmit, accepted without commands', async () => {
  const h = harness();
  try {
    await h.start();
    assert.equal(h.messages.length, 1);
    assert.equal(h.messages[0].options.triggerTurn, true);
    assert.equal(h.messages[0].message.customType, 'codinator-review');
    await h.reserve();
    const result = await h.tools.codex_submit_review.execute('call-1', { summary: '# Delivery\nTests passed' });
    assert.equal(result.terminate, true);
    assert.equal(h.operations.filter(r => r.op === 'submit').length, 0, 'must wait for settled');
    await h.handlers.agent_settled({}, h.ctx);
    assert.equal(h.operations.filter(r => r.op === 'submit').length, 1);
    assert.equal((await h.handlers.tool_call({ toolName: 'write' }, h.ctx)).block, true);
    h.state.state = 'needs_changes'; h.state.round = 2; h.state.feedback = '# Codex review\nFix boundary';
    await h.poll();
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 2);
    assert.match(h.messages.at(-1).message.content, /Fix boundary/);
    await h.reserve();
    await h.tools.codex_submit_review.execute('call-1', { summary: '# Fixed' });
    await h.handlers.agent_settled({}, h.ctx);
    h.state.state = 'accepted'; h.state.reason = 'verified';
    await h.poll(); await h.poll();
    assert.equal(h.messages.length, 3, 'accepted displayed once');
    assert.equal(h.messages.at(-1).options.triggerTurn, false);
  } finally { await h.close(); }
});

test('mixed batch is rejected without submitting or freezing ordinary tools', async () => {
  const h = harness();
  try {
    await h.start();
    h.ctx.sessionManager.getBranch = () => [{ type: 'message', message: { role: 'assistant', content: [
      { type: 'toolCall', id: 'call-1' }, { type: 'toolCall', id: 'other' },
    ] } }];
    const result = await h.reserve();
    assert.equal(result.block, true);
    assert.match(result.reason, /alone/);
    assert.equal(await h.handlers.tool_call({ toolName: 'write' }, h.ctx), undefined);
    assert.equal(h.operations.filter(r => r.op === 'submit').length, 0);
  } finally { await h.close(); }
});

test('explicit resume of legacy blocked feedback dispatches Pi once with the existing review', async () => {
  const h = harness();
  try {
    Object.assign(h.state, { state: 'blocked', phase: 'feedback', round: 3, attempt: 2,
      reason: 'Two consecutive reviews retain the same issue set', feedback: 'R1: Finish the partial repair' });
    await h.start();
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 0);
    assert.equal((await h.handlers.tool_call({ toolName: 'write' }, h.ctx)).block, true);
    await h.commands['codex-resume'].handler('', h.ctx);
    await h.poll();
    assert.equal(h.operations.filter(r => r.op === 'resume').length, 1);
    assert.equal(h.operations.filter(r => r.op === 'begin').length, 1);
    const turns = h.messages.filter(m => m.options.triggerTurn);
    assert.equal(turns.length, 1);
    assert.match(turns[0].message.content, /R1: Finish the partial repair/);
    assert.equal(await h.handlers.tool_call({ toolName: 'write' }, h.ctx), undefined);
  } finally { await h.close(); }
});

test('Escape after reservation cancels pending submission and does not restart Pi', async () => {
  const h = harness();
  try {
    await h.start(); await h.reserve();
    await h.tools.codex_submit_review.execute('call-1', { summary: 'delivery' });
    h.handlers.message_end({ message: { role: 'assistant', stopReason: 'aborted' } });
    await h.handlers.agent_settled({}, h.ctx);
    await h.poll();
    assert.equal(h.state.state, 'paused');
    assert.equal(h.operations.filter(r => r.op === 'submit').length, 0);
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 1);
  } finally { await h.close(); }
});

test('abort after terminating tool does not require an aborted assistant message', async () => {
  const h = harness();
  try {
    await h.start(); await h.reserve();
    const controller = new AbortController();
    await h.tools.codex_submit_review.execute('call-1', { summary: 'delivery' }, controller.signal);
    h.handlers.message_end({ message: { role: 'assistant', stopReason: 'toolUse' } });
    controller.abort();
    await h.handlers.agent_settled({}, h.ctx);
    assert.equal(h.state.state, 'paused');
    assert.equal(h.operations.filter(r => r.op === 'submit').length, 0);
  } finally { await h.close(); }
});

test('pause is persisted before abort and frozen ! shell commands never execute', async () => {
  const h = harness();
  try {
    await h.start();
    h.ctx.abort = async () => assert.equal(h.state.state, 'paused');
    await h.commands['codex-pause'].handler('', h.ctx);
    assert.equal(h.handlers.user_bash().result.exitCode, 1);
    assert.equal((await h.handlers.tool_call({ toolName: 'bash' }, h.ctx)).block, true);
    await h.commands['codex-resume'].handler('', h.ctx);
    assert.equal(h.state.state, 'implementing');
  } finally { await h.close(); }
});

test('model mismatch pauses before another implementation turn', async () => {
  const h = harness();
  try {
    await h.start();
    h.ctx.model.id = 'different';
    await h.handlers.before_agent_start({}, h.ctx);
    assert.equal(h.state.state, 'paused');
    assert.equal((await h.handlers.tool_call({ toolName: 'bash' }, h.ctx)).block, true);
  } finally { await h.close(); }
});

test('definite rejection returns to Pi once without replaying the submission', async () => {
  const h = harness({ submit: async (_req, state) => {
    throw Object.assign(new Error('outside.txt is outside allowed paths'),
      { code: 'submission_rejected', status: { ...state, state: 'implementing' } });
  } });
  try {
    await h.start(); await h.reserve();
    await h.tools.codex_submit_review.execute('call-1', { summary: 'delivery' });
    await h.handlers.agent_settled({}, h.ctx);
    assert.equal(h.operations.filter(r => r.op === 'submit').length, 1);
    assert.equal(h.operations.filter(r => r.op === 'pause').length, 0);
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 2);
    assert.match(h.messages.at(-1).message.content, /明确拒收.*outside.txt/);
    assert.equal(await h.handlers.tool_call({ toolName: 'write' }, h.ctx), undefined);
    await h.reserve();
    await h.tools.codex_submit_review.execute('call-1', { summary: 'still bad' });
    await h.handlers.agent_settled({}, h.ctx);
    assert.equal(h.state.state, 'paused');
    assert.match(h.state.reason, /再次被明确拒收/);
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 2);
  } finally { await h.close(); }
});

test('unknown submission failure pauses with exact cause and never retries', async () => {
  const h = harness({ submit: async () => { throw new Error('socket timeout after possible receipt'); } });
  try {
    await h.start(); await h.reserve();
    await h.tools.codex_submit_review.execute('call-1', { summary: 'delivery' });
    await h.handlers.agent_settled({}, h.ctx); await h.poll();
    assert.equal(h.state.state, 'paused');
    assert.match(h.state.reason, /提交结果不确定：socket timeout/);
    assert.equal(h.operations.filter(r => r.op === 'submit').length, 1);
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 1);
  } finally { await h.close(); }
});

test('late rejection cannot undo an explicit pause or trigger more Pi work', async () => {
  let release;
  const h = harness({ submit: (_req, state) => new Promise((_resolve, reject) => {
    release = () => reject(Object.assign(new Error('scope rejected'),
      { code: 'submission_rejected', status: { ...state, state: 'implementing' } }));
  }) });
  try {
    await h.start(); await h.reserve();
    await h.tools.codex_submit_review.execute('call-1', { summary: 'delivery' });
    const settled = h.handlers.agent_settled({}, h.ctx);
    await h.commands['codex-pause'].handler('', h.ctx);
    release(); await settled;
    assert.equal(h.state.state, 'paused');
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 1);
  } finally { await h.close(); }
});

test('abort while waiting for a successful receipt pauses without replay', async () => {
  let release;
  const h = harness({ submit: (_req, state) => new Promise(resolve => {
    release = () => { state.state = 'reviewing'; resolve(structuredClone(state)); };
  }) });
  try {
    await h.start(); await h.reserve();
    const controller = new AbortController();
    await h.tools.codex_submit_review.execute('call-1', { summary: 'delivery' }, controller.signal);
    const settled = h.handlers.agent_settled({}, h.ctx);
    controller.abort(); release(); await settled;
    assert.equal(h.state.state, 'paused');
    assert.match(h.state.reason, /提交等待期间 Pi 已中止/);
    assert.equal(h.operations.filter(r => r.op === 'submit').length, 1);
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 1);
  } finally { await h.close(); }
});

test('failed pause RPC preserves cause in session and polling cannot unfreeze tools', async () => {
  const h = harness({
    submit: async () => { throw new Error('submit transport failed'); },
    pause: async () => { throw new Error('controller still unreachable'); },
  });
  try {
    await h.start(); await h.reserve();
    await h.tools.codex_submit_review.execute('call-1', { summary: 'delivery' });
    await h.handlers.agent_settled({}, h.ctx);
    const count = h.operations.length;
    await h.poll();
    assert.equal(h.operations.length, count);
    assert.equal((await h.handlers.tool_call({ toolName: 'write' }, h.ctx)).block, true);
    assert.ok(h.messages.some(m => /提交结果不确定：submit transport failed/.test(m.message.content)));
    assert.ok(h.messages.some(m => /暂停状态写入失败/.test(m.message.content)));
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 1);
  } finally {
    // The shutdown request also fails while this transport remains offline.
    await h.close().catch(() => {});
  }
});

test('session shutdown clears pending and poll cannot dispatch another round', async () => {
  const h = harness();
  try {
    await h.start(); await h.reserve();
    await h.tools.codex_submit_review.execute('call-1', { summary: 'delivery' });
    await h.handlers.session_shutdown();
    h.state.state = 'needs_changes';
    await h.poll();
    assert.equal(h.operations.filter(r => r.op === 'submit').length, 0);
    assert.equal(h.messages.filter(m => m.options.triggerTurn).length, 1);
  } finally { await h.close(); }
});

test('bridge preserves Chinese across a UTF-8 chunk boundary', async () => {
  const original = net.createConnection;
  net.createConnection = () => {
    const socket = new Duplex({ read() {}, write(_chunk, _encoding, done) { done(); } });
    socket.setTimeout = () => socket;
    const data = Buffer.from(JSON.stringify({ ok: true, value: '检查完成' }));
    const split = data.indexOf(Buffer.from('检查')) + 1;
    queueMicrotask(() => {
      socket.emit('connect');
      socket.push(data.subarray(0, split));
      socket.push(data.subarray(split));
      socket.push(null);
    });
    return socket;
  };
  syncBuiltinESMExports();
  try { assert.equal(await connect('/unused', { op: 'status' }), '检查完成'); }
  finally { net.createConnection = original; syncBuiltinESMExports(); }
});
