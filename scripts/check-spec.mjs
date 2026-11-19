#!/usr/bin/env node
/**
 * 规范一致性校验（自检门禁的一部分）。
 *
 * 校验三件事：
 * 1. `cordis.yml` 与 `cordis.patch.yml` 的插件 row 与 config 完全一致；
 * 2. `locale/*.json` 与 `locale/*.yml` 的内容一致（DSH 读 json，yml 是镜像）；
 * 3. `package.json` 的关键字段符合 DSH 插件规范。
 *
 * 任何一项不一致都会以非零码退出，供 `npm run check:spec` 与 CI 使用。
 */

import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const failures = [];
const notes = [];

/** 极简 YAML 读取：只支持本仓库用到的「- insert: [{id, name, config}]」形状。 */
function readPatchConfig(file) {
  const text = readFileSync(join(ROOT, file), 'utf8');
  const idMatch = text.match(/^\s*-\s*id:\s*(.+)$/m);
  const nameMatch = text.match(/^\s*name:\s*['"]?([^'"\n]+)['"]?\s*$/m);
  const configBlock = text.split(/^\s*config:\s*$/m)[1] ?? '';

  const config = {};
  for (const rawLine of configBlock.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    if (/^[a-zA-Z]/.test(line) && line.includes(':')) {
      const index = line.indexOf(':');
      const key = line.slice(0, index).trim();
      let value = line.slice(index + 1).trim();
      value = value.replace(/\s+#.*$/, '');
      if (value === '') continue;
      if (value === 'true') value = true;
      else if (value === 'false') value = false;
      else if (/^-?\d+$/.test(value)) value = Number(value);
      else if (value === '{}') value = {};
      else if (/^'.*'$/.test(value) || /^".*"$/.test(value)) value = value.slice(1, -1);
      config[key] = value;
    }
  }
  return {
    id: idMatch ? idMatch[1].trim() : null,
    name: nameMatch ? nameMatch[1].trim() : null,
    config,
  };
}

// --- 1) cordis.yml vs cordis.patch.yml -------------------------------------
const patch = readPatchConfig('cordis.patch.yml');
const manifest = readPatchConfig('cordis.yml');

if (patch.id !== manifest.id) {
  failures.push(`cordis 清单 id 不一致：patch=${patch.id} / manifest=${manifest.id}`);
}
if (patch.name !== manifest.name) {
  failures.push(`cordis 清单 name 不一致：patch=${patch.name} / manifest=${manifest.name}`);
}
if (patch.name !== 'dsh-davinci-qing') {
  failures.push(`插件 name 必须是 dsh-davinci-qing，实际 ${patch.name}`);
}

const patchKeys = Object.keys(patch.config).sort();
const manifestKeys = Object.keys(manifest.config).sort();
if (patchKeys.join(',') !== manifestKeys.join(',')) {
  failures.push(
    `cordis 配置项不一致：\n  patch   = ${patchKeys.join(', ')}\n  manifest= ${manifestKeys.join(', ')}`,
  );
} else {
  for (const key of patchKeys) {
    if (String(patch.config[key]) !== String(manifest.config[key])) {
      failures.push(`配置项 ${key} 不一致：patch=${patch.config[key]} / manifest=${manifest.config[key]}`);
    }
  }
}

// --- 2) locale json vs yml -------------------------------------------------
const pairs = [
  ['locale/zh.json', 'locale/zh-CN.yml'],
  ['locale/en.json', 'locale/en-US.yml'],
];

/** 把 locale json 压成 "路径=值" 的扁平集合，用于和 YAML 的 key 对比。 */
function flattenJson(value, prefix = '') {
  const out = {};
  for (const [key, entry] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (entry && typeof entry === 'object' && !Array.isArray(entry)) {
      Object.assign(out, flattenJson(entry, path));
    } else {
      out[path] = String(entry);
    }
  }
  return out;
}

/** 从 YAML 文本里抽出 "路径: 值"，仅覆盖本仓库的简单嵌套结构。 */
function flattenYaml(text) {
  const out = {};
  const stack = [];
  let folding = null; // 正在收集的折叠块 { path, parts }

  const flushFolding = () => {
    if (folding) {
      out[folding.path] = folding.parts.join(' ').replace(/\s+/g, ' ').trim();
      folding = null;
    }
  };

  for (const rawLine of text.split(/\r?\n/)) {
    if (!rawLine.trim() || rawLine.trim().startsWith('#')) continue;
    const indent = rawLine.match(/^\s*/)[0].length;
    const line = rawLine.trim();

    // 折叠块（>- / | / >）的续行：更缩进且不像 key
    if (folding && indent > 2 && !/^[A-Za-z_][\w-]*:\s/.test(line)) {
      folding.parts.push(line);
      continue;
    }
    flushFolding();

    if (line.startsWith('- ')) continue;
    const index = line.indexOf(':');
    if (index < 0) continue;

    const key = line.slice(0, index).trim();
    const value = line.slice(index + 1).trim();
    const depth = Math.floor(indent / 2);

    stack.length = depth;
    stack[depth] = key;

    if (value === '' || value === '|') continue;
    if (value === '>-' || value === '>' || value === '|-') {
      folding = { path: stack.slice(0, depth + 1).join('.'), parts: [] };
      continue;
    }
    out[stack.slice(0, depth + 1).join('.')] = value.replace(/^['"]|['"]$/g, '');
  }
  flushFolding();
  return out;
}

for (const [jsonPath, ymlPath] of pairs) {
  let jsonFlat;
  try {
    jsonFlat = flattenJson(JSON.parse(readFileSync(join(ROOT, jsonPath), 'utf8')));
  } catch (error) {
    failures.push(`${jsonPath} 无法解析：${error.message}`);
    continue;
  }
  let ymlFlat;
  try {
    ymlFlat = flattenYaml(readFileSync(join(ROOT, ymlPath), 'utf8'));
  } catch (error) {
    failures.push(`${ymlPath} 无法读取：${error.message}`);
    continue;
  }

  const missingInYml = Object.keys(jsonFlat).filter((key) => !(key in ymlFlat));
  if (missingInYml.length > 0) {
    failures.push(`${ymlPath} 缺少 ${missingInYml.length} 个键：${missingInYml.slice(0, 5).join(', ')}`);
  }
  const missingInJson = Object.keys(ymlFlat).filter((key) => !(key in jsonFlat));
  if (missingInJson.length > 0) {
    failures.push(`${jsonPath} 缺少 ${missingInJson.length} 个键：${missingInJson.slice(0, 5).join(', ')}`);
  }
}

// --- 3) package.json 规范字段 ---------------------------------------------
const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));

const checks = [
  ['keywords 含 "dsh-plugin"', Array.isArray(pkg.keywords) && pkg.keywords.includes('dsh-plugin')],
  ['main 指向 dist/index.js', pkg.main === 'dist/index.js'],
  ['types 指向 dist/index.d.ts', pkg.types === 'dist/index.d.ts'],
  ['exports["."].default', pkg.exports?.['.']?.default === './dist/index.js'],
  ['exports["."].types', pkg.exports?.['.']?.types === './dist/index.d.ts'],
  ['dsh.bundle.patch 指向 cordis.patch.yml', pkg.dsh?.bundle?.patch === './cordis.patch.yml'],
  ['scripts.build 存在', typeof pkg.scripts?.build === 'string'],
  ['type 为 module', pkg.type === 'module'],
  ['engines.node 已声明', typeof pkg.engines?.node === 'string'],
];

for (const [label, pass] of checks) {
  if (!pass) failures.push(`package.json 规范字段不满足：${label}`);
}

// --- 报告 -----------------------------------------------------------------
console.log('dsh-davinci-qing 规范一致性校验');
console.log('='.repeat(56));
console.log(`插件 name      : ${patch.name}`);
console.log(`配置项          : ${patchKeys.join(', ')}`);
console.log(`locale 对照     : ${pairs.map((p) => `${p[0]} ↔ ${p[1]}`).join(' | ')}`);
console.log(`package 检查    : ${checks.length} 项`);
for (const note of notes) console.log(`note: ${note}`);
console.log('-'.repeat(56));

if (failures.length === 0) {
  console.log('✅ 全部一致，符合 DSH 插件规范');
  process.exit(0);
}

console.log(`❌ 发现 ${failures.length} 处不一致：`);
for (const failure of failures) console.log(`   - ${failure}`);
process.exit(1);
