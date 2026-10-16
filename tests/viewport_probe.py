"""多视口布局体检：用 Playwright 精确设置视口，找横向溢出元素并整页截图。"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")

URL = "http://127.0.0.1:8123/image"
OUT = Path(r"E:\dsh\Yhs-ApiDetect\.tmp_verify")
VIEWPORTS = [(390, 844), (414, 896), (520, 900), (768, 1000), (1440, 900)]

PROBE = """() => {
  const iw = window.innerWidth;
  const bad = [];
  document.querySelectorAll('*').forEach(function (el) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > iw + 0.5) {
      bad.push({tag: el.tagName.toLowerCase(), cls: String(el.className || ''), right: Math.round(r.right), w: Math.round(r.width)});
    }
  });
  return {iw: iw, client: document.documentElement.clientWidth, bodyScroll: document.body.scrollWidth, badCount: bad.length, bad: bad.slice(0, 10)};
}"""


def launch(p):
    try:
        return p.chromium.launch(channel="chrome")
    except Exception as exc:  # noqa: BLE001
        print("channel=chrome 不可用:", exc)
        return p.chromium.launch(executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe")


with sync_playwright() as p:
    browser = launch(p)
    report = []
    for width, height in VIEWPORTS:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(URL, wait_until="load")
        page.wait_for_timeout(600)
        info = page.evaluate(PROBE)
        shot = OUT / ("vp-%d.png" % width)
        page.screenshot(path=str(shot), full_page=True)
        info["viewport"] = width
        info["shot"] = str(shot)
        info["shotBytes"] = shot.stat().st_size if shot.exists() else 0
        report.append(info)
        page.close()
    browser.close()

for row in report:
    print(json.dumps(row, ensure_ascii=False))
