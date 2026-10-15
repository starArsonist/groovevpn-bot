// Запуск: node --test web/connect
// Тест выполняет тот же блок LOGIC:BEGIN/END из index.html, что работает в браузере.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const html = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'index.html'), 'utf8');

const block = html.match(/\/\* LOGIC:BEGIN \*\/([\s\S]*?)\/\* LOGIC:END \*\//);
assert.ok(block, 'в index.html не найден блок LOGIC:BEGIN/END');

const logic = new Function(
  block[1] + '\nreturn { APPS, parseFragment, validate, buildDeeplink, isAllowedDeeplink, resolve };'
)();

const HOSTS = ['sub.example.com'];
const SUB = 'https://sub.example.com:8443/sub/TOKEN123';
const enc = encodeURIComponent;
const fragment = (app, sub) => `#app=${app}&sub=${enc(sub)}`;
const resolveWith = (hash, hosts = HOSTS) => logic.resolve(hash, hosts, 'Groove');

// ---------- допустимые сочетания ----------

test('допустимое приложение и хост из белого списка', () => {
  for (const app of ['happ', 'v2raytun', 'hiddify']) {
    const r = resolveWith(fragment(app, SUB));
    assert.equal(r.ok, true, app);
    assert.equal(r.app, app);
    assert.equal(r.sub, SUB);
  }
});

test('deeplink строится по шаблону каждого приложения', () => {
  assert.equal(resolveWith(fragment('happ', SUB)).deeplink, `happ://add/${SUB}`);
  assert.equal(resolveWith(fragment('v2raytun', SUB)).deeplink, `v2raytun://import/${SUB}`);
  assert.equal(resolveWith(fragment('hiddify', SUB)).deeplink, `hiddify://import/${SUB}#Groove`);
});

test('имя профиля Hiddify кодируется', () => {
  const link = logic.resolve(fragment('hiddify', SUB), HOSTS, 'Мой профиль #1&x').deeplink;
  assert.equal(link, `hiddify://import/${SUB}#${enc('Мой профиль #1&x')}`);
  assert.equal(link.split('#').length, 2);
});

test('регистр хоста не важен, порт и путь допустимы', () => {
  assert.equal(resolveWith(fragment('happ', 'https://SUB.Example.COM/sub/x')).ok, true);
  assert.equal(resolveWith(fragment('happ', 'https://sub.example.com:2096/a?b=c')).ok, true);
  assert.equal(logic.resolve(fragment('happ', SUB), ['SUB.EXAMPLE.COM'], 'G').ok, true);
});

test('значение sub раскодируется ровно один раз', () => {
  const sub = 'https://sub.example.com/sub/a%2Fb?x=1&y=%2B';
  const r = resolveWith(fragment('happ', sub));
  assert.equal(r.ok, true);
  assert.equal(r.sub, sub);
});

// ---------- отказы ----------

const REJECT = [
  ['чужой хост', fragment('happ', 'https://evil.example.net/sub/x')],
  ['поддомен разрешённого хоста', fragment('happ', 'https://x.sub.example.com/sub/x')],
  ['разрешённый хост как поддомен чужого', fragment('happ', 'https://sub.example.com.evil.net/sub/x')],
  ['суффикс-обманка', fragment('happ', 'https://evilsub.example.com/sub/x')],
  ['хост в userinfo', fragment('happ', 'https://sub.example.com@evil.net/sub/x')],
  ['учётные данные при верном хосте', fragment('happ', 'https://user:pass@sub.example.com/sub/x')],
  ['хост с точкой в конце', fragment('happ', 'https://sub.example.com./sub/x')],
  ['http вместо https', fragment('happ', 'http://sub.example.com/sub/x')],
  ['javascript:', fragment('happ', 'javascript:alert(1)')],
  ['javascript: с разрешённым хостом в тексте', fragment('happ', 'javascript://sub.example.com/%0Aalert(1)')],
  ['data:', fragment('happ', 'data:text/html,<script>alert(1)</script>')],
  ['file:', fragment('happ', 'file:///etc/passwd')],
  ['схема приложения вместо https', fragment('happ', 'happ://add/https://sub.example.com/x')],
  ['неизвестное приложение', fragment('evilapp', SUB)],
  ['приложение __proto__', fragment('__proto__', SUB)],
  ['приложение constructor', fragment('constructor', SUB)],
  ['приложение toString', fragment('toString', SUB)],
  ['приложение в другом регистре', fragment('HAPP', SUB)],
  ['пустой фрагмент', ''],
  ['только решётка', '#'],
  ['нет sub', '#app=happ'],
  ['нет app', `#sub=${enc(SUB)}`],
  ['пустой sub', '#app=happ&sub='],
  ['sub с фрагментом', fragment('happ', 'https://sub.example.com/x#frag')],
  ['sub с пробелом', fragment('happ', 'https://sub.example.com/a b')],
  ['sub с переводом строки', fragment('happ', 'https://sub.example.com/a\nb')],
  ['sub с управляющим символом', fragment('happ', 'https://sub.example.com/a\u0000b')],
  ['слишком длинный sub', fragment('happ', 'https://sub.example.com/' + 'a'.repeat(2100))],
  ['sub передан без кодирования с плюсом', '#app=happ&sub=https://sub.example.com/a+b']
];

for (const [name, hash] of REJECT) {
  test(`отказ: ${name}`, () => {
    assert.deepEqual(resolveWith(hash), { ok: false });
  });
}

test('пустой список ALLOWED_SUB_HOSTS = отказ всем', () => {
  assert.deepEqual(resolveWith(fragment('happ', SUB), []), { ok: false });
});

test('некорректный список хостов = отказ', () => {
  assert.deepEqual(resolveWith(fragment('happ', SUB), null), { ok: false });
  assert.deepEqual(resolveWith(fragment('happ', SUB), 'sub.example.com'), { ok: false });
});

test('данные из query-параметров игнорируются', () => {
  assert.deepEqual(resolveWith(`?app=happ&sub=${enc(SUB)}`), { ok: false });
});

// ---------- deeplink ----------

test('buildDeeplink не работает для неизвестных приложений', () => {
  assert.equal(logic.buildDeeplink('evil', SUB, 'G'), null);
  assert.equal(logic.buildDeeplink('__proto__', SUB, 'G'), null);
});

test('isAllowedDeeplink пропускает только схемы из белого списка', () => {
  assert.equal(logic.isAllowedDeeplink('happ://add/x'), true);
  assert.equal(logic.isAllowedDeeplink('v2raytun://import/x'), true);
  assert.equal(logic.isAllowedDeeplink('hiddify://import/x'), true);
  for (const bad of ['javascript:alert(1)', 'https://evil.net', 'data:text/html,x', 'vless://x', '', null, undefined, 42]) {
    assert.equal(logic.isAllowedDeeplink(bad), false, String(bad));
  }
});

test('каждый результат resolve - это deeplink из белого списка', () => {
  for (const app of ['happ', 'v2raytun', 'hiddify']) {
    assert.equal(logic.isAllowedDeeplink(resolveWith(fragment(app, SUB)).deeplink), true);
  }
});

// ---------- статические проверки файла ----------

test('ALLOWED_SUB_HOSTS - список простых имён хостов (без схемы, порта и пути)', () => {
  const match = html.match(/const ALLOWED_SUB_HOSTS = \[([^\]]*)\];/);
  assert.ok(match, 'не найдена константа ALLOWED_SUB_HOSTS');
  const hosts = [...match[1].matchAll(/'([^']*)'/g)].map((m) => m[1]);
  for (const host of hosts) {
    assert.match(host, /^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$/i, `некорректное имя хоста: ${host}`);
  }
  assert.match(html, /const PROFILE_NAME = '[^']+';/);
});

test('обязательные meta-теги', () => {
  assert.match(html, /<meta name="referrer" content="no-referrer">/);
  assert.match(html, /<meta name="robots" content="noindex,nofollow">/);
  assert.match(html, /Content-Security-Policy" content="default-src 'none'/);
});

test('нет небезопасных DOM-API и динамического исполнения кода', () => {
  const forbidden = [
    /innerHTML/, /outerHTML/, /insertAdjacentHTML/, /document\.write/,
    /\beval\s*\(/, /new Function/, /setTimeout\s*\(\s*['"`]/, /\.srcdoc/
  ];
  for (const pattern of forbidden) {
    assert.doesNotMatch(html, pattern, String(pattern));
  }
});

test('нет внешних запросов и загрузки ресурсов', () => {
  const forbidden = [
    /\bsrc\s*=/i, /<link\b/i, /@import/i, /\burl\s*\(/, /\bfetch\s*\(/, /XMLHttpRequest/,
    /WebSocket/, /sendBeacon/, /EventSource/, /\bimport\s*\(/, /<img\b/i, /<iframe\b/i,
    /<script[^>]*\ssrc/i, /<form\b/i, /<base\b/i
  ];
  for (const pattern of forbidden) {
    assert.doesNotMatch(html, pattern, String(pattern));
  }
});

test('http(s)-адреса в файле - только ссылки "Где скачать" на официальные страницы', () => {
  const found = new Set(html.match(/https?:\/\/[^\s'"<>)]+/g) ?? []);
  assert.deepEqual([...found].sort(), ['https://hiddify.com/app/', 'https://www.happ.su/main']);
});

test('в тексте страницы нет слова VPN (настройки деплоя - хост и имя профиля - не в счёт)', () => {
  const withoutDeploySettings = html.replace(/^const (ALLOWED_SUB_HOSTS|PROFILE_NAME) = .*$/gm, '');
  assert.doesNotMatch(withoutDeploySettings, /vpn/i);
});
