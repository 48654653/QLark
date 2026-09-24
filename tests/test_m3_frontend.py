"""前端真机回归：mock 数据绝不能泄漏进真实模式。

**背景**（这是一次真实事故）：页面选择会写进 ``localStorage``，所以"上次停在模型页"
时，下次启动的**早期**就会用内置 mock 数据把表单填满，并把 ``S.modelLoaded`` 置为 true。
切到真实后端后这个标志没有被清掉，于是模型页**永远不再重新加载**。

后果不止是显示错误 —— 用户点「应用」会把预览数据（例如 ``vision_model:
"deepseek-vl2"``、``max_tokens: 4096``）**写回真实配置**。这套测试就是锁死它。

做法：往临时数据库里写一组**一眼能认出的真实值**，然后用 ``tools/ui_check.py``
在真实 pywebview 里打开模型页，检查表单里出现的是这组真实值而不是 mock 值。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_CHECK = ROOT / "tools" / "ui_check.py"

#: 一组"绝不可能出现在 mock 里"的真实值
REAL_VALUES = {
    "model": "real-chat-model",
    "vision_model": "REAL-VISION-MODEL",
    "max_tokens": 7777,
    "temperature": 0.25,
    "memory_rounds": 33,
}

PROBE_JS = """
(function () {
  function v(id) { var el = document.getElementById(id); return el ? el.value : "<missing>"; }
  return JSON.stringify({
    model: v("mModel"),
    vision: v("mVision"),
    maxTokens: v("mMaxTokens"),
    temperature: v("mTemperature"),
    memoryRounds: v("bMemoryRounds"),
    activePage: (function () {
      var p = document.querySelector(".page.active");
      return p ? p.getAttribute("data-page") : "?";
    })(),
    undefinedCount: (document.body.innerText.match(/undefined/g) || []).length
  });
})()
"""


@pytest.fixture
def ui_env(qlark_home: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把真实值写进临时数据库，并准备探针文件。"""
    from qlark import paths
    from qlark.storage.db import Database

    # 注意：必须写在**真实应用会读的那个库**上（paths.db_path() 是 <data>/data/qlark.db，
    # 而单元测试夹具用的 <data>/qlark.db 是另一个文件）
    paths.ensure_data_dirs()
    db = Database(paths.db_path())
    db.update_config(
        {
            **REAL_VALUES,
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-abcdef1234567890",
            "qq_account": qlark_home.qq,
            "qq_nickname": "测试账号",
        }
    )
    db.close()

    monkeypatch.setenv("FAKE_NAPCAT_HAVE_CRED", "1")
    monkeypatch.setenv("FAKE_NAPCAT_SCAN_DELAY", "120")

    probe = tmp_path / "probe.js"
    probe.write_text(PROBE_JS, encoding="utf-8")
    return qlark_home, probe


def _run_ui_check(view: str, probe: Path) -> str:
    result = subprocess.run(
        [
            sys.executable,
            str(UI_CHECK),
            "--view",
            view,
            "--warmup",
            "10",
            "--js-file",
            str(probe),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(ROOT),
    )
    return result.stdout + result.stderr


def _extract_probe(output: str) -> dict:
    match = re.search(r"JS 探针结果：\s*\n\s*(\{.*\})", output)
    assert match, f"没有拿到探针输出：\n{output[-2000:]}"
    return json.loads(match.group(1))


def test_model_page_shows_real_backend_data(ui_env) -> None:
    """模型页必须显示后端真实数据，而不是内置 mock 预览数据。"""
    _home, probe = ui_env
    output = _run_ui_check("model", probe)
    data = _extract_probe(output)

    assert data["activePage"] == "model"
    # mock 里这几个值分别长这样：deepseek-vl2 / 4096 / 1 / 20
    assert data["model"] == REAL_VALUES["model"], "读到的是 mock 的模型名"
    assert data["vision"] == REAL_VALUES["vision_model"], "读到的是 mock 的视觉模型"
    assert data["maxTokens"] == str(REAL_VALUES["max_tokens"]), "读到的是 mock 的 max_tokens"
    assert data["temperature"] == str(REAL_VALUES["temperature"]), "读到的是 mock 的温度"
    assert data["memoryRounds"] == str(REAL_VALUES["memory_rounds"]), "读到的是 mock 的记忆轮数"
    assert data["undefinedCount"] == 0


def test_model_page_survives_repeated_launches(ui_env) -> None:
    """第二次启动会从 localStorage 恢复页面 —— 这正是触发该 bug 的路径。"""
    _home, probe = ui_env
    for attempt in range(2):
        output = _run_ui_check("model", probe)
        data = _extract_probe(output)
        assert data["activePage"] == "model"
        assert data["maxTokens"] == str(REAL_VALUES["max_tokens"]), (
            f"第 {attempt + 1} 次启动读到了 mock 数据（maxTokens={data['maxTokens']}）"
        )
