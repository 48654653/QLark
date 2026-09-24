-- QLark 数据模型（需求规格 v1.1 §7）
-- 引擎：SQLite，WAL 模式
-- 注意：M1 只用到 config / stats_daily / session_activity，
--       其余表先建好，避免 M4 阶段再做迁移。

-- ---------------------------------------------------------------- 人设
CREATE TABLE IF NOT EXISTS personas (
  uid                  INTEGER PRIMARY KEY AUTOINCREMENT,  -- 不可变，永不复用
  slot                 INTEGER NOT NULL,                  -- 界面编号 1..N（可重排）
  name                 TEXT    NOT NULL DEFAULT '',
  avatar_path          TEXT    DEFAULT '',
  system_prompt        TEXT    DEFAULT '',
  enabled              INTEGER DEFAULT 0,
  model_override       TEXT    DEFAULT NULL,
  temperature_override REAL    DEFAULT NULL,
  max_tokens_override  INTEGER DEFAULT NULL,
  created_at           INTEGER NOT NULL
);

-- ---------------------------------------------------------------- 会话状态
-- 每个群 / 每个私聊用户一行，记录其当前生效人设
CREATE TABLE IF NOT EXISTS session_state (
  session_id     TEXT PRIMARY KEY,        -- group:<群号> | private:<QQ号>
  session_type   TEXT NOT NULL,           -- group | private
  session_ref    TEXT NOT NULL,           -- 群号 | QQ号
  active_persona INTEGER NOT NULL,        -- personas.uid
  updated_at     INTEGER NOT NULL
);

-- ---------------------------------------------------------------- 记忆：原文
-- 只存触发了机器人的问答；图片以 [图片] 占位
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_key  TEXT    NOT NULL,           -- <session_id>:p<persona_uid>
  role        TEXT    NOT NULL,           -- user | assistant
  content     TEXT    NOT NULL,
  has_image   INTEGER DEFAULT 0,
  tokens      INTEGER DEFAULT 0,
  msg_id      TEXT,                       -- OneBot 消息 ID，用于去重
  created_at  INTEGER NOT NULL,
  summarized  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_key ON messages(memory_key, summarized, id);

-- ---------------------------------------------------------------- 记忆：总结
-- 合并式压缩：新总结 = 总结(老总结 + 新 W 轮)，一个 memory_key 最多一条
CREATE TABLE IF NOT EXISTS summaries (
  memory_key  TEXT PRIMARY KEY,
  content     TEXT NOT NULL,
  covered_to  INTEGER NOT NULL,           -- 已覆盖到的 messages.id
  pending     INTEGER DEFAULT 0,          -- 压缩失败待重试
  updated_at  INTEGER NOT NULL
);

-- ---------------------------------------------------------------- 每日统计
CREATE TABLE IF NOT EXISTS stats_daily (
  date              TEXT NOT NULL,        -- 本地时区 YYYY-MM-DD，00:00 重置
  prompt_tokens     INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  calls             INTEGER DEFAULT 0,
  errors            INTEGER DEFAULT 0,
  msg_in            INTEGER DEFAULT 0,
  msg_out           INTEGER DEFAULT 0,
  PRIMARY KEY (date)
);

-- ---------------------------------------------------------------- 全局配置
CREATE TABLE IF NOT EXISTS config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                     -- JSON 编码
);

-- ---------------------------------------------------------------- 人设切换白名单
CREATE TABLE IF NOT EXISTS persona_whitelist (
  qq TEXT PRIMARY KEY
);

-- ---------------------------------------------------------------- 会话活跃度
CREATE TABLE IF NOT EXISTS session_activity (
  session_id   TEXT PRIMARY KEY,
  last_active  INTEGER NOT NULL,
  msg_count    INTEGER DEFAULT 0
);
