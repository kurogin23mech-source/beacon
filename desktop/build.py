#!/usr/bin/env python3
"""Generate desktop/dist/index.html from server/static/index.html + desktop/layer.js.

Usage:
  python desktop/build.py
  # or from project root:
  python3 desktop/build.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
WEB = ROOT / 'server/static/index.html'
LAYER = ROOT / 'desktop/layer.js'
OUT = ROOT / 'desktop/dist/index.html'


def extract_css(web: str) -> str:
    m = re.search(r'<style>(.*?)</style>', web, re.DOTALL)
    if not m:
        sys.exit('ERROR: <style> block not found in web index.html')
    return m.group(0)  # includes <style> tags


def extract_shared_js(web: str) -> str:
    m = re.search(
        r'// @BUILD:SHARED-START\n(.*?)// @BUILD:SHARED-END',
        web, re.DOTALL
    )
    if not m:
        sys.exit('ERROR: @BUILD:SHARED-START / @BUILD:SHARED-END markers not found in web index.html')
    shared = m.group(1)
    # Remove @BUILD:SKIP-START ... @BUILD:SKIP-END blocks (web-only functions)
    shared = re.sub(r'// @BUILD:SKIP-START\n.*?// @BUILD:SKIP-END\n?', '', shared, flags=re.DOTALL)
    return shared.rstrip()


def build(web: str, layer: str) -> str:
    css = extract_css(web)
    shared_js = extract_shared_js(web)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Beacon Desktop</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=JetBrains+Mono:wght@300;400;500;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
{css}
</head>
<body>
<div class="container" id="app">
  <div class="loading"><div class="loading-bar"></div><div class="loading-text">Loading</div></div>
</div>
<div id="menu-root"></div>

<script>
// ============================================================
// DESKTOP LAYER (desktop/layer.js)
// ============================================================
{layer.strip()}

// ============================================================
// SHARED RENDERING (from server/static/index.html)
// ============================================================
{shared_js}
</script>
</body>
</html>
'''


def main():
    if not WEB.exists():
        sys.exit(f'ERROR: {WEB} not found')
    if not LAYER.exists():
        sys.exit(f'ERROR: {LAYER} not found')

    web = WEB.read_text(encoding='utf-8')
    layer = LAYER.read_text(encoding='utf-8')

    OUT.parent.mkdir(parents=True, exist_ok=True)
    result = build(web, layer)
    OUT.write_text(result, encoding='utf-8')

    lines = result.count('\n')
    print(f'Generated {OUT} ({lines} lines)')


if __name__ == '__main__':
    main()
