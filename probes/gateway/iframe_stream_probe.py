"""Observe the embedded Sim editor's own streaming connection.

Credentials stay in memory. The report records handshake facts, not secrets.
"""
import asyncio
import json
import time
import traceback
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var/gateway/iframe-stream-20261028"
BASE = "http://10.30.0.28:3194"
CHROME = (
    "/data01/home/zukang.xu/.cache/ms-playwright/"
    "chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell"
)


def log(message):
    with (OUT / "probe.log").open("a") as stream:
        stream.write(message + "\n")


def header_facts(headers):
    folded = {key.lower(): value for key, value in headers.items()}
    csp = folded.get("content-security-policy", "")
    ancestors = ""
    for part in csp.split(";"):
        if "frame-ancestors" in part:
            ancestors = part.strip()
    return {
        "xFrameOptions": folded.get("x-frame-options"),
        "frameAncestors": ancestors or None,
    }


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "probe.log").write_text("")
    sockets = []

    def on_socket(websocket):
        item = {"path": websocket.url.split("?", 1)[0], "opened": False, "received": []}
        sockets.append(item)

        def received(payload):
            text = payload if isinstance(payload, str) else ""
            if len(item["received"]) < 2 and text:
                item["received"].append(text[:24])
            if text.startswith("0"):
                item["opened"] = True

        websocket.on("framereceived", received)

    report = {"sockets": sockets}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        qwen = json.loads((ROOT / "var/qwenpaw-secrets/local-login.json").read_text())
        login = await context.request.post(BASE + "/qwenpaw/api/auth/login", data=qwen)
        report["qwenLoginStatus"] = login.status
        log("qwen " + str(login.status))
        auth = await login.json()
        token = auth.get("access_token") or auth.get("token")
        if not token:
            raise RuntimeError("qwen_login_missing_token")
        page = await context.new_page()
        page.on("websocket", on_socket)
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
        await page.goto(BASE + "/qwenpaw/")
        await page.evaluate(
            "(value)=>localStorage.setItem('qwenpaw_auth_token', value)", token)
        await page.reload()
        await page.wait_for_timeout(3000)
        await page.get_by_text("Console account", exact=True).click()
        await page.wait_for_timeout(800)
        await page.get_by_text("Settings", exact=True).click()
        await page.wait_for_timeout(1200)
        await page.get_by_text("工作流工作台", exact=True).first.click()
        await page.wait_for_timeout(800)
        documents = []

        def on_response(response):
            if response.request.resource_type != "document":
                return
            if "/workspace" not in response.url and "/login" not in response.url:
                return
            documents.append(response)

        page.on("response", on_response)
        await page.get_by_role("button", name="打开原版画布").click()
        frame = page.frame_locator('iframe[title="Sim 原版工作流编辑器"]')
        email = frame.locator('input[name="email"]')
        editor = frame.get_by_text("New workflow", exact=True)
        try:
            await email.or_(editor).wait_for(timeout=20000)
        except Exception:
            await page.screenshot(path=str(OUT / "iframe-timeout.png"))
            log("no login or editor")
            raise
        if await email.is_visible():
            sim = json.loads((ROOT / "var/sim/probe-account.json").read_text())
            await email.fill(sim["email"])
            await frame.locator('input[name="password"]').fill(sim["password"])
            await frame.get_by_role("button", name="Sign in", exact=True).click()
            log("signed-in")
        await editor.wait_for(timeout=20000)
        finals = [item for item in documents if item.status == 200]
        chosen = finals[-1] if finals else documents[-1]
        report["document"] = {"status": chosen.status, "urlPath": chosen.url.split("?", 1)[0].replace(BASE, ""),
                              **header_facts(chosen.headers)}
        report["documentStatuses"] = [item.status for item in documents]
        log("document " + str(report["document"]))
        report["editorVisible"] = True
        report["sameOrigin"] = all(
            item.url.startswith(BASE) for item in page.frames if item != page.main_frame)
        log("editor")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not any(
                item["opened"] and item["path"].endswith("/socket.io/") for item in sockets):
            await page.wait_for_timeout(300)
        report["firstHandshake"] = any(
            item["opened"] and item["path"].endswith("/socket.io/") for item in sockets)
        log("first " + str(report["firstHandshake"]))
        before = len(sockets)
        await page.get_by_role("button", name="重新加载画布", exact=True).click()
        await frame.get_by_text("New workflow", exact=True).wait_for(timeout=20000)
        log("reloaded")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not any(
                item["opened"] and item["path"].endswith("/socket.io/") for item in sockets[before:]):
            await page.wait_for_timeout(300)
        report["reloadHandshake"] = any(
            item["opened"] and item["path"].endswith("/socket.io/") for item in sockets[before:])
        report["reloadKeepsSession"] = True
        logout = await context.request.post(
            BASE + "/api/auth/sign-out", data={}, headers={"Origin": BASE})
        report["simLogoutStatus"] = logout.status
        await browser.close()
    report["passed"] = bool(
        report["qwenLoginStatus"] == 200
        and report["document"]["status"] == 200
        and report["document"]["xFrameOptions"] == "SAMEORIGIN"
        and report["document"]["frameAncestors"] == "frame-ancestors 'self'"
        and report["editorVisible"] and report["sameOrigin"]
        and report["firstHandshake"] and report["reloadHandshake"]
        and report["simLogoutStatus"] == 200)
    descriptor = (OUT / "report.json").open("w")
    try:
        json.dump(report, descriptor, ensure_ascii=False, indent=2)
        descriptor.write("\n")
    finally:
        descriptor.close()
    (OUT / "report.json").chmod(0o600)
    print(json.dumps({"passed": report["passed"], "first": report["firstHandshake"],
                      "reload": report["reloadHandshake"],
                      "xfo": report["document"]["xFrameOptions"],
                      "ancestors": report["document"]["frameAncestors"],
                      "sockets": len(sockets)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "probe.log").open("a").write(traceback.format_exc())
        raise
