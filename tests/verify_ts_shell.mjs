import assert from 'node:assert';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const mod = await import(pathToFileURL(join(ROOT, 'dist', 'index.js')).href);

console.log('=== 导出检查 ===');
console.log('name     :', mod.name);
console.log('inject   :', mod.inject);
console.log('apply    :', typeof mod.apply);
assert.equal(mod.name, 'dsh-davinci-qing');
assert.deepEqual(mod.inject, ['tools']);
assert.equal(typeof mod.apply, 'function');

// 伪造一个最小 Cordis ctx，只实现 tools.register / effect / logger
const registered = new Map();
const effects = [];
const logs = [];
const ctx = {
  logger: {
    info: (m) => logs.push(['info', m]),
    warn: (m) => logs.push(['warn', m]),
    debug: () => {},
    error: (m) => logs.push(['error', m]),
  },
  tools: {
    register(def) {
      if (!def || typeof def.name !== 'string') throw new Error('bad definition');
      if (registered.has(def.name)) throw new Error(`duplicate tool ${def.name}`);
      registered.set(def.name, def);
      return () => registered.delete(def.name);
    },
  },
  effect(fn) {
    const dispose = fn();
    effects.push(dispose);
    return () => { if (typeof dispose === 'function') dispose(); };
  },
};

console.log('\n=== apply() ===');
await mod.apply(ctx, { env: { QING_DAVINCI_FAKE: '1' }, startupTimeoutMs: 60000 });

console.log('注册工具数:', registered.size);
console.log('日志:', logs.map((l) => l.join(': ')).join(' | '));
assert.ok(registered.size === 43, `期望 43 个工具，实际 ${registered.size}`);

const health = registered.get('qing_resolve_health');
console.log('\n=== qing_resolve_health 定义 ===');
console.log('name       :', health.name);
console.log('params keys:', Object.keys(health.parameters.properties || {}));
console.log('timeoutMs  :', health.timeoutMs);
console.log('output.schema:', !!health.output?.schema);

console.log('\n=== 实际调用 qing_resolve_health ===');
const result = await health.execute({});
console.log('ok        :', result.ok);
console.log('project   :', result.project_name);
console.log('timeline  :', result.timeline_name);
console.log('simulator :', result.simulator);
assert.equal(result.ok, true, 'health 应通过（模拟器模式）');

console.log('\n=== render 输出 ===');
const blocks = health.output.render({}, result);
console.log('block type:', blocks[0].type, '| chars:', blocks[0].text.length);

console.log('\n=== 调用一个只读工具（并发安全标记）===');
const overview = registered.get('qing_project_overview');
console.log('isConcurrencySafe:', typeof overview.isConcurrencySafe === 'function' ? overview.isConcurrencySafe({}) : 'not set');

console.log('\n=== 错误路径：未知工具 ===');
const bad = await health.execute.call ? await (async () => {
  // 用 bridge 的未知工具路径：直接调一个不存在的工具名不会经过壳，改用参数错误验证
  const version = registered.get('qing_resolve_version');
  return await version.execute({ unexpected_param: 1 });
})() : null;
console.log('unexpected param result ok:', bad?.ok, '| error:', bad?.error ?? '(ignored)');

console.log('\n=== 清理（触发 effect disposer）===');
for (const dispose of effects) {
  try { dispose(); } catch (e) { console.log('dispose error:', e.message); }
}
await new Promise((r) => setTimeout(r, 800));
console.log('清理后注册表大小:', registered.size);
assert.equal(registered.size, 0);

console.log('\n✅ Cordis TS 壳验证通过');