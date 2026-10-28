"""Check that Sim sign-out closes the embedded editor.

Credentials stay in memory. The report records only status facts.
"""
import asyncio
import json
import traceback
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var/gateway/iframe-logout-20261028"
BASE = "http://10.30.0.28:3194"
CHROME = (
    "/data01/home/zukang.xu/.cache/ms-playwright/"
    "chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell"
)


def log(message):
    with (OUT / "probe.log").open("a") as stream:
        stream.write(message + "\n")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "probe.log").write_text("")
    report = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=CHROME, args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        qwen = json.loads((ROOT / "var/qwenpaw-secrets/local-login.json").read_text())
        login = await context.request.post(BASE + "/qwenpaw/api/auth/login", data=qwen)
        report["qwenLoginStatus"] = login.status
        auth = await login.json()
        token = auth.get("access_token") or auth.get("token")
        if not token:
            raise RuntimeError("qwen_login_missing_token")
        page = await context.new_page()
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
        await page.get_by_role("button", name="打开原版画布").click()
        frame = page.frame_locator('iframe[title="Sim 原版工作流编辑器"]')
        email = frame.locator('input[name="email"]')
        editor = frame.get_by_text("New workflow", exact=True)
        await email.or_(editor).wait_for(timeout=20000)
        if await email.is_visible():
            sim = json.loads((ROOT / "var/sim/probe-account.json").read_text())
            await email.fill(sim["email"])
            await frame.locator('input[name="password"]').fill(sim["password"])
            await frame.get_by_role("button", name="Sign in", exact=True).click()
            log("signed-in")
        await editor.wait_for(timeout=20000)
        report["editorBeforeLogout"] = True
        logout = await context.request.post(
            BASE + "/api/auth/sign-out", data={}, headers={"Origin": BASE})
        report["simLogoutStatus"] = logout.status
        log("logout " + str(logout.status))
        await page.get_by_role("button", name="重新加载画布", exact=True).click()
        await email.or_(editor).wait_for(timeout=20000)
        report["loginVisibleAfterReload"] = await email.is_visible()
        report["editorVisibleAfterReload"] = await editor.is_visible()
        log("after " + str(report["loginVisibleAfterReload"]) + " " + str(report["editorVisibleAfterReload"]))
        await browser.close()
    report["passed"] = bool(
        report["qwenLoginStatus"] == 200
        and report["editorBeforeLogout"]
        and report["simLogoutStatus"] == 200
        and report["loginVisibleAfterReload"]
        and not report["editorVisibleAfterReload"])
    descriptor = (OUT / "report.json").open("w")
    try:
        json.dump(report, descriptor, ensure_ascii=False, indent=2)
        descriptor.write("\n")
    finally:
        descriptor.close()
    (OUT / "report.json").chmod(0o600)
    print(json.dumps({"passed": report["passed"],
                      "logout": report["simLogoutStatus"],
                      "loginVisible": report["loginVisibleAfterReload"],
                      "editorVisible": report["editorVisibleAfterReload"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "probe.log").open("a").write(traceback.format_exc())
        raise
