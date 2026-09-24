"""日志总线测试。"""

from __future__ import annotations

from pathlib import Path

from qlark.core.models import LogSource
from qlark.napcat.logbus import LogBus, png_to_data_url


def test_sequence_increases_and_since_filters(tmp_path: Path) -> None:
    bus = LogBus(capacity=100, log_dir=tmp_path, persist=False)
    bus.add("第一条")
    bus.add("第二条")
    bus.add("第三条")

    assert bus.latest_seq == 3
    texts = [line.text for line in bus.since(1)]
    assert texts == ["第二条", "第三条"]
    assert bus.since(3) == []


def test_multiline_text_is_split(tmp_path: Path) -> None:
    bus = LogBus(log_dir=tmp_path, persist=False)
    bus.add("第一行\n第二行\r\n第三行")
    assert [line.text for line in bus.since(0)] == ["第一行", "第二行", "第三行"]


def test_capacity_drops_oldest_lines(tmp_path: Path) -> None:
    bus = LogBus(capacity=5, log_dir=tmp_path, persist=False)
    for index in range(12):
        bus.add(f"line-{index}")
    texts = [line.text for line in bus.since(0)]
    assert len(texts) == 5
    assert texts[0] == "line-7"
    assert bus.latest_seq == 12


def test_napcat_source_infers_level(tmp_path: Path) -> None:
    bus = LogBus(log_dir=tmp_path, persist=False)
    bus.add("[error] 爆炸了", source=LogSource.NAPCAT.value)
    bus.add("[warn] 注意", source=LogSource.NAPCAT.value)
    bus.add("普通输出", source=LogSource.NAPCAT.value)
    levels = [line.level for line in bus.since(0)]
    assert levels == ["error", "warn", "info"]


def test_explicit_level_wins(tmp_path: Path) -> None:
    bus = LogBus(log_dir=tmp_path, persist=False)
    bus.add("[error] 但我说这是 debug", level="debug", source=LogSource.NAPCAT.value)
    assert bus.since(0)[0].level == "debug"


def test_clear_resets_buffer_but_not_sequence(tmp_path: Path) -> None:
    bus = LogBus(log_dir=tmp_path, persist=False)
    bus.add("a")
    bus.add("b")
    seq_before = bus.latest_seq
    bus.clear()
    assert bus.since(0) == []
    assert bus.latest_seq == seq_before, "序号必须继续递增，否则前端增量拉取会错乱"
    bus.add("c")
    assert bus.since(seq_before)[0].text == "c"


def test_subscribers_receive_batches(tmp_path: Path) -> None:
    bus = LogBus(log_dir=tmp_path, persist=False)
    received: list[str] = []
    bus.subscribe(lambda lines: received.extend(line.text for line in lines))
    bus.add("x")
    bus.add("y")
    assert bus.flush_to_listeners(0) == 2
    assert received == ["x", "y"]


def test_broken_subscriber_does_not_break_logging(tmp_path: Path) -> None:
    bus = LogBus(log_dir=tmp_path, persist=False)
    bus.subscribe(lambda lines: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.add("still fine")
    assert bus.since(0)[0].text == "still fine"


def test_persist_writes_file(tmp_path: Path) -> None:
    bus = LogBus(log_dir=tmp_path, persist=True)
    bus.add("落盘测试")
    bus.close()
    files = list(tmp_path.glob("qlark-*.log"))
    assert files, "应当生成日志文件"
    content = files[0].read_text(encoding="utf-8")
    assert "落盘测试" in content


def test_export_text(tmp_path: Path) -> None:
    bus = LogBus(log_dir=tmp_path, persist=False)
    bus.add("一行", source=LogSource.NAPCAT.value)
    exported = bus.export_text()
    assert "NapCat" in exported and "一行" in exported


def test_png_to_data_url() -> None:
    assert png_to_data_url(b"\x89PNG").startswith("data:image/png;base64,")
