/* ============================================================
   QLark 云雀 · 前端交互逻辑
   - 无框架、无构建、无外链
   - 通过 window.pywebview.api 调用后端；不存在时自动降级为内置 mock
   ============================================================ */
(function () {
  'use strict';

  /* ---------------- 常量 ---------------- */
  var MAX_LOG_LINES = 3000;      // DOM 中最多保留的日志行数
  var STATE_POLL_MS = 5000;      // 真实后端下的状态轮询间隔（后端 push 为主，轮询兜底）
  var LOG_POLL_MS = 2000;        // 真实后端下的日志兜底拉取间隔
  var MOCK_LOG_MS = 2000;        // mock 模式假日志推送间隔
  var MOCK_COUNT_MS = 6000;      // mock 模式消息计数自增间隔（每约 6 秒 +1）

  var PAGE_META = {
    dashboard: ['仪表盘', '运行概览与今日统计'],
    model: ['模型配置', '对话 / 视觉 / 总结模型槽位'],
    persona: ['人设配置', '人设列表、提示词与权限白名单'],
    memory: ['记忆面板', '记忆轮数、自动总结与会话记忆'],
    logs: ['日志', 'QLark 与 NapCat 输出']
  };

  var PHASE_META = {
    idle: ['未启动', 'off'],
    env_check: ['环境自检中', 'busy'],
    env_error: ['环境异常', 'err'],
    starting: ['正在启动 NapCat', 'busy'],
    quick_login: ['快速登录中', 'busy'],
    waiting_scan: ['等待扫码登录', 'busy'],
    logged_in: ['已登录', 'ok'],
    network_setup: ['初始化网络', 'busy'],
    restarting: ['重启 NapCat', 'busy'],
    ready: ['就绪', 'ok'],
    stopped: ['已停止', 'off'],
    error: ['出错', 'err']
  };

  /* ---------------- DOM 快捷方式 ---------------- */
  function $(id) { return document.getElementById(id); }
  function mk(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function setText(node, text) {
    if (node && node.textContent !== String(text)) node.textContent = String(text);
  }
  function setClass(node, cls) { if (node && node.className !== cls) node.className = cls; }

  var dom = {
    html: document.documentElement,
    nav: $('nav'),
    pageTitle: $('pageTitle'),
    pageSub: $('pageSub'),
    themeBtn: $('themeBtn'),
    themeIconUse: $('themeIconUse'),
    loginBtn: $('loginBtn'),
    loginDot: $('loginDot'),
    loginText: $('loginText'),
    previewBadge: $('previewBadge'),
    sideDot: $('sideDot'),
    sideStatusText: $('sideStatusText'),

    stToken: $('stToken'), stTokenHint: $('stTokenHint'),
    stCalls: $('stCalls'), stCallHint: $('stCallHint'),
    stOnlineDot: $('stOnlineDot'), stOnline: $('stOnline'), stOnlineHint: $('stOnlineHint'),
    stQQ: $('stQQ'), stNick: $('stNick'),
    stMsg: $('stMsg'), stSessions: $('stSessions'),

    rtPhaseTag: $('rtPhaseTag'), rtProc: $('rtProc'), rtPid: $('rtPid'),
    rtUptime: $('rtUptime'), rtOnebot: $('rtOnebot'), rtPhase: $('rtPhase'), rtEnv: $('rtEnv'),
    rtPauseTag: $('rtPauseTag'), rtMsg: $('rtMsg'), rtMsgErr: $('rtMsgErr'), rtEvent: $('rtEvent'),
    sidePaused: $('sidePaused'),

    loginModal: $('loginModal'), modalClose: $('modalClose'), btnClose: $('btnClose'),
    modalDot: $('modalDot'), modalPhase: $('modalPhase'), modalMeta: $('modalMeta'),
    qrBox: $('qrBox'), qrImg: $('qrImg'), qrEmpty: $('qrEmpty'), qrCaption: $('qrCaption'), qrKv: $('qrKv'),
    errBox: $('errBox'), errText: $('errText'),
    envSummary: $('envSummary'), envList: $('envList'),
    btnStart: $('btnStart'), btnStop: $('btnStop'), btnRelogin: $('btnRelogin'),
    btnLogout: $('btnLogout'), btnEnvRecheck: $('btnEnvRecheck'),

    levelFilter: $('levelFilter'), logSearch: $('logSearch'), autoScroll: $('autoScroll'),
    btnClearLogs: $('btnClearLogs'), btnExportLogs: $('btnExportLogs'),
    logView: $('logView'), logJump: $('logJump'),
    logCount: $('logCount'), logFilterInfo: $('logFilterInfo'), logLive: $('logLive'),

    content: $('content'), toastWrap: $('toastWrap'),
    qaEnv: $('qaEnv'), qaRestart: $('qaRestart'), qaDataDir: $('qaDataDir'), qaRuntimeDir: $('qaRuntimeDir'),
    btnPause: $('btnPause'),

    /* ----- M3：模型配置页 ----- */
    mKeyTag: $('mKeyTag'),
    mBaseUrl: $('mBaseUrl'), eBaseUrl: $('eBaseUrl'),
    mApiKey: $('mApiKey'), mApiKeyClear: $('mApiKeyClear'), mApiKeyHint: $('mApiKeyHint'), eApiKey: $('eApiKey'),
    mModel: $('mModel'), eModel: $('eModel'),
    mVision: $('mVision'), eVision: $('eVision'),
    mSummary: $('mSummary'), eSummary: $('eSummary'),
    mMaxTokens: $('mMaxTokens'), eMaxTokens: $('eMaxTokens'),
    mTemperature: $('mTemperature'), eTemperature: $('eTemperature'),
    btnModelApply: $('btnModelApply'), btnModelTest: $('btnModelTest'), modelTestResult: $('modelTestResult'),

    napcatRestartTag: $('napcatRestartTag'), btnNapcatRestart: $('btnNapcatRestart'),
    nOnebotHost: $('nOnebotHost'), eOnebotHost: $('eOnebotHost'),
    nOnebotPort: $('nOnebotPort'), eOnebotPort: $('eOnebotPort'),
    nEventHost: $('nEventHost'), eEventHost: $('eEventHost'),
    nEventPort: $('nEventPort'), eEventPort: $('eEventPort'),
    nToken: $('nToken'), nTokenHint: $('nTokenHint'), eToken: $('eToken'),
    btnNapcatApply: $('btnNapcatApply'),

    acctCurrentTag: $('acctCurrentTag'), btnAccountsRefresh: $('btnAccountsRefresh'), acctList: $('acctList'),

    bMemoryRounds: $('bMemoryRounds'), eMemoryRounds: $('eMemoryRounds'),
    bMaxReplyChars: $('bMaxReplyChars'), eMaxReplyChars: $('eMaxReplyChars'),
    bQuoteGroup: $('bQuoteGroup'), bErrorNotice: $('bErrorNotice'), btnBehaviorApply: $('btnBehaviorApply'),

    /* ----- M3：人设配置页 ----- */
    personaCountTag: $('personaCountTag'), btnPersonaNew: $('btnPersonaNew'), personaList: $('personaList'),
    personaEditTitle: $('personaEditTitle'), personaSlotTag: $('personaSlotTag'),
    personaMsgCount: $('personaMsgCount'), btnPersonaDelete: $('btnPersonaDelete'),
    personaEmpty: $('personaEmpty'), personaEditor: $('personaEditor'),
    pName: $('pName'), ePName: $('ePName'),
    pPrompt: $('pPrompt'), pPromptCount: $('pPromptCount'), ePrompt: $('ePrompt'),
    pEnabled: $('pEnabled'),
    pModelOverride: $('pModelOverride'), ePModelOverride: $('ePModelOverride'),
    pTempOverride: $('pTempOverride'), ePTempOverride: $('ePTempOverride'),
    pTokensOverride: $('pTokensOverride'), ePTokensOverride: $('ePTokensOverride'),
    btnPersonaSave: $('btnPersonaSave'), btnPersonaResetMemory: $('btnPersonaResetMemory'),
    personaEditHint: $('personaEditHint'),
    permStateTag: $('permStateTag'), permWhitelistOnly: $('permWhitelistOnly'),
    permQqInput: $('permQqInput'), btnPermAdd: $('btnPermAdd'), permErr: $('permErr'), permChips: $('permChips')
  };

  /* ---------------- 运行时状态 ---------------- */
  var S = {
    app: null,             // AppState 快照
    usingMock: true,       // PyWebView 不存在时为 true
    theme: 'dark',
    themeFromUser: false,  // 用户是否手动选过主题（选过则忽略后端主题）
    logs: [],              // {seq, ts, level, source, text}
    lastSeq: 0,            // 已收到的最大 seq（用于去重）
    cursor: 0,             // get_logs 的请求游标（下次请求的 seq）
    level: 'all',
    keyword: '',
    autoScroll: true,
    stuck: true,           // 视图是否贴底
    pendingNew: 0,
    qrSrc: null,
    modalOpen: false,
    pauseBusy: false,      // set_paused 请求飞行中（按钮禁用，避免连点）
    timers: [],

    /* ----- M3 ----- */
    page: 'dashboard',     // 当前页面（用于状态推送时决定要不要刷新账号列表）
    model: null,           // get_model_config 归一化结果
    modelLoaded: false,
    modelTestBusy: false,
    napcatRestartRequired: false,
    accounts: null,        // {current, accounts: []}
    accountBusy: false,
    personas: [],          // list_personas 结果（已按 slot 升序）
    personaDefaultUid: null,
    personaUid: null,      // 当前选中的人设 uid
    perm: null             // {whitelist_only, whitelist}
  };

  /* ---------------- 工具函数 ---------------- */
  function pad2(n) { return n < 10 ? '0' + n : '' + n; }

  function toDate(ts) {
    if (ts === null || ts === undefined || ts === '') return null;
    var n = Number(ts);
    if (!isFinite(n) || n <= 0) {
      var d = new Date(ts);
      return isNaN(d.getTime()) ? null : d;
    }
    if (n < 1e11) n = n * 1000;   // 秒 → 毫秒
    var d2 = new Date(n);
    return isNaN(d2.getTime()) ? null : d2;
  }

  function fmtClock(ts) {
    var d = toDate(ts);
    if (!d) return '--:--:--';
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function fmtStamp(ts) {
    var d = toDate(ts);
    if (!d) return '----------- --:--:--';
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function fmtUptime(sec) {
    var s = Math.max(0, Math.floor(Number(sec) || 0));
    if (s === 0) return '—';
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    if (h > 0) return h + ' 小时 ' + m + ' 分';
    if (m > 0) return m + ' 分 ' + ss + ' 秒';
    return ss + ' 秒';
  }

  function fmtNum(n) {
    var v = Number(n);
    if (!isFinite(v)) v = 0;
    return v.toLocaleString('zh-CN');
  }

  // 相对时间（如「12 秒前」）
  function fmtRel(ts) {
    var d = toDate(ts);
    if (!d) return null;
    var sec = Math.round((Date.now() - d.getTime()) / 1000);
    if (!isFinite(sec)) return null;
    if (sec < 0) sec = 0;
    if (sec < 60) return sec + ' 秒前';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
    if (sec < 86400) return Math.floor(sec / 3600) + ' 小时前';
    return Math.floor(sec / 86400) + ' 天前';
  }

  // 长文本截断（错误原因一行内显示，约 60 字）
  function clip(text, max) {
    var s = (text === undefined || text === null) ? '' : String(text);
    return s.length > max ? (s.slice(0, max) + '…') : s;
  }

  function show(node, on) { if (node) node.hidden = !on; }

  function toast(message, kind) {
    var box = mk('div', 'toast' + (kind ? ' ' + kind : ''), message);
    dom.toastWrap.appendChild(box);
    setTimeout(function () {
      box.style.transition = 'opacity .2s ease';
      box.style.opacity = '0';
      setTimeout(function () { if (box.parentNode) box.parentNode.removeChild(box); }, 220);
    }, 3200);
  }

  function setUseIcon(sel) {
    if (!dom.themeIconUse) return;
    dom.themeIconUse.setAttribute('href', sel);
    try { dom.themeIconUse.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', sel); } catch (e) { /* ignore */ }
  }

  function defaultState() {
    return {
      phase: 'idle', phase_text: '未启动',
      qq: null, nickname: null, online: false,
      onebot_ready: false, napcat_running: false,
      pid: null, uptime_sec: 0, qrcode: null, last_error: null, theme: 'dark',
      // M2：后端可能尚未返回这两块，缺失时为 null，界面统一显示「未就绪」
      bot: null,
      event_server: null,
      stats: {
        date: '', prompt_tokens: 0, completion_tokens: 0, calls: 0,
        errors: 0, msg_in: 0, msg_out: 0, active_sessions: 0
      },
      env: { ok: false, checked_at: 0, items: [] }
    };
  }

  function numOr(v, fallback) {
    var n = Number(v);
    return isFinite(n) ? n : fallback;
  }

  function normalizeBot(raw) {
    var src = (raw && typeof raw === 'object') ? raw : {};
    var lastReply = Number(src.last_reply_at);
    return {
      running: !!src.running,
      paused: !!src.paused,
      queued: Math.max(0, numOr(src.queued, 0)),
      handled: Math.max(0, numOr(src.handled, 0)),
      replied: Math.max(0, numOr(src.replied, 0)),
      dropped: Math.max(0, numOr(src.dropped, 0)),
      last_error: (src.last_error === undefined || src.last_error === null || src.last_error === '')
        ? null : String(src.last_error),
      last_reply_at: (isFinite(lastReply) && lastReply > 0) ? lastReply : null
    };
  }

  function normalizeEventServer(raw) {
    var src = (raw && typeof raw === 'object') ? raw : {};
    var lastEvent = Number(src.last_event_at);
    var port = Number(src.port);
    return {
      running: !!src.running,
      host: src.host ? String(src.host) : null,
      port: (isFinite(port) && port > 0) ? port : null,
      last_event_at: (isFinite(lastEvent) && lastEvent > 0) ? lastEvent : null
    };
  }

  function normalizeState(raw) {
    var base = defaultState();
    if (!raw || typeof raw !== 'object') return base;
    var out = Object.assign({}, base, raw);
    out.stats = Object.assign({}, base.stats, (raw.stats && typeof raw.stats === 'object') ? raw.stats : {});
    var env = (raw.env && typeof raw.env === 'object') ? raw.env : {};
    out.env = {
      ok: !!env.ok,
      checked_at: env.checked_at || 0,
      items: Array.isArray(env.items) ? env.items : []
    };
    out.bot = (raw.bot && typeof raw.bot === 'object') ? normalizeBot(raw.bot) : null;
    out.event_server = (raw.event_server && typeof raw.event_server === 'object')
      ? normalizeEventServer(raw.event_server) : null;
    if (typeof out.phase !== 'string' || !PHASE_META[out.phase]) out.phase = 'idle';
    if (!out.phase_text) out.phase_text = PHASE_META[out.phase][0];
    return out;
  }

  /* ---------------- 后端 API 层 ---------------- */
  function realApi() {
    return (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
  }

  function apiCall(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    var real = realApi();
    if (!S.usingMock) {
      // 真实后端模式：后端没实现的接口必须报错，绝不能悄悄用 mock 顶上
      if (real && typeof real[name] === 'function') {
        try {
          return Promise.resolve(real[name].apply(real, args));
        } catch (e) {
          return Promise.reject(e);
        }
      }
      return Promise.reject(new Error('后端未提供接口：' + name));
    }
    if (MOCK.api[name]) {
      return Promise.resolve(MOCK.api[name].apply(MOCK.api, args));
    }
    return Promise.reject(new Error('接口不可用：' + name));
  }

  function runApi(name, okMsg) {
    return apiCall(name).then(function (res) {
      if (okMsg) toast(okMsg, 'ok');
      return res;
    }, function (err) {
      toast('调用 ' + name + ' 失败：' + (err && err.message ? err.message : err), 'err');
      throw err;
    });
  }

  /* ---------------- 主题 ---------------- */
  function applyTheme(theme, persist, fromUser) {
    S.theme = (theme === 'light') ? 'light' : 'dark';
    dom.html.setAttribute('data-theme', S.theme);
    setUseIcon(S.theme === 'dark' ? '#i-sun' : '#i-moon');
    if (fromUser) S.themeFromUser = true;
    if (persist) {
      try { localStorage.setItem('qlark.theme', S.theme); } catch (e) { /* ignore */ }
    }
  }

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem('qlark.theme'); } catch (e) { saved = null; }
    if (saved === 'dark' || saved === 'light') {
      S.themeFromUser = true;
      applyTheme(saved, false, true);
    } else {
      applyTheme('dark', false, false);
    }
  }

  function toggleTheme() {
    var next = (S.theme === 'dark') ? 'light' : 'dark';
    applyTheme(next, true, true);
    apiCall('set_theme', next)['catch'](function () { /* 主题仅为界面偏好，失败静默 */ });
  }

  /* ---------------- 状态渲染 ---------------- */
  function phaseInfo(phase) {
    return PHASE_META[phase] || ['未知阶段', 'off'];
  }

  function statusDotClass(st) {
    var info = phaseInfo(st.phase);
    if (st.online || st.phase === 'ready') return 'dot ok lg';
    if (info[1] === 'err') return 'dot err lg';
    if (info[1] === 'busy') return 'dot busy lg';
    return 'dot off lg';
  }

  function renderHeader(st) {
    var loggedIn = !!st.qq;
    setClass(dom.loginDot, loggedIn && st.online ? 'dot ok' : 'dot off');
    setText(dom.loginText, loggedIn ? ('QQ ' + st.qq) : '未登录');
    setClass(dom.loginBtn, loggedIn && st.online ? 'status-btn online' : 'status-btn');
    setClass(dom.sideDot, loggedIn && st.online ? 'dot ok' : 'dot off');
    setText(dom.sideStatusText, loggedIn ? ('QQ ' + st.qq + (st.nickname ? ' · ' + st.nickname : '')) : '未登录');
  }

  function renderStats(st) {
    var s = st.stats;
    setText(dom.stToken, fmtNum((Number(s.prompt_tokens) || 0) + (Number(s.completion_tokens) || 0)));
    setText(dom.stTokenHint, '输入 ' + fmtNum(s.prompt_tokens) + ' · 输出 ' + fmtNum(s.completion_tokens));
    setText(dom.stCalls, fmtNum(s.calls));
    setText(dom.stCallHint, '失败 ' + fmtNum(s.errors) + ' 次');

    var online = !!st.online;
    setClass(dom.stOnlineDot, online ? 'dot ok lg' : 'dot off lg');
    setText(dom.stOnline, online ? '在线' : '离线');
    setText(dom.stOnlineHint, online ? 'OneBot 链路正常' : (st.onebot_ready ? 'OneBot 就绪，等待登录' : 'NapCat 未就绪'));

    setText(dom.stQQ, st.qq ? String(st.qq) : '未登录');
    setText(dom.stNick, '昵称 ' + (st.nickname || '—'));
    setText(dom.stMsg, fmtNum(s.msg_in) + ' / ' + fmtNum(s.msg_out));
    setText(dom.stSessions, fmtNum(s.active_sessions));
  }

  function renderRuntime(st) {
    var info = phaseInfo(st.phase);
    var tagCls = 'tag';
    if (info[1] === 'ok') tagCls += ' ok';
    else if (info[1] === 'err') tagCls += ' err';
    else if (info[1] === 'busy') tagCls += ' warn';
    setClass(dom.rtPhaseTag, tagCls);
    setText(dom.rtPhaseTag, st.phase_text || info[0]);

    setClass(dom.rtProc, 'rt-value ' + (st.napcat_running ? 'ok' : 'dim'));
    setText(dom.rtProc, st.napcat_running ? '运行中' : '未运行');

    setText(dom.rtPid, st.pid ? String(st.pid) : '—');

    setClass(dom.rtUptime, 'rt-value ' + (st.uptime_sec > 0 ? '' : 'dim'));
    setText(dom.rtUptime, fmtUptime(st.uptime_sec));

    setClass(dom.rtOnebot, 'rt-value ' + (st.onebot_ready ? 'ok' : 'warn'));
    setText(dom.rtOnebot, st.onebot_ready ? '已就绪 · 127.0.0.1:3000' : '未就绪');

    setClass(dom.rtPhase, 'rt-value mono dim');
    setText(dom.rtPhase, (st.phase_text || info[0]) + '（' + st.phase + '）');

    var env = st.env;
    if (!env.checked_at && !env.items.length) {
      setClass(dom.rtEnv, 'rt-value dim');
      setText(dom.rtEnv, '未执行');
    } else {
      // 非关键项（例如"尚未保存账号"）不该报成"异常"，只作提示
      var failed = env.items.filter(function (it) { return !it.ok; }).length;
      var critical = env.items.filter(function (it) { return !it.ok && it.critical !== false; }).length;
      setClass(dom.rtEnv, 'rt-value ' + (critical === 0 ? 'ok' : 'err'));
      setText(dom.rtEnv, critical === 0
        ? ('全部通过 · ' + env.items.length + ' 项' + (failed ? '（' + failed + ' 项提示）' : ''))
        : (critical + ' 项异常 / 共 ' + env.items.length + ' 项'));
    }

    renderPipeline(st);
  }

  /* M2：消息流水线 / 事件服务（后端可能还没实现，一律做空值兜底） */
  function renderPipeline(st) {
    var bot = st.bot;
    if (!bot) {
      setClass(dom.rtMsg, 'rt-value dim');
      setText(dom.rtMsg, '未就绪');
      setText(dom.rtMsgErr, '');
      show(dom.rtMsgErr, false);
    } else {
      var text = '已回复 ' + fmtNum(bot.replied) + ' / 收到 ' + fmtNum(bot.handled) +
        '（丢弃 ' + fmtNum(bot.dropped) + '）';
      if (bot.queued > 0) text += ' · 排队 ' + fmtNum(bot.queued);
      var rel = bot.last_reply_at ? fmtRel(bot.last_reply_at) : null;
      if (rel) text += ' · 最近回复 ' + rel;
      setClass(dom.rtMsg, 'rt-value ' + (bot.paused ? 'warn' : (bot.running ? 'ok' : 'dim')));
      setText(dom.rtMsg, text);

      var err = bot.last_error ? clip(bot.last_error, 60) : '';
      setText(dom.rtMsgErr, err);
      show(dom.rtMsgErr, !!err);
    }

    var ev = st.event_server;
    if (!ev) {
      setClass(dom.rtEvent, 'rt-value dim');
      setText(dom.rtEvent, '未就绪');
    } else if (ev.running) {
      setClass(dom.rtEvent, 'rt-value ok');
      setText(dom.rtEvent, '监听中 · ' + (ev.host || '127.0.0.1') + ':' + (ev.port === null ? '—' : ev.port));
    } else {
      setClass(dom.rtEvent, 'rt-value warn');
      setText(dom.rtEvent, '未运行');
    }

    renderPause(st);
  }

  /* 暂停回复：按钮 / 徽章 / 左栏提示。状态一律以后端 onState 推送为准 */
  function renderPause(st) {
    var bot = st.bot;
    var running = !!(bot && bot.running);
    var paused = !!(bot && bot.paused);

    show(dom.rtPauseTag, paused);
    show(dom.sidePaused, paused);

    var btn = dom.btnPause;
    if (!btn) return;
    setText(btn, paused ? '恢复回复' : '暂停回复');
    setClass(btn, paused ? 'btn warn sm' : 'btn ghost sm');
    btn.disabled = !running || S.pauseBusy;
    btn.title = !running
      ? '机器人尚未启动'
      : (S.pauseBusy ? '正在切换暂停状态…'
        : (paused ? '点击恢复机器人自动回复' : '点击暂停机器人回复（消息仍会接收并记录）'));
  }

  function renderModal(st) {
    if (!S.modalOpen) return;
    setClass(dom.modalDot, statusDotClass(st));
    setText(dom.modalPhase, st.phase_text || phaseInfo(st.phase)[0]);
    setText(dom.modalMeta, 'QQ：' + (st.qq ? st.qq : '未登录') + ' · 阶段 ' + st.phase);

    var qr = st.qrcode;
    var url = (qr && qr.data_url) ? qr.data_url : null;
    if (url) {
      if (S.qrSrc !== url) {
        dom.qrImg.src = url;
        S.qrSrc = url;
      }
      dom.qrImg.hidden = false;
      dom.qrEmpty.hidden = true;
      setText(dom.qrCaption, '二维码更新于 ' + fmtClock(qr.mtime) + ' · 约 121 秒自动刷新');
    } else {
      if (dom.qrImg.getAttribute('src')) dom.qrImg.removeAttribute('src');
      S.qrSrc = null;
      dom.qrImg.hidden = true;
      dom.qrEmpty.hidden = false;
      setText(dom.qrCaption, '二维码保持白底黑块，未做任何滤镜处理');
    }

    var kv = dom.qrKv;
    kv.textContent = '';
    [
      ['登录阶段', st.phase_text || phaseInfo(st.phase)[0]],
      ['当前 QQ', st.qq ? String(st.qq) : '未登录'],
      ['昵称', st.nickname || '—'],
      ['NapCat 进程', st.napcat_running ? ('运行中 · PID ' + (st.pid || '—')) : '未运行'],
      ['OneBot 端口', st.onebot_ready ? '已就绪' : '未就绪'],
      ['运行时长', fmtUptime(st.uptime_sec)]
    ].forEach(function (row) {
      kv.appendChild(mk('span', 'k', row[0]));
      kv.appendChild(mk('span', 'v', row[1]));
    });

    if (st.last_error) {
      dom.errBox.hidden = false;
      setText(dom.errText, String(st.last_error));
    } else {
      dom.errBox.hidden = true;
      setText(dom.errText, '');
    }

    renderEnv(st.env);
  }

  function renderEnv(env) {
    var list = dom.envList;
    list.textContent = '';
    var items = env.items || [];

    if (!items.length) {
      var emptyCls = 'tag';
      if (env.checked_at) emptyCls += ' warn';
      setClass(dom.envSummary, emptyCls);
      setText(dom.envSummary, env.checked_at ? '无检查项' : '未执行');
      list.appendChild(mk('li', 'env-empty', env.checked_at ? '本次自检没有返回检查项' : '尚未执行环境自检'));
      return;
    }

    var failed = 0;
    var critical = 0;
    items.forEach(function (it) {
      var ok = !!it.ok;
      var isCritical = it.critical !== false;
      if (!ok) {
        failed++;
        if (isCritical) critical++;
      }
      // 关键项失败=红色叉；非关键项失败=橙色感叹号（只是提示，不阻塞启动）
      var tone = ok ? 'ok' : (isCritical ? 'bad' : 'warn');

      var li = mk('li', 'env-item ' + tone);
      var mark = mk('span', 'env-mark ' + tone);
      mark.appendChild(iconUse(ok ? '#i-check' : (isCritical ? '#i-cross' : '#i-warn')));
      li.appendChild(mark);

      var main = mk('div', 'env-main');
      main.appendChild(mk('div', 'env-label ' + tone, (it.label || it.key || '未命名检查项')));
      if (it.detail) main.appendChild(mk('div', 'env-detail', it.detail));
      li.appendChild(main);

      if (!ok && !isCritical) li.appendChild(mk('span', 'env-fix', '提示'));
      else if (!ok && it.fixable) li.appendChild(mk('span', 'env-fix', '可修复'));
      list.appendChild(li);
    });

    setClass(dom.envSummary, 'tag ' + (critical === 0 ? 'ok' : 'err'));
    setText(dom.envSummary, critical === 0
      ? ('全部通过 · ' + items.length + ' 项' + (failed ? '（' + failed + ' 项提示）' : ''))
      : (critical + ' 项异常 / 共 ' + items.length + ' 项'));
  }

  function iconUse(sel) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', sel);
    use.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', sel);
    svg.appendChild(use);
    return svg;
  }

  function renderAll() {
    var st = S.app || defaultState();
    renderHeader(st);
    renderStats(st);
    renderRuntime(st);
    if (S.modalOpen) renderModal(st);
    if (st.theme && !S.themeFromUser && (st.theme === 'dark' || st.theme === 'light') && st.theme !== S.theme) {
      applyTheme(st.theme, false, false);
    }
  }

  /* ---------------- 日志 ---------------- */
  function normalizeLine(raw, fallbackSeq) {
    if (!raw || typeof raw !== 'object') return null;
    var level = String(raw.level || 'info').toLowerCase();
    if (level !== 'debug' && level !== 'info' && level !== 'warn' && level !== 'error') level = 'info';
    var source = String(raw.source || 'QLark');
    var srcKey = /napcat/i.test(source) ? 'napcat' : 'qlark';
    var seq = (raw.seq === undefined || raw.seq === null) ? fallbackSeq : Number(raw.seq);
    if (!isFinite(seq)) seq = fallbackSeq;
    return {
      seq: seq,
      ts: raw.ts || raw.time || 0,
      level: level,
      source: source,
      srcKey: srcKey,
      text: (raw.text === undefined || raw.text === null) ? '' : String(raw.text)
    };
  }

  function pushLines(rawLines) {
    if (!rawLines) return;
    var arr = Array.isArray(rawLines) ? rawLines : [rawLines];
    var added = 0;
    for (var i = 0; i < arr.length; i++) {
      var line = normalizeLine(arr[i], S.lastSeq + 1);
      if (!line) continue;
      if (line.seq <= S.lastSeq) continue;   // 去重：push 与轮询可能重复投递同一行
      S.logs.push(line);
      S.lastSeq = line.seq;
      if (S.cursor <= line.seq) S.cursor = line.seq + 1;
      added++;
      if (matches(line)) appendLogRow(line);
    }
    if (S.logs.length > MAX_LOG_LINES) trimLogs();
    if (added > 0) {
      S.pendingNew += added;
      onNewLines();
    }
    updateLogFoot();
  }

  function matches(line) {
    if (S.level !== 'all' && line.level !== S.level) return false;
    if (S.keyword) {
      var kw = S.keyword.toLowerCase();
      if (line.text.toLowerCase().indexOf(kw) === -1 &&
        line.source.toLowerCase().indexOf(kw) === -1) return false;
    }
    return true;
  }

  function filtered() {
    return S.logs.filter(matches);
  }

  function appendLogRow(line) {
    var empty = dom.logView.querySelector('.log-empty');
    if (empty) dom.logView.removeChild(empty);

    var row = mk('div', 'log-row lv-' + line.level);
    row.appendChild(mk('span', 'log-time', fmtClock(line.ts)));
    row.appendChild(mk('span', 'log-level lv-' + line.level, line.level));
    row.appendChild(mk('span', 'log-source src-' + line.srcKey, line.source));

    var text = mk('span', 'log-text');
    fillHighlight(text, line.text);
    row.appendChild(text);

    dom.logView.appendChild(row);
  }

  function fillHighlight(container, text) {
    var kw = S.keyword;
    if (!kw) { container.textContent = text; return; }
    var lower = text.toLowerCase();
    var k = kw.toLowerCase();
    var from = 0, idx;
    while ((idx = lower.indexOf(k, from)) !== -1) {
      if (idx > from) container.appendChild(document.createTextNode(text.slice(from, idx)));
      container.appendChild(mk('mark', 'hl', text.slice(idx, idx + k.length)));
      from = idx + k.length;
      if (k.length === 0) break;
    }
    if (from < text.length) container.appendChild(document.createTextNode(text.slice(from)));
  }

  function renderLogsFull() {
    dom.logView.textContent = '';
    var list = filtered();
    if (!list.length) {
      var hint = S.logs.length ? '没有符合当前筛选条件的日志' : '暂无日志，等待输出…';
      dom.logView.appendChild(mk('div', 'log-empty', hint));
    } else {
      for (var i = 0; i < list.length; i++) appendLogRow(list[i]);
    }
    updateLogFoot();
    scrollToBottom();
  }

  function trimLogs() {
    var overflow = S.logs.length - MAX_LOG_LINES;
    if (overflow <= 0) return;
    S.logs.splice(0, overflow);
    var rows = dom.logView.querySelectorAll('.log-row');
    for (var i = 0; i < overflow && i < rows.length; i++) {
      if (rows[i].parentNode) rows[i].parentNode.removeChild(rows[i]);
    }
  }

  function updateLogFoot() {
    setText(dom.logCount, filtered().length + ' / ' + S.logs.length + ' 条');
    var levelText = (S.level === 'all') ? '全部级别' : ('仅 ' + S.level);
    if (S.keyword) levelText += ' · 关键字「' + S.keyword + '」';
    setText(dom.logFilterInfo, levelText);

    if (S.autoScroll && S.stuck) {
      setClass(dom.logLive, 'live-ok');
      setText(dom.logLive, '实时跟随中');
    } else if (!S.autoScroll) {
      setClass(dom.logLive, 'live-warn');
      setText(dom.logLive, '自动滚动已关闭');
    } else {
      setClass(dom.logLive, 'live-warn');
      setText(dom.logLive, '已暂停跟随');
    }
  }

  function onNewLines() {
    if (S.autoScroll && S.stuck) {
      scrollToBottom();
      S.pendingNew = 0;
      dom.logJump.hidden = true;
    } else {
      dom.logJump.hidden = false;
    }
    updateLogFoot();
  }

  function scrollToBottom() {
    dom.logView.scrollTop = dom.logView.scrollHeight;
    S.stuck = true;
    S.pendingNew = 0;
    dom.logJump.hidden = true;
  }

  function onLogScroll() {
    var v = dom.logView;
    var nearBottom = (v.scrollHeight - v.scrollTop - v.clientHeight) < 24;
    S.stuck = nearBottom;
    if (nearBottom) {
      S.pendingNew = 0;
      dom.logJump.hidden = true;
    } else {
      dom.logJump.hidden = false;
    }
    updateLogFoot();
  }

  /* ---------------- 日志数据加载 ---------------- */
  function loadLogsOnce() {
    return apiCall('get_logs', S.cursor).then(function (res) {
      if (!res || typeof res !== 'object') return;
      var lines = Array.isArray(res.lines) ? res.lines : [];
      pushLines(lines);
      if (res.next !== undefined && res.next !== null) {
        var next = Number(res.next);
        if (isFinite(next) && next > S.cursor) S.cursor = next;
      }
      if (!lines.length && !S.logs.length) updateLogFoot();
    }, function (err) {
      setClass(dom.logLive, 'live-warn');
      setText(dom.logLive, '日志拉取失败');
      if (window.console) console.warn('get_logs 失败：', err);
    });
  }

  function exportLogs() {
    var list = filtered();
    if (!list.length) { toast('当前没有可导出的日志', 'warn'); return; }
    var text = list.map(function (l) {
      return fmtStamp(l.ts) + ' | ' + l.level.toUpperCase() + ' | ' + l.source + ' | ' + l.text;
    }).join('\r\n') + '\r\n';
    var d = new Date();
    var name = 'qlark-log-' + d.getFullYear() + pad2(d.getMonth() + 1) + pad2(d.getDate()) +
      '-' + pad2(d.getHours()) + pad2(d.getMinutes()) + pad2(d.getSeconds()) + '.txt';
    try {
      var blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
      toast('已导出 ' + list.length + ' 条日志：' + name, 'ok');
    } catch (e) {
      toast('导出失败：' + (e && e.message ? e.message : e), 'err');
    }
  }

  /* ---------------- 页面切换 ---------------- */
  function switchPage(page) {
    if (!PAGE_META[page]) page = 'dashboard';
    S.page = page;
    var items = dom.nav.querySelectorAll('.nav-item');
    for (var i = 0; i < items.length; i++) {
      var on = items[i].getAttribute('data-page') === page;
      items[i].className = on ? 'nav-item active' : 'nav-item';
      if (on) items[i].setAttribute('aria-current', 'page');
      else items[i].removeAttribute('aria-current');
    }
    var pages = document.querySelectorAll('.page');
    for (var j = 0; j < pages.length; j++) {
      var active = pages[j].getAttribute('data-page') === page;
      pages[j].className = active ? 'page active' : 'page';
    }
    setText(dom.pageTitle, PAGE_META[page][0]);
    setText(dom.pageSub, PAGE_META[page][1]);
    try { localStorage.setItem('qlark.page', page); } catch (e) { /* ignore */ }
    if (page === 'logs') {
      S.stuck = true;
      scrollToBottom();
    } else if (page === 'model') {
      // 模型页：首次进入才拉配置，账号列表每次进入都刷新（登录状态可能已变）
      if (!S.modelLoaded) loadModelConfig();
      loadAccounts();
    } else if (page === 'persona') {
      loadPersonas();
    }
  }

  /* ============================================================
     M3：模型配置页
     - 后端契约字段缺失时一律兜底，不抛错、不显示 undefined
     - 「对话行为」与模型 API 共用 apply_model_config 一次提交
     ============================================================ */

  // 后端字段名 → [输入框 id, 错误提示 id]
  var MODEL_FIELDS = {
    base_url: ['mBaseUrl', 'eBaseUrl'],
    api_key: ['mApiKey', 'eApiKey'],
    model: ['mModel', 'eModel'],
    vision_model: ['mVision', 'eVision'],
    summary_model: ['mSummary', 'eSummary'],
    max_tokens: ['mMaxTokens', 'eMaxTokens'],
    temperature: ['mTemperature', 'eTemperature'],
    memory_rounds: ['bMemoryRounds', 'eMemoryRounds'],
    max_reply_chars: ['bMaxReplyChars', 'eMaxReplyChars'],
    reply_quote_group: ['bQuoteGroup', null],
    error_notice_private: ['bErrorNotice', null]
  };

  var NAPCAT_FIELDS = {
    onebot_host: ['nOnebotHost', 'eOnebotHost'],
    onebot_port: ['nOnebotPort', 'eOnebotPort'],
    event_host: ['nEventHost', 'eEventHost'],
    event_port: ['nEventPort', 'eEventPort'],
    token: ['nToken', 'eToken']
  };

  var PERSONA_FIELDS = {
    name: ['pName', 'ePName'],
    system_prompt: ['pPrompt', 'ePrompt'],
    enabled: ['pEnabled', null],
    model_override: ['pModelOverride', 'ePModelOverride'],
    temperature_override: ['pTempOverride', 'ePTempOverride'],
    max_tokens_override: ['pTokensOverride', 'ePTokensOverride']
  };

  function asStr(v) { return (v === undefined || v === null) ? '' : String(v); }
  function asBool(v, fallback) { return (v === undefined || v === null || v === '') ? fallback : !!v; }
  function hasText(v) { return v !== undefined && v !== null && String(v) !== ''; }

  function errText(err) {
    if (!err) return '未知错误';
    if (typeof err === 'string') return err;
    return err.message ? String(err.message) : String(err);
  }

  // 数字输入 → 数字；空 → null；非法 → 原样字符串（交给后端报错）
  function inputNum(el) {
    if (!el) return null;
    var raw = String(el.value).trim();
    if (raw === '') return null;
    var n = Number(raw);
    return isFinite(n) ? n : raw;
  }

  function markBad(input, bad) {
    if (!input) return;
    if (input.tagName === 'INPUT' && input.type === 'checkbox') return;
    var base = (input.tagName === 'TEXTAREA') ? 'textarea' : 'input';
    input.className = bad ? (base + ' bad') : base;
  }

  // 按字段渲染 errors，返回 {shown, unknown}；未被点名的字段自动清除错误
  function renderFieldErrors(fieldMap, errors) {
    var errs = (errors && typeof errors === 'object') ? errors : {};
    var shown = 0;
    var unknown = [];

    Object.keys(errs).forEach(function (key) {
      if (!hasText(errs[key])) return;
      var ids = fieldMap[key];
      if (!ids) { unknown.push(String(errs[key])); return; }
      var box = ids[1] ? $(ids[1]) : null;
      if (box) { box.textContent = String(errs[key]); show(box, true); }
      markBad(ids[0] ? $(ids[0]) : null, true);
      shown++;
    });

    Object.keys(fieldMap).forEach(function (key) {
      if (hasText(errs[key])) return;
      var ids = fieldMap[key];
      var box = ids[1] ? $(ids[1]) : null;
      if (box) { box.textContent = ''; show(box, false); }
      markBad(ids[0] ? $(ids[0]) : null, false);
    });

    return { shown: shown, unknown: unknown };
  }

  function clearFieldErrors(fieldMap) { renderFieldErrors(fieldMap, {}); }

  // 按钮加载态：禁用 + 换文案，恢复时还原原文案
  function setBusy(btn, busy, busyText) {
    if (!btn) return;
    if (busy) {
      if (btn.getAttribute('data-idle-text') === null) {
        btn.setAttribute('data-idle-text', btn.textContent);
      }
      btn.disabled = true;
      btn.textContent = busyText || '处理中…';
    } else {
      btn.disabled = false;
      var idle = btn.getAttribute('data-idle-text');
      if (idle !== null) btn.textContent = idle;
    }
  }

  function normalizeModelConfig(raw) {
    var s = (raw && typeof raw === 'object') ? raw : {};
    var noticeRaw = (s.error_notice_private === undefined) ? s.notify_error_private : s.error_notice_private;
    return {
      base_url: asStr(s.base_url),
      api_key_masked: asStr(s.api_key_masked),
      api_key_set: !!s.api_key_set,
      model: asStr(s.model),
      vision_model: asStr(s.vision_model),
      summary_model: asStr(s.summary_model),
      max_tokens: numOr(s.max_tokens, 4096),
      temperature: numOr(s.temperature, 1),
      onebot_host: asStr(s.onebot_host) || '127.0.0.1',
      onebot_port: numOr(s.onebot_port, 3000),
      event_host: asStr(s.event_host) || '127.0.0.1',
      event_port: numOr(s.event_port, 3001),
      token_masked: asStr(s.token_masked),
      memory_rounds: numOr(s.memory_rounds, 20),
      auto_summary: (s.auto_summary === undefined || s.auto_summary === null) ? null : !!s.auto_summary,
      reply_quote_group: asBool(s.reply_quote_group, false),
      max_reply_chars: numOr(s.max_reply_chars, 500),
      error_notice_private: asBool(noticeRaw, true),
      restart_required: !!s.restart_required
    };
  }

  function renderNapcatRestartTag() {
    show(dom.napcatRestartTag, !!S.napcatRestartRequired);
  }

  function renderModelConfig(cfg) {
    S.model = cfg;

    dom.mBaseUrl.value = cfg.base_url;
    dom.mModel.value = cfg.model;
    dom.mVision.value = cfg.vision_model;
    dom.mSummary.value = cfg.summary_model;
    dom.mMaxTokens.value = String(cfg.max_tokens);
    dom.mTemperature.value = String(cfg.temperature);

    // API Key 永不回填明文：只把掩码放到 placeholder
    dom.mApiKey.value = '';
    if (cfg.api_key_set) {
      dom.mApiKey.placeholder = cfg.api_key_masked || '已保存（留空表示不修改）';
      setClass(dom.mKeyTag, 'tag ok');
      setText(dom.mKeyTag, 'API Key 已配置');
      setText(dom.mApiKeyHint, '已保存 ' + (cfg.api_key_masked || '••••••••') +
        '；留空表示不修改，点「清除」可删除已保存的 Key');
    } else {
      dom.mApiKey.placeholder = 'sk-…';
      setClass(dom.mKeyTag, 'tag warn');
      setText(dom.mKeyTag, '未配置 API Key');
      setText(dom.mApiKeyHint, '尚未保存 API Key，填写后点「应用」才会生效');
    }

    dom.nOnebotHost.value = cfg.onebot_host;
    dom.nOnebotPort.value = String(cfg.onebot_port);
    dom.nEventHost.value = cfg.event_host;
    dom.nEventPort.value = String(cfg.event_port);
    dom.nToken.value = '';
    dom.nToken.placeholder = cfg.token_masked || '（未设置）';
    setText(dom.nTokenHint, cfg.token_masked
      ? ('已保存 ' + cfg.token_masked + '；留空表示不修改')
      : '留空表示不修改已保存的 Token');

    dom.bMemoryRounds.value = String(cfg.memory_rounds);
    dom.bMaxReplyChars.value = String(cfg.max_reply_chars);
    dom.bQuoteGroup.checked = !!cfg.reply_quote_group;
    dom.bErrorNotice.checked = !!cfg.error_notice_private;

    S.napcatRestartRequired = !!cfg.restart_required;
    renderNapcatRestartTag();
    clearFieldErrors(MODEL_FIELDS);
    clearFieldErrors(NAPCAT_FIELDS);
  }

  function loadModelConfig() {
    return apiCall('get_model_config').then(function (res) {
      S.modelLoaded = true;
      renderModelConfig(normalizeModelConfig(res));
    }, function (err) {
      S.modelLoaded = false;
      setClass(dom.mKeyTag, 'tag err');
      setText(dom.mKeyTag, '读取失败');
      setText(dom.mApiKeyHint, '读取模型配置失败：' + errText(err));
      if (window.console) console.warn('get_model_config 失败：', err);
    });
  }

  // 组装 apply_model_config 的 payload。
  // withModelFields=false 时只提交「对话行为」，但沿用同一个接口。
  function collectModelPayload(withModelFields) {
    var payload = {};
    if (withModelFields) {
      payload.base_url = dom.mBaseUrl.value.trim();
      var key = dom.mApiKey.value.trim();
      if (key !== '') payload.api_key = key;   // 留空 = 不修改
      payload.model = dom.mModel.value.trim();
      payload.vision_model = dom.mVision.value.trim();
      payload.summary_model = dom.mSummary.value.trim();
      payload.max_tokens = inputNum(dom.mMaxTokens);
      payload.temperature = inputNum(dom.mTemperature);
    }
    payload.memory_rounds = inputNum(dom.bMemoryRounds);
    payload.reply_quote_group = !!dom.bQuoteGroup.checked;
    payload.max_reply_chars = inputNum(dom.bMaxReplyChars);
    payload.error_notice_private = !!dom.bErrorNotice.checked;
    // 后端若返回过 auto_summary（记忆面板 M4 用），原样带回去，避免被重置
    if (S.model && S.model.auto_summary !== null) payload.auto_summary = S.model.auto_summary;
    return payload;
  }

  function applyModelConfig(withModelFields) {
    var payload = collectModelPayload(withModelFields);
    var btn = withModelFields ? dom.btnModelApply : dom.btnBehaviorApply;
    setBusy(btn, true, '应用中…');
    return apiCall('apply_model_config', payload).then(function (res) {
      setBusy(btn, false);
      if (res && res.ok === false) {
        var r = renderFieldErrors(MODEL_FIELDS, res.errors);
        var msgs = r.unknown.slice();
        if (!r.shown && !msgs.length && hasText(res.error)) msgs.push(String(res.error));
        if (!r.shown && !msgs.length && hasText(res.message)) msgs.push(String(res.message));
        toast(msgs.length ? msgs[0] : '配置未保存，请检查标红字段', 'err');
        return;
      }
      clearFieldErrors(MODEL_FIELDS);
      if (res && res.restart_required) {
        S.napcatRestartRequired = true;
        renderNapcatRestartTag();
      }
      toast('已应用', 'ok');
      loadModelConfig();   // 回显后端规范化后的值
    }, function (err) {
      setBusy(btn, false);
      toast('应用失败：' + errText(err), 'err');
    });
  }

  function clearApiKey() {
    if (!window.confirm('确定清除已保存的 API Key？清除后需要重新填写才能调用模型。')) return;
    var btn = dom.mApiKeyClear;
    setBusy(btn, true, '清除中…');
    // 必须带上 clear_api_key：后端把"空串"当作"没改"（否则输入框没动过就被清空了）
    apiCall('apply_model_config', { api_key: '', clear_api_key: true }).then(function (res) {
      setBusy(btn, false);
      if (res && res.ok === false) {
        var r = renderFieldErrors(MODEL_FIELDS, res.errors);
        toast(r.shown ? '清除失败，请检查标红字段' : '清除失败', 'err');
        return;
      }
      dom.mApiKey.value = '';
      toast('已清除 API Key', 'ok');
      loadModelConfig();
    }, function (err) {
      setBusy(btn, false);
      toast('清除失败：' + errText(err), 'err');
    });
  }

  function renderTestResult(res) {
    var el = dom.modelTestResult;
    if (!el) return;
    var r = (res && typeof res === 'object') ? res : {};
    var parts = [];
    var lat = Number(r.latency_ms);
    var hasLat = isFinite(lat) && lat >= 0;

    if (r.ok) {
      parts.push('✓ 连接正常');
      if (hasLat) parts.push(Math.round(lat) + 'ms');
      if (hasText(r.model)) parts.push(String(r.model));
      var pt = Number(r.prompt_tokens), ct = Number(r.completion_tokens);
      if (isFinite(pt) && isFinite(ct)) parts.push(pt + '+' + ct + ' tokens');
      setClass(el, 'test-result ok');
    } else {
      var hint = hasText(r.hint) ? String(r.hint)
        : (hasText(r.message) ? String(r.message) : '连接失败');
      var line = '✗ ' + hint + (hasText(r.kind) ? ('（' + r.kind + '）') : '');
      parts.push(line);
      if (hasLat && lat > 0) parts.push(Math.round(lat) + 'ms');
      setClass(el, 'test-result err');
    }
    setText(el, parts.join(' · '));
    show(el, true);
  }

  function testModel() {
    if (S.modelTestBusy) return;
    S.modelTestBusy = true;
    var btn = dom.btnModelTest;
    setBusy(btn, true, '测试中…');
    show(dom.modelTestResult, false);

    // 按界面上的内容测试（清空 = 用空值测试，后端会给出 config 类错误）；
    // API Key 例外：留空表示「继续用已保存的 Key」，因此省略该参数
    var payload = {
      base_url: dom.mBaseUrl.value.trim(),
      model: dom.mModel.value.trim()
    };
    var key = dom.mApiKey.value.trim();
    if (key !== '') payload.api_key = key;

    apiCall('test_model', payload).then(function (res) {
      S.modelTestBusy = false;
      setBusy(btn, false);
      renderTestResult(res);
    }, function (err) {
      S.modelTestBusy = false;
      setBusy(btn, false);
      renderTestResult({ ok: false, kind: 'connection', hint: errText(err) });
    });
  }

  function applyNapcatConfig() {
    var payload = {
      onebot_host: dom.nOnebotHost.value.trim(),
      onebot_port: inputNum(dom.nOnebotPort),
      event_host: dom.nEventHost.value.trim(),
      event_port: inputNum(dom.nEventPort)
    };
    var token = dom.nToken.value.trim();
    if (token !== '') payload.token = token;   // 留空 = 不修改

    var btn = dom.btnNapcatApply;
    setBusy(btn, true, '应用中…');
    apiCall('apply_napcat_config', payload).then(function (res) {
      setBusy(btn, false);
      if (res && res.ok === false) {
        var r = renderFieldErrors(NAPCAT_FIELDS, res.errors);
        var msgs = r.unknown.slice();
        if (!r.shown && !msgs.length && hasText(res.error)) msgs.push(String(res.error));
        toast(msgs.length ? msgs[0] : '配置未保存，请检查标红字段', 'err');
        return;
      }
      clearFieldErrors(NAPCAT_FIELDS);
      // 契约里成功会带 restart_required: true；只有明确 false 才不提示重启
      S.napcatRestartRequired = !(res && res.restart_required === false);
      renderNapcatRestartTag();
      toast(S.napcatRestartRequired ? '已保存，重启 NapCat 后生效' : '已应用',
        S.napcatRestartRequired ? 'warn' : 'ok');
      loadModelConfig();
    }, function (err) {
      setBusy(btn, false);
      toast('应用失败：' + errText(err), 'err');
    });
  }

  function restartNapcat() {
    if (!window.confirm('重启 NapCat 会短暂断开连接，确定继续？')) return;
    var btn = dom.btnNapcatRestart;
    setBusy(btn, true, '重启中…');
    apiCall('restart_napcat').then(function () {
      setBusy(btn, false);
      S.napcatRestartRequired = false;
      renderNapcatRestartTag();
      toast('NapCat 重启指令已发送', 'ok');
    }, function (err) {
      setBusy(btn, false);
      toast('重启失败：' + errText(err), 'err');
    });
  }

  /* ---------------- M3：登录账号 ---------------- */
  function normalizeAccounts(raw) {
    var s = (raw && typeof raw === 'object') ? raw : {};
    var list = Array.isArray(s.accounts) ? s.accounts : [];
    var out = [];
    list.forEach(function (item) {
      var a = (item && typeof item === 'object') ? item : {};
      var qq = asStr(a.qq);
      if (qq === '') return;
      out.push({
        qq: qq,
        nickname: hasText(a.nickname) ? String(a.nickname) : null,
        has_credential: !!a.has_credential,
        known_by_qlark: !!a.known_by_qlark
      });
    });
    return { current: hasText(s.current) ? String(s.current) : null, accounts: out };
  }

  function loadAccounts() {
    setClass(dom.acctCurrentTag, 'tag');
    setText(dom.acctCurrentTag, '读取中…');
    return apiCall('list_accounts').then(function (res) {
      S.accounts = normalizeAccounts(res);
      renderAccounts();
    }, function (err) {
      S.accounts = null;
      setClass(dom.acctCurrentTag, 'tag err');
      setText(dom.acctCurrentTag, '读取失败');
      dom.acctList.textContent = '';
      dom.acctList.appendChild(mk('li', 'acct-empty', '账号列表读取失败：' + errText(err)));
    });
  }

  function renderAccounts() {
    var data = S.accounts || { current: null, accounts: [] };
    var cur = data.current;
    var list = dom.acctList;

    if (cur) {
      setClass(dom.acctCurrentTag, 'tag ok');
      setText(dom.acctCurrentTag, '当前 ' + cur);
    } else {
      setClass(dom.acctCurrentTag, 'tag');
      setText(dom.acctCurrentTag, '未登录');
    }

    list.textContent = '';
    if (!data.accounts.length) {
      list.appendChild(mk('li', 'acct-empty', '未检测到本机已登录过的 QQ'));
      return;
    }

    data.accounts.forEach(function (a) {
      var li = mk('li', 'acct-item' + (a.qq === cur ? ' current' : ''));
      var main = mk('div', 'acct-main');
      main.appendChild(mk('span', 'acct-qq mono', a.qq));
      main.appendChild(mk('span', 'acct-nick', a.nickname || '昵称未获取'));
      if (a.has_credential && a.known_by_qlark) main.appendChild(mk('span', 'tag ok', '可免扫码'));
      else if (a.has_credential) main.appendChild(mk('span', 'tag warn', '有凭据'));
      else main.appendChild(mk('span', 'tag', '需扫码'));
      if (a.qq === cur) main.appendChild(mk('span', 'tag ok', '当前登录'));
      li.appendChild(main);

      var btn = mk('button', 'btn ghost sm', '用这个账号登录');
      btn.type = 'button';
      btn.disabled = !!S.accountBusy;
      btn.addEventListener('click', function () { loginAs(a.qq); });
      li.appendChild(btn);
      list.appendChild(li);
    });
  }

  function loginAs(qq) {
    if (S.accountBusy) return;
    S.accountBusy = true;
    renderAccounts();
    apiCall('login_as', String(qq)).then(function (res) {
      S.accountBusy = false;
      if (res && res.ok === false) {
        var msg = hasText(res.error) ? String(res.error) : (hasText(res.message) ? String(res.message) : '未知原因');
        toast('切换账号失败：' + msg, 'err');
      } else {
        toast('已开始用 ' + qq + ' 登录，请到「登录 / 连接」面板查看进度', 'ok');
      }
      loadAccounts();
    }, function (err) {
      S.accountBusy = false;
      renderAccounts();
      toast('切换账号失败：' + errText(err), 'err');
    });
  }

  /* ============================================================
     M3：人设配置页
     ============================================================ */
  function normalizePersonas(raw) {
    var s = (raw && typeof raw === 'object') ? raw : {};
    var list = Array.isArray(s.personas) ? s.personas : [];
    var out = list.map(function (item, i) {
      var p = (item && typeof item === 'object') ? item : {};
      return {
        uid: numOr(p.uid, i + 1),
        slot: numOr(p.slot, i + 1),
        name: asStr(p.name),
        avatar_path: asStr(p.avatar_path),
        system_prompt: asStr(p.system_prompt),
        enabled: asBool(p.enabled, true),
        model_override: hasText(p.model_override) ? String(p.model_override) : null,
        temperature_override: hasText(p.temperature_override) ? numOr(p.temperature_override, null) : null,
        max_tokens_override: hasText(p.max_tokens_override) ? numOr(p.max_tokens_override, null) : null,
        can_delete: asBool(p.can_delete, true),
        message_count: Math.max(0, numOr(p.message_count, 0))
      };
    });
    out.sort(function (a, b) { return a.slot - b.slot; });
    return { default_uid: numOr(s.default_uid, null), personas: out };
  }

  function currentPersona() {
    var found = null;
    S.personas.forEach(function (p) { if (p.uid === S.personaUid) found = p; });
    return found;
  }

  function loadPersonas(keepUid) {
    return apiCall('list_personas').then(function (res) {
      var data = normalizePersonas(res);
      S.personas = data.personas;
      S.personaDefaultUid = data.default_uid;

      var want = (keepUid === undefined || keepUid === null) ? S.personaUid : keepUid;
      var found = null;
      S.personas.forEach(function (p) { if (p.uid === want) found = p; });
      if (!found) found = S.personas.length ? S.personas[0] : null;
      S.personaUid = found ? found.uid : null;

      renderPersonaList();
      renderPersonaEditor();
      loadPersonaPermission();
    }, function (err) {
      S.personas = [];
      S.personaUid = null;
      setClass(dom.personaCountTag, 'tag err');
      setText(dom.personaCountTag, '读取失败');
      dom.personaList.textContent = '';
      dom.personaList.appendChild(mk('li', 'persona-empty-row', '人设列表读取失败：' + errText(err)));
      renderPersonaEditor();
      if (window.console) console.warn('list_personas 失败：', err);
    });
  }

  function renderPersonaList() {
    var list = dom.personaList;
    list.textContent = '';
    setClass(dom.personaCountTag, 'tag');
    setText(dom.personaCountTag, S.personas.length + ' / 99');

    if (!S.personas.length) {
      list.appendChild(mk('li', 'persona-empty-row', '还没有任何人设，点「＋ 新建人设」创建一个'));
      return;
    }

    S.personas.forEach(function (p) {
      var row = mk('li', 'persona-row' + (p.uid === S.personaUid ? ' active' : '') +
        (p.enabled ? '' : ' disabled'));
      row.setAttribute('data-uid', String(p.uid));
      row.appendChild(mk('span', 'persona-slot', '人设' + p.slot));
      row.appendChild(mk('span', 'persona-name' + (p.name ? '' : ' unnamed'), p.name || '（未命名）'));
      // 停用的人设切不过去，必须在列表上就能看出来，否则用户只会觉得 /person 没用
      if (!p.enabled) row.appendChild(mk('span', 'tag warn persona-flag', '已停用'));
      row.appendChild(mk('span', 'persona-msgs', fmtNum(p.message_count) + ' 条'));
      row.title = (p.name || '（未命名）') + '（人设' + p.slot + '）' +
        (p.uid === S.personaDefaultUid ? ' · 默认' : '') +
        (p.enabled ? '' : ' · 已停用，/person 切不过去');
      row.addEventListener('click', function () { selectPersona(p.uid); });
      list.appendChild(row);
    });
  }

  function renderPersonaEditor() {
    var p = currentPersona();
    clearFieldErrors(PERSONA_FIELDS);

    if (!p) {
      show(dom.personaEditor, false);
      show(dom.personaEmpty, true);
      show(dom.personaSlotTag, false);
      setText(dom.personaEditTitle, '人设编辑');
      setText(dom.personaMsgCount, '');
      setText(dom.personaEditHint, '');
      dom.btnPersonaDelete.disabled = true;
      dom.btnPersonaDelete.title = '请先选择一个人设';
      dom.btnPersonaSave.disabled = true;
      dom.btnPersonaResetMemory.disabled = true;
      return;
    }

    show(dom.personaEmpty, false);
    show(dom.personaEditor, true);
    show(dom.personaSlotTag, true);
    setText(dom.personaSlotTag, '人设' + p.slot);
    setText(dom.personaEditTitle, p.name || '（未命名）');
    setText(dom.personaMsgCount, '已记录 ' + fmtNum(p.message_count) + ' 条记忆');

    dom.pName.value = p.name;
    dom.pPrompt.value = p.system_prompt;
    updatePromptCount();
    dom.pEnabled.checked = !!p.enabled;
    dom.pModelOverride.value = p.model_override || '';
    dom.pTempOverride.value = (p.temperature_override === null) ? '' : String(p.temperature_override);
    dom.pTokensOverride.value = (p.max_tokens_override === null) ? '' : String(p.max_tokens_override);

    dom.btnPersonaSave.disabled = false;
    dom.btnPersonaResetMemory.disabled = false;
    dom.btnPersonaDelete.disabled = !p.can_delete;
    dom.btnPersonaDelete.title = p.can_delete ? '删除该人设（需要二次确认）' : '出厂默认人设不可删除';
    setText(dom.personaEditHint, p.uid === S.personaDefaultUid ? '当前默认人设' : '');
  }

  function selectPersona(uid) {
    if (S.personaUid === uid) return;
    S.personaUid = uid;
    renderPersonaList();
    renderPersonaEditor();
  }

  function updatePromptCount() {
    setText(dom.pPromptCount, dom.pPrompt.value.length + ' 字');
  }

  function createPersona() {
    var btn = dom.btnPersonaNew;
    setBusy(btn, true, '新建中…');
    apiCall('create_persona').then(function (res) {
      setBusy(btn, false);
      if (res && res.ok === false) {
        toast('新建人设失败：' + (hasText(res.error) ? String(res.error) : '未知原因'), 'err');
        return;
      }
      var uid = (res && res.uid !== undefined && res.uid !== null) ? res.uid : null;
      toast(hasText(res && res.slot) ? ('已新建人设' + res.slot) : '已新建人设', 'ok');
      loadPersonas(uid);
    }, function (err) {
      setBusy(btn, false);
      toast('新建人设失败：' + errText(err), 'err');
    });
  }

  function savePersona() {
    var p = currentPersona();
    if (!p) return;
    var payload = {
      name: dom.pName.value.trim(),
      system_prompt: dom.pPrompt.value,
      enabled: !!dom.pEnabled.checked,
      model_override: dom.pModelOverride.value.trim(),
      temperature_override: inputNum(dom.pTempOverride),
      max_tokens_override: inputNum(dom.pTokensOverride)
    };
    var btn = dom.btnPersonaSave;
    setBusy(btn, true, '保存中…');
    apiCall('update_persona', p.uid, payload).then(function (res) {
      setBusy(btn, false);
      if (res && res.ok === false) {
        var r = renderFieldErrors(PERSONA_FIELDS, res.errors);
        var msgs = r.unknown.slice();
        if (!r.shown && !msgs.length && hasText(res.error)) msgs.push(String(res.error));
        toast(msgs.length ? msgs[0] : '保存失败，请检查标红字段', 'err');
        return;
      }
      clearFieldErrors(PERSONA_FIELDS);
      toast('已保存', 'ok');
      loadPersonas(p.uid);
    }, function (err) {
      setBusy(btn, false);
      toast('保存失败：' + errText(err), 'err');
    });
  }

  function deletePersona() {
    var p = currentPersona();
    if (!p) return;
    if (!p.can_delete) { toast('出厂默认人设不可删除', 'warn'); return; }
    var msg = '删除「' + (p.name || '（未命名）') + '」（人设' + p.slot + '）？\n\n' +
      '删除后：正在使用它的人/群会切回人设1（无提示）；' +
      '该人设的全部记忆将被永久删除（' + fmtNum(p.message_count) + ' 条）。确定删除？';
    if (!window.confirm(msg)) return;

    var btn = dom.btnPersonaDelete;
    setBusy(btn, true, '删除中…');
    apiCall('delete_persona', p.uid).then(function (res) {
      setBusy(btn, false);
      if (res && res.ok === false) {
        toast('删除失败：' + (hasText(res.error) ? String(res.error) : '未知原因'), 'err');
        return;
      }
      var sessions = numOr(res && res.affected_sessions, 0);
      var msgs = numOr(res && res.deleted_messages, 0);
      toast('已删除人设' + p.slot + '：' + fmtNum(sessions) + ' 个会话切回人设1，永久删除 ' +
        fmtNum(msgs) + ' 条记忆', 'ok');
      S.personaUid = null;
      loadPersonas();
    }, function (err) {
      setBusy(btn, false);
      toast('删除失败：' + errText(err), 'err');
    });
  }

  function resetPersonaMemory() {
    var p = currentPersona();
    if (!p) return;
    var msg = '确定清空「' + (p.name || '（未命名）') + '」（人设' + p.slot + '）的全部记忆？\n\n' +
      '将永久删除 ' + fmtNum(p.message_count) + ' 条记忆，正在使用该人设的会话会从空白重新开始。此操作不可恢复。';
    if (!window.confirm(msg)) return;

    var btn = dom.btnPersonaResetMemory;
    setBusy(btn, true, '清空中…');
    apiCall('reset_persona_memory', p.uid).then(function (res) {
      setBusy(btn, false);
      if (res && res.ok === false) {
        toast('清空失败：' + (hasText(res.error) ? String(res.error) : '未知原因'), 'err');
        return;
      }
      toast('已清空人设' + p.slot + ' 的记忆（' + fmtNum(numOr(res && res.deleted_messages, 0)) + ' 条）', 'ok');
      loadPersonas(p.uid);
    }, function (err) {
      setBusy(btn, false);
      toast('清空失败：' + errText(err), 'err');
    });
  }

  /* ---------------- M3：切换权限 ---------------- */
  function normalizePerm(raw) {
    var s = (raw && typeof raw === 'object') ? raw : {};
    var wl = Array.isArray(s.whitelist) ? s.whitelist : [];
    var out = [];
    wl.forEach(function (x) {
      var v = asStr(x);
      if (v !== '' && out.indexOf(v) === -1) out.push(v);
    });
    return { whitelist_only: asBool(s.whitelist_only, true), whitelist: out };
  }

  function showPermErr(msg) {
    if (!dom.permErr) return;
    if (hasText(msg)) { dom.permErr.textContent = String(msg); show(dom.permErr, true); }
    else { dom.permErr.textContent = ''; show(dom.permErr, false); }
  }

  function loadPersonaPermission() {
    return apiCall('get_persona_permission').then(function (res) {
      S.perm = normalizePerm(res);
      renderPerm();
    }, function (err) {
      setClass(dom.permStateTag, 'tag err');
      setText(dom.permStateTag, '读取失败');
      if (window.console) console.warn('get_persona_permission 失败：', err);
    });
  }

  function renderPerm() {
    var p = S.perm || { whitelist_only: true, whitelist: [] };
    dom.permWhitelistOnly.checked = !!p.whitelist_only;
    setClass(dom.permStateTag, 'tag ' + (p.whitelist_only ? 'ok' : 'warn'));
    setText(dom.permStateTag, p.whitelist_only
      ? ('仅白名单 · ' + p.whitelist.length + ' 个 QQ')
      : '所有人可用 /person');

    var box = dom.permChips;
    box.textContent = '';
    if (!p.whitelist.length) {
      box.appendChild(mk('span', 'chips-empty', p.whitelist_only
        ? '白名单为空：目前没有 QQ 能在群里切换人设'
        : '白名单为空'));
      return;
    }
    p.whitelist.forEach(function (qq) {
      var chip = mk('span', 'chip');
      chip.appendChild(mk('span', null, qq));
      var x = mk('button', 'chip-x', '×');
      x.type = 'button';
      x.title = '从白名单移除 ' + qq;
      x.setAttribute('aria-label', '移除 ' + qq);
      x.addEventListener('click', function () { removePerm(qq); });
      chip.appendChild(x);
      box.appendChild(chip);
    });
  }

  function setPermEnabled(on) {
    var next = !!on;
    dom.permWhitelistOnly.disabled = true;
    apiCall('set_persona_permission', next).then(function (res) {
      dom.permWhitelistOnly.disabled = false;
      if (res && res.ok === false) {
        toast('权限保存失败' + (hasText(res.error) ? ('：' + res.error) : ''), 'err');
        renderPerm();
        return;
      }
      if (!S.perm) S.perm = { whitelist_only: true, whitelist: [] };
      S.perm.whitelist_only = next;
      renderPerm();
      toast(next ? '已开启：仅白名单 QQ 可切换人设' : '已关闭：任何人都可以在群里切换人设',
        next ? 'ok' : 'warn');
    }, function (err) {
      dom.permWhitelistOnly.disabled = false;
      renderPerm();
      toast('权限保存失败：' + errText(err), 'err');
    });
  }

  function addPerm() {
    var qq = dom.permQqInput.value.trim();
    showPermErr('');
    if (!/^\d{5,12}$/.test(qq)) { showPermErr('请输入 5 – 12 位数字 QQ 号'); return; }
    if (S.perm && S.perm.whitelist.indexOf(qq) !== -1) { showPermErr(qq + ' 已在白名单中'); return; }

    var btn = dom.btnPermAdd;
    setBusy(btn, true, '添加中…');
    apiCall('add_persona_whitelist', qq).then(function (res) {
      setBusy(btn, false);
      if (res && res.ok === false) {
        showPermErr(hasText(res.error) ? String(res.error)
          : (hasText(res.message) ? String(res.message) : '添加失败'));
        return;
      }
      dom.permQqInput.value = '';
      if (!S.perm) S.perm = { whitelist_only: true, whitelist: [] };
      if (res && Array.isArray(res.whitelist)) S.perm.whitelist = normalizePerm({ whitelist: res.whitelist }).whitelist;
      else if (S.perm.whitelist.indexOf(qq) === -1) S.perm.whitelist.push(qq);
      renderPerm();
      toast('已把 ' + qq + ' 加入白名单', 'ok');
    }, function (err) {
      setBusy(btn, false);
      showPermErr('添加失败：' + errText(err));
    });
  }

  function removePerm(qq) {
    if (!S.perm) return;
    var idx = S.perm.whitelist.indexOf(qq);
    if (idx === -1) return;
    S.perm.whitelist.splice(idx, 1);
    renderPerm();   // 乐观更新，失败再回滚
    apiCall('remove_persona_whitelist', qq).then(function (res) {
      if (res && res.ok === false) {
        toast('移除失败' + (hasText(res.error) ? ('：' + res.error) : ''), 'err');
        loadPersonaPermission();
        return;
      }
      if (res && Array.isArray(res.whitelist)) {
        S.perm.whitelist = normalizePerm({ whitelist: res.whitelist }).whitelist;
        renderPerm();
      }
      toast('已移除 ' + qq, 'ok');
    }, function (err) {
      toast('移除失败：' + errText(err), 'err');
      loadPersonaPermission();
    });
  }

  /* ---------------- 模态 ---------------- */
  function openModal() {
    S.modalOpen = true;
    dom.loginModal.hidden = false;
    renderModal(S.app || defaultState());
    dom.btnStart.focus();
  }

  function closeModal() {
    S.modalOpen = false;
    dom.loginModal.hidden = true;
  }

  /* ---------------- 后端事件订阅 ---------------- */
  window.qlark = {
    onState: function (state) {
      if (!state || typeof state !== 'object') return;
      var prevQQ = S.app ? S.app.qq : undefined;
      S.app = normalizeState(state);
      renderAll();
      // 登录状态变化时刷新账号列表（只在模型页需要）
      if (S.page === 'model' && S.accounts !== null && S.app.qq !== prevQQ) loadAccounts();
    },
    onLog: function (lines) {
      pushLines(lines);
    }
  };

  /* ---------------- mock（浏览器降级模式） ---------------- */
  var MOCK_LOG_POOL = [
    ['info', 'QLark', '状态推送：phase=waiting_scan，等待手机 QQ 扫码'],
    ['debug', 'NapCat', '[NapCat] WebSocket 服务已监听 127.0.0.1:3000'],
    ['info', 'NapCat', '[NapCat] 登录二维码已生成并写入 cache/qrcode.png'],
    ['info', 'QLark', '日志读线程存活，环形缓冲 4096 行'],
    ['warn', 'NapCat', '[NapCat] 二维码即将过期（约 121 秒刷新一次）'],
    ['debug', 'QLark', '环境自检缓存命中，跳过重复检查'],
    ['info', 'QLark', 'OneBot 端口 3000 探测：尚未就绪'],
    ['debug', 'QLark', '统计落盘：stats/2026-01-01.json'],
    ['error', 'NapCat', '[NapCat] 未找到 runtime/napcat/node.exe（示例错误行）'],
    ['info', 'QLark', '快捷键已注册：Ctrl+R 重载界面，Esc 关闭弹窗'],
    ['debug', 'QLark', '主题偏好已写入 localStorage: qlark.theme'],
    ['info', 'NapCat', '[NapCat] 心跳正常，延迟 12ms']
  ];

  function makeMockQr() {
    // 生成一张纯装饰性的"伪二维码"（仅供浏览器预览视觉效果，不可扫描）
    try {
      var modules = 25, scale = 8, quiet = 4;
      var size = (modules + quiet * 2) * scale;
      var cv = document.createElement('canvas');
      cv.width = size;
      cv.height = size;
      var ctx = cv.getContext('2d');
      if (!ctx) return null;
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, size, size);
      ctx.fillStyle = '#000000';

      var seed = 20260101;
      function rnd() {
        seed = (seed * 1103515245 + 12345) & 0x7fffffff;
        return seed / 0x7fffffff;
      }
      function finder(r, c) {
        for (var y = 0; y < 7; y++) {
          for (var x = 0; x < 7; x++) {
            var edge = (y === 0 || y === 6 || x === 0 || x === 6);
            var core = (y >= 2 && y <= 4 && x >= 2 && x <= 4);
            if (edge || core) ctx.fillRect((quiet + c + x) * scale, (quiet + r + y) * scale, scale, scale);
          }
        }
      }
      finder(0, 0); finder(0, modules - 7); finder(modules - 7, 0);

      for (var i = 0; i < modules; i++) {
        for (var j = 0; j < modules; j++) {
          var inFinder = (i < 8 && j < 8) || (i < 8 && j > modules - 9) || (i > modules - 9 && j < 8);
          if (inFinder) continue;
          if (i === 6) { // 定位时序线
            if (j % 2 === 0) ctx.fillRect((quiet + j) * scale, (quiet + i) * scale, scale, scale);
            continue;
          }
          if (j === 6) {
            if (i % 2 === 0) ctx.fillRect((quiet + j) * scale, (quiet + i) * scale, scale, scale);
            continue;
          }
          if (rnd() > 0.5) ctx.fillRect((quiet + j) * scale, (quiet + i) * scale, scale, scale);
        }
      }
      return cv.toDataURL('image/png');
    } catch (e) {
      return null;
    }
  }

  var MOCK = (function () {
    var mockQr = makeMockQr();
    var todayIso = (function () {
      var d = new Date();
      return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
    })();

    var state = {
      phase: 'waiting_scan',
      phase_text: '等待扫码登录',
      qq: null,
      nickname: null,
      online: false,
      onebot_ready: false,
      napcat_running: true,
      pid: 18472,
      uptime_sec: 137,
      qrcode: mockQr ? { data_url: mockQr, mtime: Math.floor(Date.now() / 1000) } : null,
      last_error: null,
      theme: 'dark',
      // M2：预览模式给一组示例值；running 直接为 true，方便在浏览器里点按钮观察暂停 / 恢复
      bot: {
        running: true,
        paused: false,
        queued: 0,
        handled: 12,
        replied: 10,
        dropped: 2,
        last_error: null,
        last_reply_at: Math.floor(Date.now() / 1000) - 42
      },
      event_server: {
        running: true,
        host: '127.0.0.1',
        port: 3001,
        last_event_at: Math.floor(Date.now() / 1000) - 3
      },
      stats: {
        date: todayIso,
        prompt_tokens: 18342,
        completion_tokens: 9021,
        calls: 47,
        errors: 1,
        msg_in: 128,
        msg_out: 96,
        active_sessions: 7
      },
      env: {
        ok: false,
        checked_at: Math.floor(Date.now() / 1000),
        items: [
          { key: 'webview2', label: 'WebView2 运行时', ok: true, detail: '已安装 120.0.2210.91（预览数据）', fixable: false },
          { key: 'napcat_runtime', label: 'NapCat 运行时', ok: true, detail: 'runtime/napcat/node.exe 存在', fixable: false },
          { key: 'onebot_port', label: 'OneBot 端口 3000', ok: false, detail: '端口未监听：登录成功后才会就绪', fixable: true },
          { key: 'data_dir', label: '数据目录可写', ok: false, detail: 'data/ 目录不可写，请检查磁盘权限', fixable: true }
        ]
      },

      /* M3：模型配置（预览数据） */
      model_config: {
        base_url: 'https://api.deepseek.com',
        api_key_masked: 'sk-3f9a••••••••••••7c21',
        api_key_set: true,
        model: 'deepseek-chat',
        vision_model: 'deepseek-vl2',
        summary_model: '',
        max_tokens: 4096,
        temperature: 1,
        onebot_host: '127.0.0.1',
        onebot_port: 3000,
        event_host: '127.0.0.1',
        event_port: 3001,
        token_masked: 'ql••••••3k',
        memory_rounds: 20,
        auto_summary: true,
        reply_quote_group: true,
        max_reply_chars: 500,
        error_notice_private: true,
        restart_required: false
      },

      /* M3：账号（current 为 null 表示还没登录） */
      accounts: {
        current: null,
        accounts: [
          { qq: '2157649443', nickname: '米蛋', has_credential: true, known_by_qlark: true },
          { qq: '1000123456', nickname: '小号 · 测试', has_credential: true, known_by_qlark: false },
          { qq: '3558997120', nickname: null, has_credential: false, known_by_qlark: false }
        ]
      },

      /* M3：人设（已按 slot 升序） */
      personas: [
        {
          uid: 1, slot: 1, name: '米蛋', avatar_path: null,
          system_prompt: '你是「米蛋」，一只住在 QQ 里的云雀。\n说话简短、口语化，偶尔用「咕」结尾。\n不知道的事就直说不知道，不要编造。',
          enabled: true, model_override: null, temperature_override: null,
          max_tokens_override: null, can_delete: false, message_count: 128
        },
        {
          uid: 2, slot: 2, name: '严谨助手', avatar_path: null,
          system_prompt: '你是严谨的技术助手。回答先给结论，再给依据；代码务必给出可运行的完整片段，不要省略。',
          enabled: true, model_override: 'deepseek-reasoner', temperature_override: 0.3,
          max_tokens_override: 2048, can_delete: true, message_count: 42
        },
        {
          uid: 3, slot: 3, name: '', avatar_path: null,
          system_prompt: '',
          enabled: false, model_override: null, temperature_override: null,
          max_tokens_override: null, can_delete: true, message_count: 0
        }
      ],
      persona_default_uid: 1,
      persona_permission: { whitelist_only: true, whitelist: ['2157649443', '1000123456'] }
    };

    var buffer = [];
    var seq = 0;

    function emit(line) {
      seq++;
      var item = { seq: seq, ts: Math.floor(Date.now() / 1000), level: line[0], source: line[1], text: line[2] };
      buffer.push(item);
      if (buffer.length > MAX_LOG_LINES) buffer.splice(0, buffer.length - MAX_LOG_LINES);
      if (window.qlark && window.qlark.onLog) window.qlark.onLog([item]);
      return item;
    }

    function deliver() {
      if (window.qlark && window.qlark.onState) window.qlark.onState(JSON.parse(JSON.stringify(state)));
    }

    function snapshot() {
      return JSON.parse(JSON.stringify(state));
    }

    // 预置日志（只在确认进入 mock 模式后调用，避免污染真实后端模式）
    var seed = [
      ['info', 'QLark', 'QLark 云雀启动（预览模式，未连接 Python 后端）'],
      ['info', 'QLark', '数据目录：E:\\QLark\\data'],
      ['debug', 'QLark', '读取配置 config/app.json 成功'],
      ['info', 'NapCat', '[NapCat] 正在启动 NapCat …'],
      ['info', 'NapCat', '[NapCat] 版本 NapCat.Shell 4.x，工作目录 runtime/napcat'],
      ['info', 'QLark', '环境自检完成：4 项检查，2 项通过'],
      ['debug', 'NapCat', '[NapCat] 登录缓存命中，尝试快速登录'],
      ['warn', 'QLark', '快速登录失败，降级为扫码登录'],
      ['info', 'NapCat', '[NapCat] 二维码已生成：cache/qrcode.png'],
      ['info', 'QLark', '等待用户扫码（二维码约 121 秒刷新一次）'],
      ['debug', 'QLark', '仪表盘数据已刷新：Token 27363 / 调用 47'],
      ['info', 'QLark', '提示：这是浏览器预览模式的模拟日志，用于检查界面效果']
    ];
    var seeded = false;
    function seedLogs() {
      if (seeded) return;
      seeded = true;
      seed.forEach(emit);
    }

    var poolIndex = 0;
    function tick() {
      var line = MOCK_LOG_POOL[poolIndex % MOCK_LOG_POOL.length];
      poolIndex++;
      emit(line);
    }

    function delay(ms) {
      return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    function findPersona(uid) {
      var target = Number(uid);
      var found = null;
      state.personas.forEach(function (p) { if (p.uid === target) found = p; });
      return found;
    }

    function maskSecret(text, head, tail) {
      var s = String(text);
      if (s.length <= head + tail) return '••••••••';
      return s.slice(0, head) + '••••••••' + s.slice(-tail);
    }

    var api = {
      get_state: function () {
        return Promise.resolve(snapshot());
      },
      start: function () {
        state.phase = 'starting';
        state.phase_text = '正在启动 NapCat';
        state.napcat_running = true;
        state.pid = 18472;
        state.uptime_sec = 0;
        state.last_error = null;
        emit(['info', 'QLark', '用户点击「启动 / 重试」，开始启动流程']);
        deliver();
        return delay(700).then(function () {
          state.phase = 'quick_login';
          state.phase_text = '快速登录中';
          emit(['info', 'NapCat', '[NapCat] 尝试使用已保存账号快速登录']);
          deliver();
          return delay(900);
        }).then(function () {
          state.phase = 'waiting_scan';
          state.phase_text = '等待扫码登录';
          state.qrcode = mockQr ? { data_url: mockQr, mtime: Math.floor(Date.now() / 1000) } : null;
          state.onebot_ready = false;
          state.bot.running = true;
          state.event_server.running = true;
          emit(['info', 'NapCat', '[NapCat] 二维码已生成，请使用手机 QQ 扫码']);
          deliver();
          return { ok: true };
        });
      },
      stop: function () {
        state.phase = 'stopped';
        state.phase_text = '已停止';
        state.napcat_running = false;
        state.pid = null;
        state.onebot_ready = false;
        state.online = false;
        state.uptime_sec = 0;
        state.bot.running = false;
        state.bot.queued = 0;
        state.event_server.running = false;
        emit(['info', 'QLark', '已停止 NapCat 进程树']);
        deliver();
        return Promise.resolve({ ok: true });
      },
      restart_napcat: function () {
        state.phase = 'restarting';
        state.phase_text = '重启 NapCat';
        emit(['info', 'QLark', '重启 NapCat（配置需要重启生效）']);
        deliver();
        return delay(1200).then(function () {
          state.phase = 'waiting_scan';
          state.phase_text = '等待扫码登录';
          state.uptime_sec = 0;
          emit(['info', 'NapCat', '[NapCat] 重启完成，等待登录']);
          deliver();
          return { ok: true };
        });
      },
      run_env_check: function () {
        state.phase = 'env_check';
        state.phase_text = '环境自检中';
        emit(['info', 'QLark', '开始环境自检']);
        deliver();
        return delay(800).then(function () {
          state.env.checked_at = Math.floor(Date.now() / 1000);
          state.env.items = [
            { key: 'webview2', label: 'WebView2 运行时', ok: true, detail: '已安装 120.0.2210.91（预览数据）', fixable: false },
            { key: 'napcat_runtime', label: 'NapCat 运行时', ok: true, detail: 'runtime/napcat/node.exe 存在', fixable: false },
            { key: 'onebot_port', label: 'OneBot 端口 3000', ok: false, detail: '端口未监听：登录成功后才会就绪', fixable: true },
            { key: 'data_dir', label: '数据目录可写', ok: false, detail: 'data/ 目录不可写，请检查磁盘权限', fixable: true }
          ];
          state.env.ok = false;
          state.phase = state.napcat_running ? 'waiting_scan' : 'idle';
          state.phase_text = state.napcat_running ? '等待扫码登录' : '未启动';
          emit(['warn', 'QLark', '环境自检完成：2 项通过，2 项异常']);
          deliver();
          return { ok: true };
        });
      },
      relogin: function () {
        state.qq = null;
        state.nickname = null;
        state.online = false;
        state.phase = 'waiting_scan';
        state.phase_text = '等待扫码登录';
        state.qrcode = mockQr ? { data_url: mockQr, mtime: Math.floor(Date.now() / 1000) } : null;
        emit(['info', 'QLark', '已清除保存的账号，转为扫码登录']);
        deliver();
        return Promise.resolve({ ok: true });
      },
      logout: function () {
        state.qq = null;
        state.nickname = null;
        state.online = false;
        state.onebot_ready = false;
        state.phase = 'idle';
        state.phase_text = '未启动';
        state.qrcode = null;
        emit(['info', 'QLark', '已退出登录并清除账号记录']);
        deliver();
        return Promise.resolve({ ok: true });
      },
      get_logs: function (sinceSeq) {
        var from = Number(sinceSeq) || 0;
        var lines = buffer.filter(function (l) { return l.seq > from; });
        return Promise.resolve({ lines: JSON.parse(JSON.stringify(lines)), next: seq + 1 });
      },
      clear_logs: function () {
        buffer.length = 0;
        return Promise.resolve({ ok: true });
      },
      open_data_dir: function () { return Promise.resolve({ ok: false, message: '预览模式无法打开目录' }); },
      open_runtime_dir: function () { return Promise.resolve({ ok: false, message: '预览模式无法打开目录' }); },
      set_theme: function (t) { state.theme = (t === 'light') ? 'light' : 'dark'; return Promise.resolve({ ok: true }); },
      set_paused: function (p) {
        var next = !!p;
        state.bot.paused = next;
        emit(['info', 'QLark', next
          ? '已暂停自动回复：消息仍会接收，但不会调用模型 / 发送'
          : '已恢复自动回复']);
        deliver();
        return Promise.resolve({ paused: next });
      },
      quit_app: function () { return Promise.resolve({ ok: false, message: '预览模式无法退出程序' }); },

      /* ================= M3：模型配置 ================= */
      get_model_config: function () {
        return Promise.resolve(JSON.parse(JSON.stringify(state.model_config)));
      },
      apply_model_config: function (patch) {
        var p = (patch && typeof patch === 'object') ? patch : {};
        var cfg = state.model_config;
        var errors = {};

        if (p.base_url !== undefined) {
          var baseUrl = String(p.base_url || '');
          if (baseUrl === '') errors.base_url = 'Base URL 不能为空';
          else if (!/^https?:\/\//i.test(baseUrl)) errors.base_url = 'Base URL 需以 http:// 或 https:// 开头';
        }
        if (p.model !== undefined && String(p.model || '') === '') errors.model = '对话模型不能为空';

        [
          ['max_tokens', 1, 32000, '最大输出 Token 需在 1 – 32000 之间'],
          ['temperature', 0, 2, '温度需在 0 – 2 之间'],
          ['memory_rounds', 1, 200, '记忆轮数需在 1 – 200 之间'],
          ['max_reply_chars', 20, 5000, '单条回复最大字数需在 20 – 5000 之间']
        ].forEach(function (row) {
          var v = p[row[0]];
          if (v === undefined || v === null) return;
          var n = Number(v);
          if (!isFinite(n) || n < row[1] || n > row[2]) errors[row[0]] = row[3];
        });

        if (Object.keys(errors).length) {
          return delay(220).then(function () { return { ok: false, errors: errors }; });
        }

        if (p.api_key !== undefined) {
          var key = String(p.api_key || '');
          if (key === '') { cfg.api_key_set = false; cfg.api_key_masked = ''; }
          else { cfg.api_key_set = true; cfg.api_key_masked = maskSecret(key, 6, 4); }
        }
        if (p.base_url !== undefined) cfg.base_url = String(p.base_url || '');
        if (p.model !== undefined) cfg.model = String(p.model || '');
        if (p.vision_model !== undefined) cfg.vision_model = String(p.vision_model || '');
        if (p.summary_model !== undefined) cfg.summary_model = String(p.summary_model || '');
        if (p.max_tokens !== undefined && p.max_tokens !== null) cfg.max_tokens = Number(p.max_tokens);
        if (p.temperature !== undefined && p.temperature !== null) cfg.temperature = Number(p.temperature);
        if (p.memory_rounds !== undefined && p.memory_rounds !== null) cfg.memory_rounds = Number(p.memory_rounds);
        if (p.max_reply_chars !== undefined && p.max_reply_chars !== null) cfg.max_reply_chars = Number(p.max_reply_chars);
        if (p.reply_quote_group !== undefined) cfg.reply_quote_group = !!p.reply_quote_group;
        if (p.error_notice_private !== undefined) cfg.error_notice_private = !!p.error_notice_private;
        if (p.auto_summary !== undefined) cfg.auto_summary = !!p.auto_summary;
        cfg.restart_required = false;

        emit(['info', 'QLark', '模型配置已更新（预览数据）']);
        return delay(260).then(function () { return { ok: true }; });
      },
      apply_napcat_config: function (patch) {
        var p = (patch && typeof patch === 'object') ? patch : {};
        var cfg = state.model_config;
        var errors = {};

        function port(key, label) {
          if (p[key] === undefined || p[key] === null) return null;
          var n = Number(p[key]);
          if (!isFinite(n) || n < 1 || n > 65535 || Math.floor(n) !== n) {
            errors[key] = label + '需为 1 – 65535 的整数';
            return null;
          }
          return n;
        }
        ['onebot_host', 'event_host'].forEach(function (key) {
          if (p[key] !== undefined && String(p[key] || '') === '') {
            errors[key] = '监听地址不能为空';
          }
        });
        var onebotPort = port('onebot_port', 'OneBot 动作端口');
        var eventPort = port('event_port', 'QLark 事件端口');
        if (onebotPort !== null && eventPort !== null && onebotPort === eventPort) {
          errors.event_port = 'QLark 事件端口不能与 OneBot 动作端口相同';
        }

        if (Object.keys(errors).length) {
          return delay(220).then(function () { return { ok: false, errors: errors }; });
        }

        if (p.onebot_host !== undefined) cfg.onebot_host = String(p.onebot_host);
        if (onebotPort !== null) cfg.onebot_port = onebotPort;
        if (p.event_host !== undefined) cfg.event_host = String(p.event_host);
        if (eventPort !== null) cfg.event_port = eventPort;
        if (p.token !== undefined && String(p.token || '') !== '') {
          cfg.token_masked = maskSecret(String(p.token), 2, 2);
        }
        cfg.restart_required = true;

        emit(['warn', 'QLark', 'NapCat 连接配置已保存，重启 NapCat 后才能生效']);
        return delay(260).then(function () { return { ok: true, restart_required: true }; });
      },
      test_model: function (opt) {
        var o = (opt && typeof opt === 'object') ? opt : {};
        var cfg = state.model_config;
        // 只有「没传」才回退到已保存配置；传了空串就按空串测（前端就是这么发的）
        var baseUrl = (o.base_url === undefined || o.base_url === null) ? cfg.base_url : String(o.base_url);
        var model = (o.model === undefined || o.model === null) ? cfg.model : String(o.model);
        if (baseUrl === '' || model === '') {
          return delay(600).then(function () {
            return {
              ok: false, kind: 'config', latency_ms: 0,
              message: '缺少 Base URL 或模型名',
              hint: '还未配置 Base URL / 对话模型，请填写后点「应用」'
            };
          });
        }
        emit(['debug', 'QLark', '测试连接：向 ' + baseUrl + ' 发送 max_tokens=1 的最小请求（预览数据）']);
        return delay(600).then(function () {
          return {
            ok: true, latency_ms: 320, model: model,
            prompt_tokens: 5, completion_tokens: 1
          };
        });
      },

      /* ================= M3：账号 ================= */
      list_accounts: function () {
        return Promise.resolve(JSON.parse(JSON.stringify(state.accounts)));
      },
      login_as: function (qq) {
        var target = String(qq === undefined || qq === null ? '' : qq);
        var found = null;
        state.accounts.accounts.forEach(function (a) { if (a.qq === target) found = a; });
        if (!found) {
          return delay(200).then(function () { return { ok: false, error: '未找到该账号的登录记录' }; });
        }
        state.accounts.current = target;
        state.qq = target;
        state.nickname = found.nickname;
        state.online = true;
        state.onebot_ready = true;
        state.phase = 'ready';
        state.phase_text = '就绪';
        state.qrcode = null;
        emit(['info', 'QLark', '使用已保存的账号 ' + target + ' 触发登录流程（预览数据）']);
        deliver();
        return delay(700).then(function () {
          emit(['info', 'NapCat', '[NapCat] 账号 ' + target + ' 登录成功（预览数据）']);
          deliver();
          return { ok: true };
        });
      },

      /* ================= M3：人设 ================= */
      list_personas: function () {
        var list = state.personas.slice().sort(function (a, b) { return a.slot - b.slot; });
        return Promise.resolve({
          default_uid: state.persona_default_uid,
          personas: JSON.parse(JSON.stringify(list))
        });
      },
      create_persona: function () {
        var maxSlot = 0, maxUid = 0;
        state.personas.forEach(function (p) {
          if (p.slot > maxSlot) maxSlot = p.slot;
          if (p.uid > maxUid) maxUid = p.uid;
        });
        if (maxSlot >= 99) {
          return Promise.resolve({ ok: false, error: '人设数量已达上限 99' });
        }
        var created = {
          uid: maxUid + 1, slot: maxSlot + 1, name: '', avatar_path: null,
          system_prompt: '', enabled: true,
          model_override: null, temperature_override: null, max_tokens_override: null,
          can_delete: true, message_count: 0
        };
        state.personas.push(created);
        emit(['info', 'QLark', '已新建人设' + created.slot + '（预览数据）']);
        return delay(250).then(function () {
          return { ok: true, uid: created.uid, slot: created.slot };
        });
      },
      update_persona: function (uid, patch) {
        var p = findPersona(uid);
        if (!p) return Promise.resolve({ ok: false, error: '人设不存在：' + uid });
        var q = (patch && typeof patch === 'object') ? patch : {};
        var errors = {};
        if (q.name !== undefined && String(q.name).length > 40) errors.name = '名称最多 40 个字';
        if (q.system_prompt !== undefined && String(q.system_prompt).length > 8000) {
          errors.system_prompt = '系统提示词最多 8000 字';
        }
        if (q.model_override !== undefined && String(q.model_override).length > 64) {
          errors.model_override = '模型名最多 64 个字符';
        }
        if (hasText(q.temperature_override)) {
          var tv = Number(q.temperature_override);
          if (!isFinite(tv) || tv < 0 || tv > 2) errors.temperature_override = '温度需在 0 – 2 之间';
        }
        if (hasText(q.max_tokens_override)) {
          var mv = Number(q.max_tokens_override);
          if (!isFinite(mv) || mv < 1 || mv > 32000) errors.max_tokens_override = '最大 Token 需在 1 – 32000 之间';
        }
        if (Object.keys(errors).length) {
          return delay(200).then(function () { return { ok: false, errors: errors }; });
        }
        if (q.name !== undefined) p.name = String(q.name);
        if (q.system_prompt !== undefined) p.system_prompt = String(q.system_prompt);
        if (q.enabled !== undefined) p.enabled = !!q.enabled;
        if (q.model_override !== undefined) {
          p.model_override = hasText(q.model_override) ? String(q.model_override) : null;
        }
        if (q.temperature_override !== undefined) {
          p.temperature_override = hasText(q.temperature_override) ? Number(q.temperature_override) : null;
        }
        if (q.max_tokens_override !== undefined) {
          p.max_tokens_override = hasText(q.max_tokens_override) ? Number(q.max_tokens_override) : null;
        }
        emit(['info', 'QLark', '人设' + p.slot + ' 已更新（预览数据）']);
        return delay(200).then(function () { return { ok: true }; });
      },
      delete_persona: function (uid) {
        var p = findPersona(uid);
        if (!p) return Promise.resolve({ ok: false, error: '人设不存在：' + uid });
        if (!p.can_delete) return Promise.resolve({ ok: false, error: '出厂默认人设不可删除' });
        var msgs = p.message_count;
        var slot = p.slot;
        var affected = slot === 1 ? 0 : 3;
        state.personas = state.personas.filter(function (x) { return x.uid !== p.uid; });
        emit(['warn', 'QLark', '已删除人设' + slot + '：' + affected + ' 个会话切回人设1，永久删除 ' + msgs + ' 条记忆（预览数据）']);
        return delay(260).then(function () {
          return {
            ok: true, affected_sessions: affected,
            deleted_messages: msgs, affected_keys: msgs > 0 ? 4 : 0
          };
        });
      },
      get_persona_permission: function () {
        return Promise.resolve(JSON.parse(JSON.stringify(state.persona_permission)));
      },
      set_persona_permission: function (whitelistOnly) {
        state.persona_permission.whitelist_only = !!whitelistOnly;
        emit(['info', 'QLark', whitelistOnly
          ? '已开启：仅白名单 QQ 可以在群里切换人设'
          : '已关闭：任何人都可以在群里切换人设']);
        return delay(180).then(function () { return { ok: true }; });
      },
      add_persona_whitelist: function (qq) {
        var s = String(qq === undefined || qq === null ? '' : qq);
        if (!/^\d{5,12}$/.test(s)) return Promise.resolve({ ok: false, error: 'QQ 号需为 5 – 12 位数字' });
        if (state.persona_permission.whitelist.indexOf(s) === -1) {
          state.persona_permission.whitelist.push(s);
        }
        emit(['info', 'QLark', '白名单已加入 ' + s + '（预览数据）']);
        return delay(180).then(function () {
          return { ok: true, whitelist: state.persona_permission.whitelist.slice() };
        });
      },
      remove_persona_whitelist: function (qq) {
        var s = String(qq === undefined || qq === null ? '' : qq);
        state.persona_permission.whitelist = state.persona_permission.whitelist.filter(function (x) {
          return x !== s;
        });
        emit(['info', 'QLark', '白名单已移除 ' + s + '（预览数据）']);
        return delay(150).then(function () {
          return { ok: true, whitelist: state.persona_permission.whitelist.slice() };
        });
      },
      reset_persona_memory: function (uid) {
        var p = findPersona(uid);
        if (!p) return Promise.resolve({ ok: false, error: '人设不存在：' + uid });
        var n = p.message_count;
        p.message_count = 0;
        emit(['warn', 'QLark', '已清空人设' + p.slot + ' 的全部记忆（' + n + ' 条，预览数据）']);
        return delay(260).then(function () { return { ok: true, deleted_messages: n }; });
      },
      _deliver: deliver,
      _tick: tick,
      _countTick: function () {
        // 每约 6 秒模拟处理完一条消息，便于观察数字刷新（不写日志，避免刷屏）
        state.bot.handled += 1;
        state.bot.replied += 1;
        state.bot.last_reply_at = Math.floor(Date.now() / 1000);
        state.event_server.last_event_at = Math.floor(Date.now() / 1000);
        deliver();
      },
      _seed: seedLogs,
      _snapshot: snapshot
    };

    return { api: api, state: state, emit: emit };
  })();

  /* ---------------- 定时器 ---------------- */
  function clearTimers() {
    S.timers.forEach(function (t) { clearInterval(t); });
    S.timers = [];
  }

  function startTimers() {
    clearTimers();
    if (S.usingMock) {
      S.timers.push(setInterval(function () {
        MOCK.api._tick();
      }, MOCK_LOG_MS));
      S.timers.push(setInterval(function () {
        MOCK.state.uptime_sec += 1;
        if (S.app) S.app.uptime_sec = MOCK.state.uptime_sec;
        renderAll();
      }, 1000));
      S.timers.push(setInterval(function () {
        // mock 下二维码"刷新"时间戳走一走，便于观察
        if (MOCK.state.qrcode && !MOCK.state.qq) {
          MOCK.state.qrcode.mtime = Math.floor(Date.now() / 1000);
        }
      }, 30000));
      S.timers.push(setInterval(function () {
        MOCK.api._countTick();
      }, MOCK_COUNT_MS));
    } else {
      S.timers.push(setInterval(function () {
        apiCall('get_state').then(function (res) {
          if (res && typeof res === 'object') window.qlark.onState(res);
        })['catch'](function () { /* 轮询失败静默，等下一次 */ });
      }, STATE_POLL_MS));
      S.timers.push(setInterval(loadLogsOnce, LOG_POLL_MS));
    }
  }

  /* ---------------- 真实后端接入 ---------------- */
  function switchToReal() {
    if (!S.usingMock) return;
    S.usingMock = false;
    dom.previewBadge.hidden = true;
    S.logs = [];
    S.lastSeq = 0;
    S.cursor = 0;
    S.pendingNew = 0;
    dom.logView.textContent = '';

    // 关键：清掉 mock 期间可能已经被填充的页面缓存。
    // 页面选择会存进 localStorage，所以"上次停在模型页"时，启动早期就会用
    // mock 数据把表单填满并把 modelLoaded 置为 true —— 切到真实后端后它就
    // 再也不会重新加载，用户看到的是预览数据，点「应用」还会把它写回后端。
    S.model = null;
    S.modelLoaded = false;
    S.accounts = null;
    S.personas = [];
    S.personaUid = null;
    S.perm = null;
    if (S.page === 'model') {
      loadModelConfig();
      loadAccounts();
    } else if (S.page === 'persona') {
      loadPersonas();
      loadPersonaPermission();
    }

    apiCall('get_state').then(function (res) {
      if (res && typeof res === 'object') window.qlark.onState(res);
    })['catch'](function () { /* ignore */ });
    return loadLogsOnce().then(function () {
      startTimers();
      toast('已连接 Python 后端', 'ok');
    });
  }

  function watchForBackend() {
    if (realApi()) { switchToReal(); return; }
    window.addEventListener('pywebviewready', function () {
      if (realApi()) switchToReal();
    });
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (realApi()) {
        clearInterval(timer);
        switchToReal();
      } else if (tries > 60) {
        clearInterval(timer);
      }
    }, 250);
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    dom.nav.addEventListener('click', function (ev) {
      var target = ev.target;
      while (target && target !== dom.nav && !target.getAttribute('data-page')) target = target.parentNode;
      if (!target || target === dom.nav) return;
      switchPage(target.getAttribute('data-page'));
    });

    dom.themeBtn.addEventListener('click', toggleTheme);
    dom.loginBtn.addEventListener('click', openModal);
    dom.modalClose.addEventListener('click', closeModal);
    dom.btnClose.addEventListener('click', closeModal);

    dom.loginModal.addEventListener('click', function (ev) {
      if (ev.target === dom.loginModal) closeModal();
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && S.modalOpen) closeModal();
    });

    dom.btnStart.addEventListener('click', function () { runApi('start'); });
    dom.btnStop.addEventListener('click', function () { runApi('stop'); });
    dom.btnRelogin.addEventListener('click', function () {
      runApi('relogin', '已切换为扫码登录');
    });
    dom.btnLogout.addEventListener('click', function () {
      runApi('logout', '已退出登录');
    });
    dom.btnEnvRecheck.addEventListener('click', function () { runApi('run_env_check'); });

    dom.qaEnv.addEventListener('click', function () { runApi('run_env_check', '环境自检已触发'); });
    dom.btnPause.addEventListener('click', function () {
      if (S.pauseBusy) return;
      var bot = S.app && S.app.bot;
      var next = !(bot && bot.paused);
      // 只发请求：不本地改 paused，等后端 onState 推送回来（推送为准）
      S.pauseBusy = true;
      renderPause(S.app || defaultState());
      apiCall('set_paused', next).then(function () {
        S.pauseBusy = false;
        renderPause(S.app || defaultState());
      }, function (err) {
        S.pauseBusy = false;
        renderPause(S.app || defaultState());
        toast('切换暂停状态失败：' + (err && err.message ? err.message : err), 'err');
      });
    });
    dom.qaRestart.addEventListener('click', function () {
      if (window.confirm('重启 NapCat 会短暂断开连接，确定继续？')) runApi('restart_napcat', 'NapCat 重启指令已发送');
    });
    dom.qaDataDir.addEventListener('click', function () { runApi('open_data_dir'); });
    dom.qaRuntimeDir.addEventListener('click', function () { runApi('open_runtime_dir'); });

    dom.levelFilter.addEventListener('click', function (ev) {
      var btn = ev.target;
      while (btn && btn !== dom.levelFilter && !btn.getAttribute('data-level')) btn = btn.parentNode;
      if (!btn || btn === dom.levelFilter) return;
      S.level = btn.getAttribute('data-level');
      var items = dom.levelFilter.querySelectorAll('.seg-item');
      for (var i = 0; i < items.length; i++) {
        items[i].className = (items[i] === btn) ? 'seg-item active' : 'seg-item';
      }
      renderLogsFull();
    });

    var searchTimer = null;
    dom.logSearch.addEventListener('input', function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        S.keyword = dom.logSearch.value.trim();
        renderLogsFull();
      }, 160);
    });

    dom.autoScroll.addEventListener('change', function () {
      S.autoScroll = dom.autoScroll.checked;
      if (S.autoScroll) scrollToBottom();
      updateLogFoot();
    });

    dom.btnClearLogs.addEventListener('click', function () {
      runApi('clear_logs').then(function () {
        S.logs = [];
        S.lastSeq = 0;
        S.cursor = 0;
        S.pendingNew = 0;
        dom.logView.textContent = '';
        renderLogsFull();
        toast('已清空日志缓冲', 'ok');
      })['catch'](function () { /* 已在 runApi 中提示 */ });
    });

    dom.btnExportLogs.addEventListener('click', exportLogs);
    dom.logJump.addEventListener('click', scrollToBottom);
    dom.logView.addEventListener('scroll', onLogScroll);

    /* ----- M3：模型配置页 ----- */
    // 两处「应用」都提交页面上的全部设置（同一个 apply_model_config payload），
    // 避免用户改了 A 区却点 B 区的「应用」导致改动被静默丢弃
    dom.btnModelApply.addEventListener('click', function () { applyModelConfig(true); });
    dom.btnBehaviorApply.addEventListener('click', function () { applyModelConfig(true); });
    dom.btnModelTest.addEventListener('click', testModel);
    dom.mApiKeyClear.addEventListener('click', clearApiKey);
    dom.btnNapcatApply.addEventListener('click', applyNapcatConfig);
    dom.btnNapcatRestart.addEventListener('click', restartNapcat);
    dom.btnAccountsRefresh.addEventListener('click', function () { loadAccounts(); });

    /* ----- M3：人设配置页 ----- */
    dom.btnPersonaNew.addEventListener('click', createPersona);
    dom.btnPersonaSave.addEventListener('click', savePersona);
    dom.btnPersonaDelete.addEventListener('click', deletePersona);
    dom.btnPersonaResetMemory.addEventListener('click', resetPersonaMemory);
    dom.pPrompt.addEventListener('input', updatePromptCount);
    dom.permWhitelistOnly.addEventListener('change', function () {
      setPermEnabled(dom.permWhitelistOnly.checked);
    });
    dom.btnPermAdd.addEventListener('click', addPerm);
    dom.permQqInput.addEventListener('input', function () { showPermErr(''); });
    dom.permQqInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); addPerm(); }
    });
  }

  /* ---------------- 启动 ---------------- */
  function boot() {
    initTheme();
    bind();

    var savedPage = null;
    try { savedPage = localStorage.getItem('qlark.page'); } catch (e) { savedPage = null; }
    switchPage(savedPage && PAGE_META[savedPage] ? savedPage : 'dashboard');

    if (realApi()) {
      S.usingMock = false;
      dom.previewBadge.hidden = true;
      apiCall('get_state').then(function (res) {
        if (res && typeof res === 'object') window.qlark.onState(res);
      })['catch'](function () { /* ignore */ });
      loadLogsOnce().then(function () { startTimers(); });
    } else {
      S.usingMock = true;
      dom.previewBadge.hidden = false;
      window.qlark.onState(MOCK.api._snapshot());
      MOCK.api._seed();
      loadLogsOnce().then(function () { startTimers(); });
      watchForBackend();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
