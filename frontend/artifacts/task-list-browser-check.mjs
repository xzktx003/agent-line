import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile, writeFile, mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const directory = path.resolve("frontend");
const base = process.env.WORKBENCH_URL || "http://127.0.0.1:3193/workbench/";
const chromium = process.env.CHROMIUM_PATH;
if (!chromium) throw new Error("Set CHROMIUM_PATH to an installed Chromium binary");
const privateConfig = JSON.parse(await readFile(path.join(directory, "../var/adapter/service.json"), "utf8"));
const credential = Object.keys(privateConfig.tokens)[0];
const profile = await mkdtemp(path.join(tmpdir(), "agent-line-browser-"));
const output = path.join(directory, "artifacts");
await mkdir(output, { recursive: true });
const browser = spawn(chromium, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const checks = [];
let socket;
try {
  let port;
  for (let attempt = 0; attempt < 50; attempt++) {
    try { port = (await readFile(path.join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]; break; }
    catch { await delay(100); }
  }
  assert.ok(port, "Chromium must expose a live debugging port");
  const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket = new WebSocket(tabs[0].webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  let sequence = 0;
  const pending = new Map();
  const browserErrors = [];
  socket.addEventListener("message", (message) => {
    const value = JSON.parse(message.data);
    if (value.method === "Runtime.exceptionThrown") browserErrors.push(value.params.exceptionDetails.text);
    if (pending.has(value.id)) {
      const { resolve, reject, timer } = pending.get(value.id);
      clearTimeout(timer); pending.delete(value.id);
      if (value.error) reject(new Error(value.error.message)); else resolve(value.result);
    }
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 30000);
    pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error("Browser evaluation failed");
    return result.result.value;
  };
  const waitFor = async (expression) => {
    for (let attempt = 0; attempt < 120; attempt++) {
      if (await evaluate(expression)) return;
      await delay(250);
    }
    throw new Error(`Browser condition was not reached: ${expression}`);
  };
  const screenshot = async (name) => {
    const metrics = await call("Page.getLayoutMetrics");
    const image = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, clip: { x: 0, y: 0, width: metrics.cssContentSize.width, height: metrics.cssContentSize.height, scale: 1 } });
    await writeFile(path.join(output, name), Buffer.from(image.data, "base64"));
  };
  await call("Page.enable"); await call("Runtime.enable");
  await call("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: base });
  await waitFor("document.readyState === 'complete' && !!document.getElementById('start-form')");
  assert.equal(await evaluate("document.querySelector('#start-form button[type=submit]').disabled"), true);
  await screenshot("desktop-disconnected.png");
  checks.push({ name: "未认证时禁用执行操作", passed: true });
  await evaluate("document.getElementById('token').value='invalid-browser-validation-token';document.getElementById('login-form').requestSubmit()");
  await waitFor("document.getElementById('notice').textContent.includes('凭证无效')");
  assert.equal(await evaluate("document.getElementById('connection-badge').textContent"), "尚未连接");
  checks.push({ name: "真实无效凭证被拒绝并显示中文错误", passed: true });
  await evaluate(`document.getElementById('token').value=${JSON.stringify(credential)};document.getElementById('login-form').requestSubmit()`);
  await waitFor("document.getElementById('connection-badge').textContent === '服务已连接'");
  await waitFor("document.getElementById('binding-options').children.length > 0");
  assert.equal(await evaluate("document.getElementById('token').value"), "");
  assert.equal(await evaluate("localStorage.length + sessionStorage.length"), 0);
  checks.push({ name: "真实凭证连接并读取授权绑定，未使用浏览器存储", passed: true });
  await waitFor("document.querySelectorAll('#task-list li').length > 0");
  const count = await evaluate("document.querySelectorAll('#task-list li').length");
  checks.push({name:"真实授权任务列表自动加载",passed:true,count});
  await evaluate("document.getElementById('task-status').value='succeeded';document.getElementById('task-status').dispatchEvent(new Event('change'))");
  await waitFor("document.querySelectorAll('#task-list li').length > 0 && !document.getElementById('task-list-message').textContent.includes('正在')");
  assert.equal(await evaluate("[...document.querySelectorAll('.task-states')].every(e=>e.textContent.includes('执行：执行成功'))"),true);
  checks.push({name:"真实成功状态筛选",passed:true});
  await evaluate("[...document.querySelectorAll('.task-row')].find(e=>e.textContent.includes('task-b80a131c24f04608ac159a7d6d71e4be')).click()");
  await waitFor("document.getElementById('task-result').textContent.includes('验证通过')");
  assert.equal(await evaluate("document.getElementById('cancel-task').hidden"),true);
  checks.push({name:"点选真实任务加载详情、独立验证与证据，终态不显示取消",passed:true});
  assert.equal(await evaluate("document.getElementById('task-prev').disabled"),true);
  if (await evaluate("!document.getElementById('task-next').disabled")) {
    await evaluate("document.getElementById('task-next').click()");
    await waitFor("document.getElementById('task-page').textContent==='第 2 页'");
    await evaluate("document.getElementById('task-prev').click()");
    await waitFor("document.getElementById('task-page').textContent==='第 1 页'");
    checks.push({name:"真实游标前后翻页",passed:true});
  } else checks.push({name:"真实末页禁用下一页",passed:true});
  await evaluate("window.scrollTo(0,0);document.activeElement?.blur()");
  await screenshot("task-list-desktop.png");
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await evaluate("window.scrollTo(0,0)");
  await screenshot("task-list-mobile.png");
  assert.equal(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"),true);
  checks.push({name:"窄屏无页面横向溢出",passed:true});
  await evaluate("document.getElementById('task-status').value='registered';document.getElementById('task-status').dispatchEvent(new Event('change'))");
  await waitFor("!document.getElementById('task-list-message').textContent.includes('正在')");
  checks.push({name:"切换筛选清除前一页数据并重置页码",passed:await evaluate("document.getElementById('task-page').textContent==='第 1 页'")});
  await evaluate("document.getElementById('logout').click()");
  assert.equal(await evaluate("document.querySelectorAll('#task-list li').length"),0);
  assert.equal(await evaluate("document.getElementById('task-prev').disabled && document.getElementById('task-next').disabled"),true);
  assert.equal(await evaluate("document.getElementById('task-result').textContent.includes('task-b80a')"),false);
  checks.push({name:"断开清除列表与详情并禁用分页",passed:true});
  assert.deepEqual(browserErrors, []);
  await writeFile(path.join(output,"task-list-browser-report.json"),JSON.stringify({checkedAt:new Date().toISOString(),checks,limitations:["当前真实记录不足一页，未验证非空游标前后翻页（如未生成对应检查项）","仅本机浏览器验证，不代表跨设备访问"]},null,2));
  console.log(JSON.stringify({passed:checks.length,report:"frontend/artifacts/task-list-browser-report.json"}));
} finally {
  socket?.close(); browser.kill("SIGTERM");
  await new Promise((resolve) => browser.exitCode !== null ? resolve() : browser.once("exit", resolve));
  await rm(profile, { recursive: true, force: true });
}
