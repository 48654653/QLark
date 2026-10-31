"""趣味功能：和风天气的配置校验、客户端与渲染。

三块分别对应三个坑：

1. **API Host 校验**：用户很容易把控制台那行 URL 整个粘进来，或者继续用
   已失效的公共域名 ``api.qweather.com`` —— 后者现在只会返回 ``Invalid Host``。
2. **密钥只回显打码值**：空白表示"不修改"，「清除」要显式标记。
3. **渲染在这一层做完**：原始 JSON 400~1700 字节，直接塞进 prompt 既贵又容易被
   模型抄错（把 ``windScale`` 读成温度）。这里压成 60~120 字中文。
"""

from __future__ import annotations

import pytest
import requests

from qlark.fun import config as fun_config
from qlark.fun.weather import QWeather, WeatherError, render_brief
from qlark.settings import Settings
from qlark.storage.db import Database

# 必须是**假的**凭据：这个文件要进公开仓库，真实 Host/Key 一律不能出现在这里。
KEY = "fake-key-0123456789abcdef"
HOST = "demo1234.re.qweatherapi.com"


@pytest.fixture
def settings(qlark_home) -> Settings:
    db = Database(qlark_home.data / "fun.db")
    try:
        yield Settings(db)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Host 归一化 / 校验
# --------------------------------------------------------------------------- #
def test_normalize_host_accepts_url_and_path() -> None:
    assert fun_config.normalize_host(f"https://{HOST}/") == HOST
    assert fun_config.normalize_host(f"  http://{HOST}/v7/weather  ") == HOST
    assert fun_config.normalize_host(HOST.upper()) == HOST
    assert fun_config.normalize_host("") == ""


def test_host_must_be_a_qweatherapi_domain() -> None:
    assert fun_config.is_valid_host(HOST)
    # 公共域名已经失效（返回 Invalid Host），必须在保存时就拦下来
    assert not fun_config.is_valid_host("api.qweather.com")
    assert not fun_config.is_valid_host("devapi.qweather.com")
    assert not fun_config.is_valid_host("")


# --------------------------------------------------------------------------- #
# 配置保存
# --------------------------------------------------------------------------- #
def test_apply_rejects_enable_without_credentials(settings: Settings) -> None:
    result = fun_config.apply(settings, {"fun_enabled": True})
    assert result["ok"] is False
    assert "qweather_host" in result["errors"]
    assert "qweather_key" in result["errors"]
    # 校验失败时一个字段都不该落库
    assert settings.get("fun_enabled") is False
    assert settings.get("qweather_key") == ""


def test_apply_rejects_public_domain(settings: Settings) -> None:
    result = fun_config.apply(
        settings,
        {"qweather_host": "api.qweather.com", "qweather_key": KEY},
    )
    assert result["ok"] is False
    assert "qweatherapi.com" in result["errors"]["qweather_host"]


def test_apply_rejects_short_key(settings: Settings) -> None:
    result = fun_config.apply(settings, {"qweather_host": HOST, "qweather_key": "abc"})
    assert result["ok"] is False
    assert "qweather_key" in result["errors"]


def test_apply_saves_and_masks_key(settings: Settings) -> None:
    result = fun_config.apply(
        settings,
        {
            "fun_enabled": True,
            "qweather_host": f"https://{HOST}/",
            "qweather_key": KEY,
            "qweather_city": "成都",
        },
    )
    assert result["ok"] is True
    assert settings.get("fun_enabled") is True
    assert settings.get("qweather_host") == HOST, "粘贴的 URL 要归一成域名"
    assert settings.get("qweather_key") == KEY
    assert settings.get("qweather_city") == "成都"
    # 回显只能是打码值
    masked = result["qweather_key_masked"]
    assert masked and KEY not in masked
    assert masked.startswith("fake") and masked.endswith("cdef")


def test_blank_key_keeps_the_saved_one(settings: Settings) -> None:
    fun_config.apply(settings, {"qweather_host": HOST, "qweather_key": KEY})
    fun_config.apply(settings, {"qweather_host": HOST, "qweather_key": "   "})
    assert settings.get("qweather_key") == KEY, "留空表示不修改"


def test_clear_flag_removes_the_key(settings: Settings) -> None:
    fun_config.apply(settings, {"qweather_host": HOST, "qweather_key": KEY})
    # 开着功能时清 key 会被拦下来（要先关掉），这里先关
    result = fun_config.apply(settings, {"fun_enabled": False, "qweather_key_clear": True})
    assert result["ok"] is True
    assert settings.get("qweather_key") == ""
    assert result["qweather_key_set"] is False


def test_resolve_prefers_form_values(settings: Settings) -> None:
    fun_config.apply(settings, {"qweather_host": HOST, "qweather_key": KEY, "qweather_city": "北京"})
    params = fun_config.resolve(settings, {"qweather_city": "上海"})
    assert params["city"] == "上海"
    assert params["host"] == HOST
    assert params["key"] == KEY


# --------------------------------------------------------------------------- #
# 开关与触发词表（v2.3.2：每个功能一个小开关，没有总开关）
# --------------------------------------------------------------------------- #
def test_weather_on_defaults_off(settings: Settings) -> None:
    assert fun_config.weather_on(settings) is False


def test_weather_on_falls_back_to_legacy_fun_enabled(settings: Settings) -> None:
    """v2.3.1 只有一个总开关 fun_enabled，新键没设时沿用旧值。"""
    settings.update(fun_enabled=True)
    assert fun_config.weather_on(settings) is True
    settings.update(weather_enabled=False)
    assert fun_config.weather_on(settings) is False, "新键一旦设过就以新键为准"


def test_keywords_default_when_unset(settings: Settings) -> None:
    from qlark.fun.weather import DEFAULT_KEYWORDS

    assert fun_config.keywords(settings) == list(DEFAULT_KEYWORDS)


def test_keywords_can_be_replaced(settings: Settings) -> None:
    settings.update(weather_keywords=["紫外线", "闷热"])
    assert fun_config.keywords(settings) == ["紫外线", "闷热"]


def test_normalize_keywords_trims_and_dedupes() -> None:
    words = fun_config.normalize_keywords(" 下雨 , 下雨， 带伞\n\n  紫外线  ")
    assert words == ["下雨", "带伞", "紫外线"]


def test_apply_rejects_empty_keywords_when_enabled(settings: Settings) -> None:
    result = fun_config.apply(
        settings,
        {
            "weather_enabled": True,
            "weather_keywords": [],
            "qweather_host": HOST,
            "qweather_key": KEY,
        },
    )
    assert result["ok"] is False
    assert "weather_keywords" in result["errors"]


def test_apply_saves_keywords_and_keeps_legacy_key_in_sync(settings: Settings) -> None:
    result = fun_config.apply(
        settings,
        {
            "weather_enabled": True,
            "weather_keywords": ["闷热", "紫外线"],
            "qweather_host": HOST,
            "qweather_key": KEY,
        },
    )
    assert result["ok"] is True
    assert settings.get("weather_keywords") == ["闷热", "紫外线"]
    assert settings.get("fun_enabled") is True, "旧键要同步写，回退版本时开关状态才对"
    assert result["weather_default_keywords"], "快照要带默认词表，供「恢复默认」用"


# --------------------------------------------------------------------------- #
# 关键词粗筛（只是省钱的前置过滤，判断"要不要查"由模型做）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    ["成都天气怎么样", "明天要带伞吗", "今天好热", "北京下雨了吗", "今天多少度", "要不要穿外套"],
)
def test_weather_hint_hits(text: str) -> None:
    from qlark.fun.weather import looks_like_weather_question

    assert looks_like_weather_question(text), text


@pytest.mark.parametrize("text", ["热干面真好吃", "我在学英语", "你会唱歌吗", "", "  "])
def test_weather_hint_misses(text: str) -> None:
    from qlark.fun.weather import looks_like_weather_question

    assert not looks_like_weather_question(text), text


def test_custom_keywords_are_respected() -> None:
    from qlark.fun.weather import looks_like_weather_question

    # 自定义表里没有"天气"了 → 不再触发
    assert not looks_like_weather_question("成都天气怎么样", ["紫外线"])
    assert looks_like_weather_question("今天紫外线强吗", ["紫外线"])


# --------------------------------------------------------------------------- #
# 从句子里猜城市（猜对就省掉一次模型调用，猜错也没关系 —— 还要 GeoAPI 验证）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("成都热死了", ["成都"]),
        ("成都天气怎么样", ["成都"]),
        ("北京下雨吗", ["北京"]),
        ("北京现在天气如何", ["北京"]),
        ("今天成都天气怎么样", ["成都"]),
        ("成都今天热死了", ["成都"]),
        ("明天北京降温吗", ["北京"]),
        ("石家庄下雪了", ["石家庄", "家庄"]),
    ],
)
def test_city_candidates_pick_the_place(text: str, expected: list[str]) -> None:
    from qlark.fun.weather import city_candidates

    assert city_candidates(text) == expected


@pytest.mark.parametrize("text", ["今天热死了", "热死了", "那明天呢", "哈哈哈哈", "菏泽"])
def test_city_candidates_do_not_guess_without_a_trigger(text: str) -> None:
    from qlark.fun.weather import city_candidates

    assert city_candidates(text) == []


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
NOW = {
    "temp": "14",
    "text": "霾",
    "feelsLike": "12",
    "windDir": "东北风",
    "windScale": "2",
    "humidity": "65",
}
DAILY = [
    {"textDay": "阴", "textNight": "小雨", "tempMax": "17", "tempMin": "10", "precip": "0.0"},
    {"textDay": "小雨", "textNight": "小雨", "tempMax": "12", "tempMin": "9", "precip": "6.9"},
]


def test_render_brief_keeps_the_facts_people_use() -> None:
    text = render_brief("北京", NOW, DAILY)
    assert "北京现在 14℃ 霾" in text
    assert "体感 12℃" in text
    assert "东北风2级" in text
    assert "今天 阴转小雨 17/10℃" in text
    assert "明天 小雨 12/9℃、降水 6.9mm" in text
    assert len(text) < 140, "要压成一句话，别把 JSON 直接丢给模型"


def test_render_brief_without_forecast() -> None:
    text = render_brief("北京", NOW, None)
    assert "北京现在" in text
    assert "今天" not in text


def test_render_brief_rejects_empty_now() -> None:
    with pytest.raises(WeatherError):
        render_brief("北京", {}, [])


# --------------------------------------------------------------------------- #
# 客户端（打桩，不走网络）
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.headers = {}

    def json(self) -> dict:
        return self._payload


class _Session:
    """按 URL 后缀分派响应；记录每次请求的参数与请求头。"""

    def __init__(self, mapping: dict[str, object]) -> None:
        self.mapping = mapping
        self.calls: list[dict] = []

    def get(self, url: str, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        for suffix, resp in self.mapping.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"没有为 {url} 准备响应")

    def close(self) -> None:
        pass


def _ok_session() -> _Session:
    return _Session(
        {
            "/geo/v2/city/lookup": _Resp(
                {
                    "code": "200",
                    "location": [
                        {"id": "101010100", "name": "北京", "adm1": "北京市", "adm2": "北京", "rank": "10"}
                    ],
                }
            ),
            "/v7/weather/now": _Resp({"code": "200", "now": NOW}),
            "/v7/weather/3d": _Resp({"code": "200", "daily": DAILY}),
        }
    )


def test_client_sends_key_in_header_and_renders() -> None:
    session = _ok_session()
    client = QWeather(HOST, KEY, session=session)
    text = client.brief("北京")

    assert "北京现在 14℃" in text
    assert all(call["headers"].get("X-QW-Api-Key") == KEY for call in session.calls)
    assert all(HOST in call["url"] for call in session.calls)
    assert session.calls[0]["params"]["location"] == "北京"


def test_client_requires_host_and_key() -> None:
    with pytest.raises(WeatherError):
        QWeather("", KEY)
    with pytest.raises(WeatherError):
        QWeather(HOST, "")


def test_client_maps_business_error_code() -> None:
    session = _Session(
        {"/geo/v2/city/lookup": _Resp({"code": "401", "error": {"status": 401}})}
    )
    client = QWeather(HOST, KEY, session=session)
    with pytest.raises(WeatherError) as info:
        client.brief("北京")
    assert info.value.code == "401"
    assert "凭据无效" in info.value.hint


def test_client_reports_unknown_city() -> None:
    session = _Session({"/geo/v2/city/lookup": _Resp({"code": "200"})})
    client = QWeather(HOST, KEY, session=session)
    with pytest.raises(WeatherError) as info:
        client.brief("不存在的地方")
    assert info.value.code == "404"


def test_lookup_prefers_city_over_district() -> None:
    """GeoAPI 会把市辖区一起返回来，选错就会报隔壁区的天气。

    实测：「成都」的候选里有「龙泉驿（district）」和「成都（city）」，
    单纯按 rank 取最大会选中龙泉驿。
    """
    session = _Session(
        {
            "/geo/v2/city/lookup": _Resp(
                {
                    "code": "200",
                    "location": [
                        {"id": "101270102", "name": "龙泉驿", "adm1": "四川省", "adm2": "成都市", "type": "district", "rank": "10"},
                        {"id": "101270101", "name": "成都", "adm1": "四川省", "adm2": "成都", "type": "city", "rank": "10"},
                    ],
                }
            )
        }
    )
    city = QWeather(HOST, KEY, session=session).lookup("成都")
    assert city.id == "101270101", "应当选 city，而不是 district"
    assert city.label == "成都"


def test_city_label_only_adds_region_when_ambiguous() -> None:
    from qlark.fun.weather import City

    assert City("1", "成都", "四川省", "成都").label == "成都"
    assert City("2", "朝阳", "辽宁省", "朝阳市").label == "朝阳"
    assert City("3", "龙泉驿", "四川省", "成都市").label == "龙泉驿（四川省）"


def test_client_maps_http_error() -> None:
    session = _Session({"/geo/v2/city/lookup": _Resp({}, status=500)})
    client = QWeather(HOST, KEY, session=session)
    with pytest.raises(WeatherError) as info:
        client.brief("北京")
    assert "HTTP 500" in str(info.value)


def test_client_maps_timeout() -> None:
    session = _Session({"/geo/v2/city/lookup": requests.Timeout("slow")})
    client = QWeather(HOST, KEY, session=session, timeout=1)
    with pytest.raises(WeatherError) as info:
        client.brief("北京")
    assert "超时" in str(info.value)
