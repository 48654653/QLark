"""界面布局自检：用**真实后端**打开界面，检查有没有内容被裁掉、该滚动的地方能不能滚。

这类问题（内容被 overflow:hidden 裁掉、flex 子项被压扁导致没有滚动条）人眼很难在
每个窗口尺寸下都发现，所以做成自动化检查。

用法::

    python tools/ui_check.py                    # 检查仪表盘
    python tools/ui_check.py --view login       # 打开登录模态再检查
    python tools/ui_check.py --view logs
    python tools/ui_check.py --view login --hold 20   # 检查完保持窗口 20 秒（供截图）

退出码：0 = 未发现裁切；1 = 发现被裁切的元素。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qlark.app import Application  # noqa: E402

#: 通用检查脚本：
#: 1) 找出所有"设了 overflow:hidden 但内容比容器大"的元素 —— 内容正在被裁掉
#: 2) 对几个关键滚动容器报告能否滚动
PROBE_JS = r"""
(function () {
  var out = { clipped: [], scrollers: [], viewport: {
    w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio
  } };

  function describe(el) {
    var name = el.tagName.toLowerCase();
    if (el.id) name += '#' + el.id;
    if (el.className && typeof el.className === 'string') {
      name += '.' + el.className.trim().split(/\s+/).join('.');
    }
    return name;
  }

  var all = document.querySelectorAll('body *');
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    var cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    var hiddenY = cs.overflowY === 'hidden';
    var hiddenX = cs.overflowX === 'hidden';
    if (!hiddenY && !hiddenX) continue;
    var overY = el.scrollHeight - el.clientHeight;
    var overX = el.scrollWidth - el.clientWidth;
    if ((hiddenY && overY > 2) || (hiddenX && overX > 2)) {
      out.clipped.push({
        el: describe(el),
        overflow: cs.overflowX + '/' + cs.overflowY,
        scrollH: el.scrollHeight, clientH: el.clientHeight,
        scrollW: el.scrollWidth, clientW: el.clientWidth
      });
    }
  }

  var selectors = ['.modal-body', '.page.active', '.log-list', '.log-scroll'];
  for (var j = 0; j < selectors.length; j++) {
    var node = document.querySelector(selectors[j]);
    if (!node) continue;
    var style = getComputedStyle(node);
    out.scrollers.push({
      sel: selectors[j],
      overflowY: style.overflowY,
      scrollH: node.scrollHeight, clientH: node.clientHeight,
      scrollTop: node.scrollTop,
      canScroll: node.scrollHeight > node.clientHeight + 1 && style.overflowY === 'auto'
    });
  }
  return JSON.stringify(out);
})()
"""

#: 打开指定视图的脚本
VIEW_SCRIPTS = {
    "login": "document.getElementById('loginBtn').click(); 'login'",
}
for _name in ("dashboard", "model", "persona", "memory", "logs"):
    VIEW_SCRIPTS[_name] = (
        f"document.querySelector('.nav-item[data-page=\"{_name}\"]').click(); '{_name}'"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ui_check", description="QLark 界面布局自检")
    parser.add_argument("--view", default="dashboard", choices=sorted(VIEW_SCRIPTS))
    parser.add_argument("--warmup", type=float, default=8.0, help="等待自动启动流程完成的秒数")
    parser.add_argument("--hold", type=float, default=0.0, help="检查完保持窗口的秒数（供截图）")
    parser.add_argument(
        "--js",
        default="",
        help="额外的 JS 表达式；结果会原样打印。用来验证数据是不是真的从后端绑上来了",
    )
    parser.add_argument(
        "--js-file",
        default="",
        help="从文件读取 JS 表达式（推荐：避免 PowerShell 吃掉引号）",
    )
    args = parser.parse_args(argv)

    js_source = args.js
    if args.js_file:
        js_source = Path(args.js_file).read_text(encoding="utf-8").strip()

    app = Application()
    result: dict = {}
    probe: dict = {}

    def on_ready(window) -> None:
        def worker() -> None:
            time.sleep(args.warmup)
            try:
                window.evaluate_js(VIEW_SCRIPTS[args.view])
                time.sleep(1.5)  # 等渲染 + 异步拉数据
                raw = window.evaluate_js(PROBE_JS)
                result.update(json.loads(raw))
                if js_source:
                    probe["value"] = window.evaluate_js(js_source)
            except Exception as exc:
                result["error"] = str(exc)
            if args.hold:
                time.sleep(args.hold)
            try:
                window.destroy()
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    app.run(on_ready=on_ready)

    if "error" in result:
        print(f"检查失败：{result['error']}")
        return 2

    viewport = result.get("viewport", {})
    print(f"视口：{viewport.get('w')}x{viewport.get('h')} CSS px（dpr={viewport.get('dpr')}）")
    print(f"视图：{args.view}")

    print("\n滚动容器：")
    for item in result.get("scrollers", []):
        flag = "可滚动" if item["canScroll"] else "不可滚动"
        print(
            f"  {item['sel']:<22} overflow-y={item['overflowY']:<7} "
            f"{item['scrollH']:>5}/{item['clientH']:<5} "
            f"scrollTop={item.get('scrollTop', 0):<5} {flag}"
        )

    clipped = result.get("clipped", [])
    if clipped:
        print(f"\n发现 {len(clipped)} 处内容被裁切：")
        for item in clipped:
            print(
                f"  [!!] {item['el']}\n"
                f"       overflow={item['overflow']} "
                f"垂直 {item['scrollH']}/{item['clientH']} "
                f"水平 {item['scrollW']}/{item['clientW']}"
            )
    else:
        print("\n未发现被裁切的内容")

    if js_source:
        print(f"\nJS 探针结果：\n  {probe.get('value')}")

    return 1 if clipped else 0


if __name__ == "__main__":
    sys.exit(main())
