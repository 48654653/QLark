"""和风天气（QWeather）客户端：城市名 → 城市 ID → 实时 + 3 天预报 → 一句人话。

为什么不塞给模型：天气是一次 HTTP + 一次字符串拼接就能得到的事实，让模型去
"记"它既贵又不准（会编温度）。所以这里只负责**取数 + 渲染成 60~120 字中文**，
剩下的交给调用方：面板的「测试连接」直接展示，回复流水线把它作为一段
``【工具结果】`` 注入上下文。

两个坑（都实测过，别再踩）：

1. **必须用账号专属的 API Host**（形如 ``xxxxxx.re.qweatherapi.com``，
   每个账号都不一样，去控制台「设置」里复制自己那一行）。
   公共域名 ``api.qweather.com`` / ``devapi.qweather.com`` 已经返回
   ``Invalid Host`` —— API Host 本身就是身份认证的一部分。
2. **必须带 gzip**。和风默认压缩响应，不声明 ``Accept-Encoding`` 会拿到乱码。
   ``requests`` 默认就会声明并自动解压，但自己拼 socket 时容易忘。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import requests

#: 单次请求超时（秒）。天气是"锦上添花"，绝不能让一条回复等它 10 秒。
DEFAULT_TIMEOUT = 4.0

#: 城市名最多个数换来的候选（和风 GeoAPI 的 number 参数）
LOOKUP_LIMIT = 3

#: 强关键词：命中就值得花一次极小调用去解析城市
_STRONG_HINTS = (
    "天气", "气温", "温度", "多少度", "几度", "天气预报",
    "下雨", "下雪", "雨伞", "带伞", "降温", "升温", "刮风", "霾", "紫外线",
    "好热", "好冷", "太热", "太冷", "真热", "真冷", "热死", "冷死",
)
#: 弱关键词：只有同时出现疑问/建议类词时才查（避免"热干面"这种误触发）
_WEAK_HINTS = ("热", "冷", "雨", "雪", "风", "晴", "阴", "穿")
_ASK_HINTS = ("吗", "呢", "么", "怎么", "咋", "多少", "要不要", "需要", "穿什么", "用不用", "该穿")


def looks_like_weather_question(text: str) -> bool:
    """粗筛：这句话值不值得为它多花一次模型调用。

    这层只是**省钱的前置过滤**，判断"要不要查"由模型做（见 pipeline 的
    意图调用）。所以宁可放进来多一次小调用，也别漏掉"明天要带伞吗"这种。
    """
    content = str(text or "")
    if not content.strip():
        return False
    if any(hint in content for hint in _STRONG_HINTS):
        return True
    return any(hint in content for hint in _WEAK_HINTS) and any(
        hint in content for hint in _ASK_HINTS
    )


class WeatherCache:
    """按城市缓存渲染好的那一句话。

    群里连着问三次北京，只该打一次和风 —— 额度是钱，天气 10 分钟内不会变。
    """

    def __init__(self, ttl: float = 600.0) -> None:
        self.ttl = float(ttl)
        self._data: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def get(self, city: str) -> str | None:
        import time

        key = str(city or "").strip()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            stamp, text = item
            if time.monotonic() - stamp > self.ttl:
                self._data.pop(key, None)
                return None
            return text

    def put(self, city: str, text: str) -> None:
        import time

        key = str(city or "").strip()
        with self._lock:
            self._data[key] = (time.monotonic(), str(text))

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

#: 和风的业务错误码 → 人话
CODE_HINTS: dict[str, str] = {
    "204": "该地区暂无数据",
    "400": "请求参数有误",
    "401": "凭据无效，检查 API Key",
    "402": "超出余额或额度",
    "403": "没有权限，或已超出调用限额",
    "404": "城市不存在",
    "429": "请求过于频繁，触发了限流",
    "500": "和风服务端出错",
}


class WeatherError(RuntimeError):
    """取天气失败。``code`` 是和风的业务码（拿得到的话）。"""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)

    @property
    def hint(self) -> str:
        return CODE_HINTS.get(str(self.code or ""), "天气查询失败")


@dataclass(frozen=True)
class City:
    id: str
    name: str
    adm1: str = ""
    adm2: str = ""

    @property
    def label(self) -> str:
        """给人看的名字：**只在真的会混淆时才补上级行政区**。

        GeoAPI 对「成都」会同时返回「成都（city）」和「龙泉驿（district）」，
        对「北京」会返回「北京（city）」和「海淀（district）」—— 一旦选错，
        机器人就会一本正经地报隔壁区的天气。所以这里：

        * 优先用 ``adm2``（"成都市" 对应 "成都"）判断同名，能对上就只留城市名；
        * 对不上才补 ``adm1``（"朝阳（辽宁省）" 这种真的需要区分的）。
        """
        name = self.name or ""
        region = self.adm1 or ""
        for candidate in (self.adm2, region):
            if candidate and _bare(candidate) == _bare(name):
                return name
        if not region:
            return name
        return f"{name}（{region}）"


def _bare(part: str) -> str:
    text = str(part or "").strip()
    for suffix in ("自治区", "特别行政区", "市", "省", "区", "县", "地区"):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def _location_score(item: dict[str, Any], query: str) -> tuple[int, int, int]:
    """GeoAPI 候选打分：市 > 区/县，名字完全一致优先，其次比 rank。"""
    kind = str(item.get("type") or "").strip().lower()
    exact = 1 if _bare(str(item.get("name") or "")) == _bare(query) else 0
    rank_text = str(item.get("rank") or "").strip()
    rank = int(rank_text) if rank_text.isdigit() else 0
    return (2 if kind == "city" else 0, exact, rank)


def render_brief(city: str, now: dict[str, Any], daily: list[dict[str, Any]] | None = None) -> str:
    """把和风的 JSON 压成一句中文（60~120 字）。

    **一定要在这里压**：原始 JSON 有 400~1700 字节，直接塞进 prompt 既贵又容易
    被模型抄错（它会把 ``windScale`` 读成温度）。这里只保留人会用的那几个数。
    """
    if not isinstance(now, dict) or not now:
        raise WeatherError("天气数据为空")

    def num(value: Any, fallback: str = "?") -> str:
        text = str(value or "").strip()
        return text if text else fallback

    head = f"{city}现在 {num(now.get('temp'))}℃"
    text = str(now.get("text") or "").strip()
    if text:
        head += f" {text}"
    feels = str(now.get("feelsLike") or "").strip()
    if feels and feels != str(now.get("temp") or "").strip():
        head += f"，体感 {feels}℃"
    wind = f"{str(now.get('windDir') or '').strip()}{str(now.get('windScale') or '').strip()}级"
    if wind.strip() != "级":
        head += f"，{wind}"
    humidity = str(now.get("humidity") or "").strip()
    if humidity:
        head += f"，湿度 {humidity}%"

    parts = [head + "。"]

    days = [day for day in (daily or []) if isinstance(day, dict)][:2]
    for index, day in enumerate(days):
        label = "今天" if index == 0 else "明天"
        day_text = str(day.get("textDay") or "").strip()
        night_text = str(day.get("textNight") or "").strip()
        if night_text and night_text != day_text:
            day_text = f"{day_text}转{night_text}" if day_text else night_text
        high = str(day.get("tempMax") or "").strip()
        low = str(day.get("tempMin") or "").strip()
        piece = f"{label} {day_text}".strip()
        if high and low:
            piece += f" {high}/{low}℃"
        rain = str(day.get("precip") or "").strip()
        try:
            if rain and float(rain) > 0:
                piece += f"、降水 {rain}mm"
        except ValueError:
            pass
        parts.append(piece + "。")

    return "".join(parts)


class QWeather:
    """极简和风客户端。只实现天气需要的三个接口。"""

    def __init__(
        self,
        host: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        host = str(host or "").strip()
        if not host:
            raise WeatherError("未配置和风 API Host")
        if not str(api_key or "").strip():
            raise WeatherError("未配置和风 API Key")
        if "://" not in host:
            host = "https://" + host
        self.base_url = host.rstrip("/")
        self.api_key = str(api_key).strip()
        self.timeout = float(timeout)
        self._session = session or requests.Session()

    # ------------------------------------------------------------------ 基础
    def _call(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.get(
                url,
                params=params,
                headers={"X-QW-Api-Key": self.api_key, "Accept-Encoding": "gzip"},
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise WeatherError(f"连不上和风（{self.timeout:.0f}s 超时）") from exc
        except requests.RequestException as exc:
            raise WeatherError(f"连不上和风：{exc}") from exc

        if response.status_code != 200:
            raise WeatherError(f"和风返回 HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise WeatherError("和风返回的内容不是 JSON（可能被代理/网络改写）") from exc
        if not isinstance(payload, dict):
            raise WeatherError("和风返回结构异常")

        code = str(payload.get("code") or "").strip()
        if code and code != "200":
            raise WeatherError(
                f"{CODE_HINTS.get(code, '和风返回错误')}（code={code}）", code=code
            )
        return payload

    # ------------------------------------------------------------------ 接口
    def lookup(self, city_name: str) -> City:
        """城市名 → 城市 ID。

        **必须优先选 ``type == "city"``**：GeoAPI 对「成都」会一起返回市辖区
        （龙泉驿/武侯…），按 rank 单纯取最大的那个会选中区，机器人就会报隔壁区
        的天气（实测踩过）。选择顺序：city 优先 → 名字完全一致 → rank 大。
        """
        name = str(city_name or "").strip()
        if not name:
            raise WeatherError("城市名为空")
        payload = self._call(
            "/geo/v2/city/lookup",
            {"location": name, "number": LOOKUP_LIMIT + 7, "lang": "zh"},
        )
        locations = [item for item in (payload.get("location") or []) if isinstance(item, dict)]
        if not locations:
            raise WeatherError(f"没有找到城市「{name}」", code="404")

        best = max(locations, key=lambda item: _location_score(item, name))
        return City(
            id=str(best.get("id") or ""),
            name=str(best.get("name") or name),
            adm1=str(best.get("adm1") or ""),
            adm2=str(best.get("adm2") or ""),
        )

    def now(self, city_id: str) -> dict[str, Any]:
        payload = self._call("/v7/weather/now", {"location": city_id, "lang": "zh"})
        now = payload.get("now")
        if not isinstance(now, dict):
            raise WeatherError("和风没有返回实时天气")
        return now

    def daily(self, city_id: str, days: int = 3) -> list[dict[str, Any]]:
        payload = self._call(f"/v7/weather/{int(days)}d", {"location": city_id, "lang": "zh"})
        daily = payload.get("daily")
        return [item for item in daily if isinstance(item, dict)] if isinstance(daily, list) else []

    # ------------------------------------------------------------------ 组合
    def brief(self, city_name: str) -> str:
        """端到端：城市名 → 一句中文天气。任何一步失败都抛 :class:`WeatherError`。"""
        city = self.lookup(city_name)
        return render_brief(city.label, self.now(city.id), self.daily(city.id))

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass


__all__ = [
    "CODE_HINTS",
    "City",
    "DEFAULT_TIMEOUT",
    "LOOKUP_LIMIT",
    "QWeather",
    "WeatherCache",
    "WeatherError",
    "looks_like_weather_question",
    "render_brief",
]
