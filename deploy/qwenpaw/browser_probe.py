#!/usr/bin/env python3
"""Real browser smoke for isolated login and official plugin route; no model work."""
import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
async def main():
    state = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
    credentials = json.loads((ROOT / 'var/qwenpaw-secrets/local-login.json').read_text())
    base = f"http://127.0.0.1:{state['port']}"
    async with async_playwright() as playwright:
        executable = os.environ.get('AGENT_LINE_CHROMIUM', str(Path.home() / '.cache/ms-playwright/chromium-1223/chrome-linux64/chrome'))
        browser = await playwright.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
        page = await browser.new_page(locale='zh-CN', viewport={'width': 1360, 'height': 900})
        await page.add_init_script("localStorage.setItem('language', 'zh'); localStorage.setItem('qwenpaw_sidebar_tools_mode', '2');")
        await page.goto(base, wait_until='domcontentloaded')
        await page.locator('#username').fill(credentials['username'])
        await page.locator('#password').fill(credentials['password'])
        await page.locator('button[type="submit"]').click()
        await page.get_by_text('工作流工作台', exact=True).wait_for(timeout=60000)
        await page.get_by_text('工作流工作台', exact=True).click()
        await page.get_by_role('heading', name='Agent Line 工作台').wait_for()
        await page.get_by_role('button', name='检查插件接口').click()
        await page.get_by_role('status').filter(has_text='插件接口已注册').wait_for()
        await page.screenshot(path=str(ROOT / 'var/qwenpaw/plugin-browser.png'), full_page=True)
        print('PASS: real browser login, Chinese menu, plugin route and authenticated health button; screenshot saved.')
        await browser.close()
asyncio.run(main())
