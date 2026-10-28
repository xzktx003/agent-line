import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile, writeFile, mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const directory = path.dirname(fileURLToPath(import.meta.url));
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
  await evaluate("document.getElementById('binding').value='condition-check';document.getElementById('workflow-input').value='[]';document.getElementById('start-form').requestSubmit()");
  await waitFor("document.getElementById('notice').textContent.includes('必须是 JSON 对象')");
  checks.push({ name: "阻止无效工作流输入", passed: true });
  await evaluate("document.getElementById('workflow-input').value=JSON.stringify({input:'yes'});document.getElementById('start-form').requestSubmit()");
  await waitFor("document.getElementById('run-id').value.length > 0");
  const runId = await evaluate("document.getElementById('run-id').value");
  assert.match(await evaluate("document.getElementById('run-result').textContent"), /尚未查询/);
  checks.push({ name: "真实启动条件工作流，提交回执不冒充完成", passed: true, runId });
  for (let attempt = 0; attempt < 30; attempt++) {
    await evaluate("document.getElementById('run-form').requestSubmit()");
    await waitFor("!document.querySelector('#run-form button').hasAttribute('aria-busy')");
    if ((await evaluate("document.getElementById('run-result').textContent")).includes("执行已结束")) break;
    await delay(1000);
  }
  assert.match(await evaluate("document.getElementById('run-result').textContent"), /执行已结束/);
  checks.push({ name: "真实查询 Sim 完成状态，独立验收仍未宣称通过", passed: true });
  await screenshot("desktop-connected.png");
  await evaluate("document.getElementById('task-id').value='nonexistent-browser-check';document.getElementById('task-form').requestSubmit()");
  await waitFor("document.getElementById('task-result').textContent.includes('未能读取任务')");
  checks.push({ name: "不存在任务的真实 404 显示错误，未虚构任务", passed: true });
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await evaluate("window.scrollTo(0,0)");
  await screenshot("mobile-connected.png");
  assert.equal(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), true);
  checks.push({ name: "窄屏无横向溢出", passed: true });
  await evaluate("document.getElementById('logout').click()");
  assert.equal(await evaluate("document.getElementById('connection-badge').textContent"), "尚未连接");
  assert.equal(await evaluate("document.querySelector('#start-form button[type=submit]').disabled"), true);
  checks.push({ name: "断开连接清除凭证及执行结果", passed: true });
  await call("Page.reload");
  await waitFor("document.readyState === 'complete' && !!document.getElementById('token')");
  assert.equal(await evaluate("document.getElementById('token').value"), "");
  assert.deepEqual(browserErrors, []);
  checks.push({ name: "刷新未恢复凭证，浏览器无脚本异常", passed: true });
  const report = { checkedAt: new Date().toISOString(), url: base, checks, limitations: ["取消操作尚缺真实非终态节点任务样本", "本机浏览器验证不等同于另一台局域网设备访问", "完整可视化画布尚未实现"] };
  await writeFile(path.join(output, "browser-report.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ passed: checks.length, report: "frontend/artifacts/browser-report.json", runId }));
} finally {
  socket?.close(); browser.kill("SIGTERM");
  await new Promise((resolve) => browser.exitCode !== null ? resolve() : browser.once("exit", resolve));
  await rm(profile, { recursive: true, force: true });
}
