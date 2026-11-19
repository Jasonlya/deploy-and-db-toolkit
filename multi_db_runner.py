#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多数据库 SQL 执行工具
=====================

功能:
    1. 从 .docx / .txt / .sql / .md 文档中提取 SQL 语句
    2. 对提取结果做格式校验与规范化:
       - 全角/半角标点、智能引号、不可见字符、NBSP、全角空格
       - 空白与换行、中英文分号、关键字大小写、括号平衡
       - 未闭合引号/注释、语句未以 SQL 关键字开头
    3. 在 MySQL / PostgreSQL / Oracle / SQL Server / DB2 / 达梦 6 种数据库上执行
    4. 输出终端汇总和可选 JSON 明细

用法:
    python multi_db_runner.py <文档路径> -c <配置.json> [选项]

必选参数:
    document              输入文档路径 (.docx/.txt/.sql/.md)
    -c, --config FILE     数据库配置 JSON 文件

可选参数:
    -o, --output-json FILE   执行明细写入 JSON
    --check-only             只校验不执行（配合 --dump-sql 人工复核）
    --strict                 存在 error 时直接退出（适合 CI/交付前把关）
    --dump-sql FILE          输出规范化后的 SQL
    --keep-comments          保留 -- 行注释（保留换行）
    --no-uppercase           不统一关键字大写

示例:
    # 只校验并导出规范化 SQL
    python multi_db_runner.py 变更说明.docx -c databases.json \
        --check-only --dump-sql fixed.sql

    # 严格校验，有 error 直接退出
    python multi_db_runner.py 变更说明.docx -c databases.json \
        --strict -o report.json

    # 正式执行
    python multi_db_runner.py 变更说明.docx -c databases.json -o report.json

配置文件格式 (databases.json):
    {
      "databases": [
        {
          "alias": "mysql-prod",
          "type": "mysql",
          "host": "10.0.0.11",
          "port": 3306,
          "database": "appdb",
          "user": "root",
          "password": "xxx",
          "jdbc_jar": "/opt/jdbc/mysql-connector-j-8.4.0.jar"
        }
      ]
    }

    支持的 type: mysql / postgresql / oracle / sqlserver / db2 / dm
    达梦如需原生驱动，设置 "extra": {"driver": "dmpython"}

依赖:
    pip install python-docx jaydebeapi
    达梦原生驱动: pip install dmPython
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------- 可选依赖 ----------
try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    import jaydebeapi
    JAYDEBEAPI_AVAILABLE = True
except ImportError:
    JAYDEBEAPI_AVAILABLE = False

try:
    import dmPython
    DMPYTHON_AVAILABLE = True
except ImportError:
    DMPYTHON_AVAILABLE = False


# ============================================================
# 数据结构
# ============================================================

@dataclass
class DatabaseConfig:
    """单个数据库连接配置"""
    alias: str
    db_type: str           # mysql / postgresql / oracle / sqlserver / db2 / dm
    host: str
    port: int
    database: str          # Oracle 可为 service name / SID
    user: str
    password: str
    jdbc_jar: Optional[str] = None
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class StatementResult:
    """单条 SQL 在某库上的执行结果"""
    sql: str
    success: bool
    rowcount: int = 0
    data: Optional[List[Tuple]] = None
    error: Optional[str] = None
    elapsed_ms: float = 0.0


@dataclass
class DatabaseRunResult:
    """单个数据库的整体执行结果"""
    alias: str
    db_type: str
    connected: bool
    connection_error: Optional[str] = None
    statements: List[StatementResult] = field(default_factory=list)


@dataclass
class ValidationIssue:
    """单条校验问题"""
    level: str      # error / warning / info
    code: str
    message: str


@dataclass
class ValidatedStatement:
    """校验并规范化后的 SQL 语句"""
    original: str
    normalized: str
    issues: List[ValidationIssue] = field(default_factory=list)


# ============================================================
# 一、字符级预处理
# ============================================================

# 不可见字符：BOM、零宽空格、软连字符
INVISIBLE_CHARS = ['\ufeff', '\u200b', '\u200c', '\u200d', '\u00ad']

# Word 智能引号 -> 直引号
SMART_QUOTES = {
    '\u2018': "'", '\u2019': "'", '\u201a': "'", '\u201b': "'",
    '\u201c': '"', '\u201d': '"', '\u201e': '"', '\u201f': '"',
    '\u2032': "'", '\u2033': '"',
}


def preprocess_chars(text: str) -> Tuple[str, List[str]]:
    """
    字符级预处理：
      - 移除不可见字符
      - NBSP / 全角空格 -> 普通空格
      - 智能引号 -> 直引号
      - 统一换行为 \\n
    返回 (处理后文本, 修复说明列表)
    """
    fixes: List[str] = []

    for ch in INVISIBLE_CHARS:
        if ch in text:
            text = text.replace(ch, '')
            fixes.append(f"移除不可见字符 U+{ord(ch):04X}")

    if '\u00a0' in text:
        text = text.replace('\u00a0', ' ')
        fixes.append("NBSP(U+00A0) → 普通空格")

    if '\u3000' in text:
        text = text.replace('\u3000', ' ')
        fixes.append("全角空格(U+3000) → 普通空格")

    for k, v in SMART_QUOTES.items():
        if k in text:
            text = text.replace(k, v)
            fixes.append(f"智能引号 {k} → {v}")

    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return text, fixes


# ============================================================
# 二、SQL 关键字与全角映射
# ============================================================

# 需要在代码区统一大写的关键字集合
SQL_KEYWORDS = {
    'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'IS', 'NULL', 'LIKE',
    'BETWEEN', 'EXISTS', 'UNION', 'ALL', 'DISTINCT', 'AS', 'ON', 'JOIN', 'LEFT',
    'RIGHT', 'INNER', 'OUTER', 'FULL', 'CROSS', 'GROUP', 'BY', 'ORDER', 'HAVING',
    'LIMIT', 'OFFSET', 'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE',
    'CREATE', 'TABLE', 'VIEW', 'INDEX', 'PROCEDURE', 'FUNCTION', 'TRIGGER',
    'SEQUENCE', 'ALTER', 'DROP', 'TRUNCATE', 'MERGE', 'COMMIT', 'ROLLBACK',
    'BEGIN', 'END', 'DECLARE', 'IF', 'ELSE', 'THEN', 'WHEN', 'CASE', 'LOOP',
    'WHILE', 'FOR', 'RETURN', 'RETURNS', 'PRIMARY', 'KEY', 'FOREIGN',
    'REFERENCES', 'CONSTRAINT', 'UNIQUE', 'DEFAULT', 'CHECK', 'CASCADE',
    'GRANT', 'REVOKE', 'COMMENT', 'WITH', 'RECURSIVE', 'OVER', 'PARTITION',
    'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'CAST', 'CONVERT', 'COALESCE',
    'NVL', 'DECODE', 'SYSDATE', 'CURRENT_DATE', 'CURRENT_TIMESTAMP',
    'INT', 'INTEGER', 'BIGINT', 'SMALLINT', 'DECIMAL', 'NUMERIC', 'NUMBER',
    'FLOAT', 'DOUBLE', 'REAL', 'CHAR', 'VARCHAR', 'VARCHAR2', 'NVARCHAR',
    'TEXT', 'CLOB', 'BLOB', 'DATE', 'DATETIME', 'TIMESTAMP', 'BOOLEAN',
    'EXPLAIN', 'ANALYZE', 'VACUUM', 'GO',
}

# 全角标点 -> 半角（仅在代码区生效）
FULLWIDTH_MAP = {
    '；': ';', '，': ',', '（': '(', '）': ')', '：': ':',
    '＝': '=', '＊': '*', '＋': '+', '－': '-', '／': '/',
    '％': '%', '＜': '<', '＞': '>', '！': '!', '＆': '&',
    '｜': '|', '＾': '^', '～': '~', '＃': '#', '＠': '@',
    '【': '[', '】': ']', '｛': '{', '｝': '}', '　': ' ',
    '。': '.', '、': ',', '？': '?', '“': '"', '”': '"', '‘': "'", '’': "'",
}


# ============================================================
# 三、状态机规范化
# ============================================================

def normalize_code(text: str, keep_comments: bool = False,
                   uppercase_kw: bool = True) -> Tuple[str, List[str]]:
    """
    用状态机区分代码区 / 单引号字符串 / 双引号标识符 / 反引号标识符 /
    行注释 / 块注释，只对代码区做规范化：
      - 全角标点 -> 半角
      - 连续空白 -> 单个空格
      - SQL 关键字 -> 大写
    字符串、标识符、块注释原样保留，避免破坏业务数据。
    行注释默认剥离（避免 Word 段落合并后注释吞掉后续 SQL），
    keep_comments=True 时保留并保留换行。
    """
    out: List[str] = []
    fixes: List[str] = []
    i, n = 0, len(text)
    state = 'code'
    pending_space = False
    comment_buf: List[str] = []

    def emit_space():
        """在需要的位置补一个空格，避免关键字粘连"""
        nonlocal pending_space
        if pending_space and out and out[-1] not in (' ', '\n', '(', ',', ';'):
            out.append(' ')
        pending_space = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ''

        # ---------- 代码区 ----------
        if state == 'code':
            # 行注释 --
            if ch == '-' and nxt == '-':
                state = 'line_comment'; comment_buf = []; i += 2; continue
            # 块注释 /*
            if ch == '/' and nxt == '*':
                state = 'block_comment'; emit_space(); out.append('/*'); i += 2; continue
            # 进入字符串/标识符
            if ch in ("'", '"', '`'):
                emit_space()
                state = {'\'': 'single', '"': 'double', '`': 'backtick'}[ch]
                out.append(ch); i += 1; continue
            # 空白压缩
            if ch in ' \t\n':
                pending_space = True; i += 1; continue
            # 全角转半角
            if ch in FULLWIDTH_MAP:
                mapped = FULLWIDTH_MAP[ch]
                fixes.append(f"全角字符 {ch} → {mapped}")
                ch = mapped
            # 关键字大写
            if uppercase_kw and (ch.isalpha() or ch == '_'):
                j = i
                while j < n and (text[j].isalnum() or text[j] in '_$#'):
                    j += 1
                word = text[i:j]
                if word.upper() in SQL_KEYWORDS and word != word.upper():
                    fixes.append(f"关键字 {word} → {word.upper()}")
                    word = word.upper()
                emit_space(); out.append(word); i = j; continue
            emit_space(); out.append(ch); i += 1

        # ---------- 单引号字符串 ----------
        elif state == 'single':
            out.append(ch)
            if ch == "'":
                if nxt == "'":  # 转义 ''
                    out.append(nxt); i += 2; continue
                state = 'code'
            i += 1

        # ---------- 双引号标识符 ----------
        elif state == 'double':
            out.append(ch)
            if ch == '"':
                if nxt == '"':
                    out.append(nxt); i += 2; continue
                state = 'code'
            i += 1

        # ---------- 反引号标识符 ----------
        elif state == 'backtick':
            out.append(ch)
            if ch == '`':
                state = 'code'
            i += 1

        # ---------- 行注释 ----------
        elif state == 'line_comment':
            if ch == '\n':
                body = ''.join(comment_buf).strip()
                if keep_comments:
                    out.append('--' + body + '\n')
                elif body:
                    fixes.append(f"剥离行注释: -- {body[:60]}")
                state = 'code'
                pending_space = not keep_comments
                i += 1; continue
            comment_buf.append(ch); i += 1

        # ---------- 块注释 ----------
        elif state == 'block_comment':
            out.append(ch)
            if ch == '*' and nxt == '/':
                out.append(nxt); i += 2; state = 'code'; continue
            i += 1

    # 收尾：未闭合的行注释
    if state == 'line_comment':
        body = ''.join(comment_buf).strip()
        if keep_comments:
            out.append('--' + body)
        elif body:
            fixes.append(f"剥离行注释: -- {body[:60]}")
    elif state != 'code':
        raise ValueError(f"未闭合的结构，结束状态: {state}")

    return ''.join(out).strip(), fixes


# ============================================================
# 四、安全分句与校验
# ============================================================

def split_statements_safe(text: str) -> List[str]:
    """
    按分号切分语句，跳过字符串、标识符、注释中的分号。
    与 SQLParser 的区别：不依赖第三方库，纯状态机，行为可控。
    """
    stmts, buf = [], []
    i, n, state = 0, len(text), 'code'

    while i < n:
        ch = text[i]; nxt = text[i + 1] if i + 1 < n else ''

        if state == 'code':
            if ch in ("'", '"', '`'):
                state = {'\'': 'single', '"': 'double', '`': 'backtick'}[ch]
                buf.append(ch); i += 1; continue
            # 行注释直接跳过
            if ch == '-' and nxt == '-':
                while i < n and text[i] != '\n': i += 1
                continue
            # 块注释
            if ch == '/' and nxt == '*':
                state = 'block'; buf.append('/*'); i += 2; continue
            # 语句分隔符
            if ch == ';':
                s = ''.join(buf).strip()
                if s: stmts.append(s)
                buf = []; i += 1; continue
            buf.append(ch); i += 1

        elif state == 'single':
            buf.append(ch)
            if ch == "'":
                if nxt == "'": buf.append(nxt); i += 2; continue
                state = 'code'
            i += 1

        elif state == 'double':
            buf.append(ch)
            if ch == '"':
                if nxt == '"': buf.append(nxt); i += 2; continue
                state = 'code'
            i += 1

        elif state == 'backtick':
            buf.append(ch)
            if ch == '`': state = 'code'
            i += 1

        elif state == 'block':
            buf.append(ch)
            if ch == '*' and nxt == '/':
                buf.append(nxt); i += 2; state = 'code'; continue
            i += 1

    s = ''.join(buf).strip()
    if s: stmts.append(s)
    return stmts


def check_parens(sql: str) -> Tuple[bool, str]:
    """
    检查括号平衡，跳过字符串/标识符/注释中的括号。
    返回 (是否平衡, 错误说明)
    """
    depth, i, n, state = 0, 0, len(sql), 'code'

    while i < n:
        ch = sql[i]; nxt = sql[i + 1] if i + 1 < n else ''

        if state == 'code':
            if ch in ("'", '"', '`'):
                state = {'\'': 'single', '"': 'double', '`': 'backtick'}[ch]
                i += 1; continue
            if ch == '-' and nxt == '-':
                while i < n and sql[i] != '\n': i += 1
                continue
            if ch == '/' and nxt == '*':
                state = 'block'; i += 2; continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth < 0:
                    return False, "右括号多余"
            i += 1

        elif state == 'single':
            if ch == "'":
                if nxt == "'": i += 2; continue
                state = 'code'
            i += 1
        elif state == 'double':
            if ch == '"':
                if nxt == '"': i += 2; continue
                state = 'code'
            i += 1
        elif state == 'backtick':
            if ch == '`': state = 'code'
            i += 1
        elif state == 'block':
            if ch == '*' and nxt == '/':
                i += 2; state = 'code'; continue
            i += 1

    if depth != 0:
        return False, f"括号不平衡，剩余深度 {depth}"
    return True, ""


# ============================================================
# 五、文档读取与 SQL 候选提取
# ============================================================

# 语句起始关键字，用于识别 SQL 块并剥离前置说明文字
SQL_START_PATTERN = re.compile(
    r"(?is)^\s*(SELECT|INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|"
    r"CREATE\s+(OR\s+REPLACE\s+)?(TABLE|VIEW|INDEX|PROCEDURE|FUNCTION|TRIGGER|SEQUENCE)|"
    r"ALTER\s+(TABLE|VIEW|INDEX)|DROP\s+(TABLE|VIEW|INDEX|PROCEDURE|FUNCTION|TRIGGER|SEQUENCE)|"
    r"MERGE\s+INTO|TRUNCATE\s+TABLE|COMMENT\s+ON|GRANT\s+|REVOKE\s+|WITH\s+\w+\s+AS)"
)


def read_document(path: str) -> str:
    """
    读取文档内容：
      - .docx: 段落 + 表格单元格文本，按行拼接
      - .txt/.sql/.md: 直接读取
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == '.docx':
        if not DOCX_AVAILABLE:
            raise RuntimeError("未安装 python-docx，请执行: pip install python-docx")
        doc = Document(path)
        parts: List[str] = []
        for para in doc.paragraphs:
            if para.text and para.text.strip():
                parts.append(para.text)
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" ".join(cells))
        return "\n".join(parts)

    if ext in ('.txt', '.sql', '.md'):
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()

    raise ValueError(f"不支持的文档类型: {ext}")


def extract_sql_candidates(text: str) -> List[str]:
    """
    将文档文本切成"SQL 候选块"：
      - 空行作为块分隔
      - 遇到以 SQL 关键字开头的新行，且当前块非空，则另起一块
    这样能把 SQL 前后的说明文字与 SQL 分开。
    """
    text = re.sub(r'[ \t]+', ' ', text.replace('\r\n', '\n').replace('\r', '\n')).strip()
    lines = text.split('\n')
    blocks: List[str] = []
    current: List[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(" ".join(current)); current = []
            continue
        if SQL_START_PATTERN.match(stripped) and current:
            blocks.append(" ".join(current)); current = [stripped]
        else:
            current.append(stripped)

    if current:
        blocks.append(" ".join(current))

    return blocks or [text]


# ============================================================
# 六、校验 + 规范化主入口
# ============================================================

def load_and_validate(doc_path: str, keep_comments: bool = False,
                      uppercase_kw: bool = True
                      ) -> Tuple[List[ValidatedStatement], List[ValidationIssue]]:
    """
    读取文档 -> 字符预处理 -> 提取候选块 -> 状态机规范化 -> 安全分句
    -> 逐句校验（括号、SQL 关键字开头、末尾分号）
    返回 (语句列表, 全局问题列表)
    """
    raw = read_document(doc_path)
    raw, char_fixes = preprocess_chars(raw)
    global_issues = [ValidationIssue('info', 'CHARS', f) for f in char_fixes]

    candidates = extract_sql_candidates(raw)
    statements: List[ValidatedStatement] = []

    for block in candidates:
        # 剥离 SQL 前的说明文字
        m = SQL_START_PATTERN.search(block)
        if m and m.start() > 0:
            prefix = block[:m.start()].strip()
            if prefix:
                global_issues.append(ValidationIssue(
                    'warning', 'PREFIX', f"剥离前置说明文字: {prefix[:60]}"))
            block = block[m.start():]

        # 状态机规范化
        try:
            norm, fixes = normalize_code(block, keep_comments=keep_comments,
                                         uppercase_kw=uppercase_kw)
        except ValueError as e:
            statements.append(ValidatedStatement(
                original=block, normalized=block,
                issues=[ValidationIssue('error', 'UNCLOSED', str(e))]))
            continue

        for f in fixes:
            global_issues.append(ValidationIssue('info', 'NORMALIZE', f))

        # 安全分句 + 逐句校验
        for s in split_statements_safe(norm):
            stripped = s.strip()
            if not stripped:
                continue
            if stripped.upper() == 'GO':
                continue

            issues: List[ValidationIssue] = []

            if not SQL_START_PATTERN.match(stripped):
                issues.append(ValidationIssue(
                    'warning', 'NON_SQL_START',
                    f"语句未以 SQL 关键字开头，请确认: {stripped[:50]}"))

            ok, msg = check_parens(stripped)
            if not ok:
                issues.append(ValidationIssue('error', 'PAREN', msg))

            if stripped.endswith(';'):
                issues.append(ValidationIssue(
                    'warning', 'SEMICOLON', "语句末尾已有分号，切分后统一补全"))

            # 统一去掉末尾分号再补一个，保证执行器拿到的语句格式一致
            normalized = stripped.rstrip(';').strip() + ';'
            statements.append(ValidatedStatement(
                original=block, normalized=normalized, issues=issues))

    return statements, global_issues


def print_validation(statements: List[ValidatedStatement],
                     global_issues: List[ValidationIssue]) -> Tuple[int, int]:
    """打印校验报告，返回 (error 数, warning 数)"""
    print("=" * 72)
    print("SQL 格式校验报告")
    print("=" * 72)

    for iss in global_issues:
        print(f"  [{iss.level.upper():7}] {iss.code}: {iss.message}")

    err = warn = 0
    for i, st in enumerate(statements, 1):
        levels = [x.level for x in st.issues]
        err += levels.count('error')
        warn += levels.count('warning')
        preview = re.sub(r'\s+', ' ', st.normalized)[:90]
        print(f"\n  [{i}] {preview}")
        if not st.issues:
            print("       ✓ 格式正常")
        for iss in st.issues:
            print(f"       [{iss.level.upper():7}] {iss.code}: {iss.message}")

    print(f"\n合计: {len(statements)} 条语句, error={err}, warning={warn}")
    return err, warn


def dump_sql(statements: List[ValidatedStatement], path: str):
    """把规范化后的 SQL 写入文件，便于人工复核"""
    with open(path, 'w', encoding='utf-8') as f:
        for st in statements:
            f.write(st.normalized)
            f.write("\n\n")
    print(f"\n规范化后的 SQL 已写入: {path}")


# ============================================================
# 七、数据库执行器
# ============================================================

class JDBCExecutor:
    """
    基于 jaydebeapi + JDBC 驱动的统一执行器。
    覆盖 MySQL / PostgreSQL / Oracle / SQL Server / DB2 / 达梦(JDBC)。
    """

    DRIVER_MAP = {
        "mysql": "com.mysql.cj.jdbc.Driver",
        "postgresql": "org.postgresql.Driver",
        "oracle": "oracle.jdbc.OracleDriver",
        "sqlserver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
        "db2": "com.ibm.db2.jcc.DB2Driver",
        "dm": "dm.jdbc.driver.DmDriver",
    }

    def __init__(self, cfg: DatabaseConfig):
        self.cfg = cfg
        self._conn = None

    def _build_url(self) -> str:
        """按数据库类型拼 JDBC URL"""
        c = self.cfg
        if c.db_type == "mysql":
            return f"jdbc:mysql://{c.host}:{c.port}/{c.database}?useSSL=false&allowPublicKeyRetrieval=true"
        if c.db_type == "postgresql":
            return f"jdbc:postgresql://{c.host}:{c.port}/{c.database}"
        if c.db_type == "oracle":
            return f"jdbc:oracle:thin:@{c.host}:{c.port}/{c.database}"
        if c.db_type == "sqlserver":
            return f"jdbc:sqlserver://{c.host}:{c.port};databaseName={c.database};encrypt=false"
        if c.db_type == "db2":
            return f"jdbc:db2://{c.host}:{c.port}/{c.database}"
        if c.db_type == "dm":
            return f"jdbc:dm://{c.host}:{c.port}/{c.database}"
        raise ValueError(f"不支持的数据库类型: {c.db_type}")

    def connect(self):
        """建立 JDBC 连接"""
        if not JAYDEBEAPI_AVAILABLE:
            raise RuntimeError("未安装 jaydebeapi，请执行: pip install jaydebeapi")
        driver = self.DRIVER_MAP.get(self.cfg.db_type)
        if not driver:
            raise ValueError(f"没有为 {self.cfg.db_type} 配置 JDBC Driver")
        if not self.cfg.jdbc_jar or not os.path.isfile(self.cfg.jdbc_jar):
            raise FileNotFoundError(f"JDBC jar 不存在: {self.cfg.jdbc_jar}")
        self._conn = jaydebeapi.connect(
            driver, self._build_url(),
            [self.cfg.user, self.cfg.password], self.cfg.jdbc_jar)
        return self

    def execute(self, sql: str, fetch: bool = True, limit: int = 200) -> StatementResult:
        """
        执行单条 SQL：
          - SELECT/WITH 语句取前 limit 行数据
          - 其他语句记录影响行数
          - 异常时回滚并记录错误信息
        """
        start = time.time()
        result = StatementResult(sql=sql, success=False)

        try:
            if self._conn is None:
                self.connect()
            cur = self._conn.cursor()
            try:
                cur.execute(sql)
                result.rowcount = cur.rowcount if cur.rowcount is not None else 0

                if fetch and re.match(r"(?is)^\s*(SELECT|WITH)\b", sql):
                    try:
                        result.data = cur.fetchmany(limit)
                    except Exception:
                        result.data = None

                self._conn.commit()
                result.success = True

            except Exception as e:
                try: self._conn.rollback()
                except Exception: pass
                result.error = f"{type(e).__name__}: {e}"
            finally:
                try: cur.close()
                except Exception: pass

        except Exception as e:
            result.error = f"连接/执行异常: {type(e).__name__}: {e}"
        finally:
            result.elapsed_ms = (time.time() - start) * 1000

        return result

    def close(self):
        """关闭连接"""
        if self._conn:
            try: self._conn.close()
            except Exception: pass
            self._conn = None


class DmPythonExecutor:
    """
    达梦原生 Python 驱动执行器。
    当配置中 extra.driver == "dmpython" 时启用。
    """

    def __init__(self, cfg: DatabaseConfig):
        self.cfg = cfg
        self._conn = None

    def connect(self):
        if not DMPYTHON_AVAILABLE:
            raise RuntimeError("未安装 dmPython，无法使用达梦原生驱动")
        self._conn = dmPython.connect(
            user=self.cfg.user, password=self.cfg.password,
            server=self.cfg.host, port=self.cfg.port,
            schema=self.cfg.database or None)
        return self

    def execute(self, sql: str, fetch: bool = True, limit: int = 200) -> StatementResult:
        start = time.time()
        result = StatementResult(sql=sql, success=False)

        try:
            if self._conn is None:
                self.connect()
            cur = self._conn.cursor()
            try:
                cur.execute(sql)
                result.rowcount = cur.rowcount if cur.rowcount is not None else 0
                if fetch and re.match(r"(?is)^\s*(SELECT|WITH)\b", sql):
                    try: result.data = cur.fetchmany(limit)
                    except Exception: result.data = None
                self._conn.commit()
                result.success = True
            except Exception as e:
                try: self._conn.rollback()
                except Exception: pass
                result.error = f"{type(e).__name__}: {e}"
            finally:
                try: cur.close()
                except Exception: pass
        except Exception as e:
            result.error = f"连接/执行异常: {type(e).__name__}: {e}"
        finally:
            result.elapsed_ms = (time.time() - start) * 1000

        return result

    def close(self):
        if self._conn:
            try: self._conn.close()
            except Exception: pass
            self._conn = None


def create_executor(cfg: DatabaseConfig):
    """按配置选择执行器：达梦可走原生驱动，其余统一 JDBC"""
    if cfg.db_type == "dm" and cfg.extra.get("driver") == "dmpython":
        return DmPythonExecutor(cfg)
    return JDBCExecutor(cfg)


def load_config(path: str) -> List[DatabaseConfig]:
    """加载 databases.json，补全默认端口"""
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        raw = raw.get("databases", [])

    configs = []
    for item in raw:
        cfg = DatabaseConfig(
            alias=item["alias"], db_type=item["type"].lower(),
            host=item["host"], port=int(item.get("port", 0)),
            database=item.get("database", item.get("service", "")),
            user=item["user"], password=item["password"],
            jdbc_jar=item.get("jdbc_jar"), extra=item.get("extra", {}))

        # 未指定端口时按数据库类型补默认值
        if cfg.port == 0:
            cfg.port = {"mysql": 3306, "postgresql": 5432, "oracle": 1521,
                        "sqlserver": 1433, "db2": 50000, "dm": 5236}.get(cfg.db_type, 0)

        configs.append(cfg)

    return configs


# ============================================================
# 八、执行与汇总
# ============================================================

def execute_all(statements: List[ValidatedStatement],
                configs: List[DatabaseConfig]) -> List[DatabaseRunResult]:
    """逐库连接并执行全部语句"""
    results: List[DatabaseRunResult] = []

    for cfg in configs:
        print(f"\n>>> 连接 [{cfg.alias}] ({cfg.db_type} @ {cfg.host}:{cfg.port}) ...")
        run = DatabaseRunResult(alias=cfg.alias, db_type=cfg.db_type, connected=False)
        executor = create_executor(cfg)

        try:
            executor.connect()
            run.connected = True
            print("    连接成功")

            for st in statements:
                res = executor.execute(st.normalized)
                run.statements.append(res)
                icon = "✓" if res.success else "✗"
                preview = re.sub(r'\s+', ' ', st.normalized)[:70]
                print(f"    {icon} {preview}  ({res.elapsed_ms:.0f}ms)")

        except Exception as e:
            run.connection_error = f"{type(e).__name__}: {e}"
            print(f"    连接失败: {run.connection_error}")
        finally:
            executor.close()

        results.append(run)

    return results


def summarize(results: List[DatabaseRunResult], output_json: Optional[str] = None):
    """打印汇总；output_json 不为空时写 JSON 明细"""
    total, success = 0, 0

    print("\n" + "=" * 72)
    print("执行结果汇总")
    print("=" * 72)

    for r in results:
        print(f"\n[{r.alias}] ({r.db_type})")
        if not r.connected:
            print(f"  ✗ 连接失败: {r.connection_error}")
            continue

        ok_count = sum(1 for s in r.statements if s.success)
        fail_count = len(r.statements) - ok_count
        total += len(r.statements)
        success += ok_count

        print(f"  语句总数: {len(r.statements)}  |  成功: {ok_count}  |  失败: {fail_count}")

        for i, s in enumerate(r.statements, 1):
            status = "✓" if s.success else "✗"
            preview = re.sub(r'\s+', ' ', s.sql.strip())[:80]
            print(f"    {status} [{i}] {preview}  ({s.elapsed_ms:.0f}ms)")

            if s.success and s.data is not None:
                print(f"        返回行数(最多显示 {len(s.data)} 行)")
                for row in s.data[:5]:
                    print(f"          {row}")
                if len(s.data) > 5:
                    print(f"          ... 其余 {len(s.data) - 5} 行省略")

            if not s.success:
                print(f"        错误: {s.error}")

    if total:
        print(f"\n总计: {success}/{total} 条语句执行成功 ({success / total * 100:.1f}%)")

    if output_json:
        payload = [{
            "alias": r.alias,
            "db_type": r.db_type,
            "connected": r.connected,
            "connection_error": r.connection_error,
            "statements": [{
                "sql": s.sql,
                "success": s.success,
                "rowcount": s.rowcount,
                "elapsed_ms": round(s.elapsed_ms, 2),
                "error": s.error,
                "sample_data": [[str(v) for v in row] for row in (s.data or [])][:20],
            } for s in r.statements],
        } for r in results]

        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 明细已写入: {output_json}")


# ============================================================
# 九、主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="多数据库 SQL 执行工具：文档提取 → 格式校验/规范化 → 6 库执行 → 汇总",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  # 只校验并导出规范化 SQL
  python multi_db_runner.py 变更说明.docx -c databases.json --check-only --dump-sql fixed.sql

  # 严格校验，有 error 直接退出
  python multi_db_runner.py 变更说明.docx -c databases.json --strict -o report.json

  # 正式执行
  python multi_db_runner.py 变更说明.docx -c databases.json -o report.json
""")

    parser.add_argument("document", help="输入文档路径 (.docx / .txt / .sql / .md)")
    parser.add_argument("-c", "--config", required=True, help="数据库配置 JSON 文件")
    parser.add_argument("-o", "--output-json", help="执行明细写入 JSON 文件")
    parser.add_argument("--check-only", action="store_true", help="只校验不执行")
    parser.add_argument("--strict", action="store_true", help="存在 error 时直接退出")
    parser.add_argument("--dump-sql", metavar="FILE", help="输出规范化后的 SQL 到文件")
    parser.add_argument("--keep-comments", action="store_true", help="保留 -- 行注释（保留换行）")
    parser.add_argument("--no-uppercase", action="store_true", help="不统一关键字大写")

    args = parser.parse_args()

    if not os.path.isfile(args.document):
        print(f"文档不存在: {args.document}", file=sys.stderr)
        return 1

    try:
        # 1. 提取 + 校验 + 规范化
        statements, global_issues = load_and_validate(
            args.document,
            keep_comments=args.keep_comments,
            uppercase_kw=not args.no_uppercase)

        if not statements:
            print("未从文档中提取到任何 SQL 语句。", file=sys.stderr)
            return 1

        err, _ = print_validation(statements, global_issues)

        # 2. 可选：导出规范化 SQL
        if args.dump_sql:
            dump_sql(statements, args.dump_sql)

        # 3. 只校验模式
        if args.check_only:
            return 1 if (args.strict and err > 0) else 0

        # 4. 存在 error 时的处理
        if err > 0:
            if args.strict:
                print(f"\n校验存在 {err} 个 error，--strict 模式下终止执行。", file=sys.stderr)
                return 1
            print(f"\n警告: 存在 {err} 个 error，继续执行可能导致失败。")
            ans = input("是否继续在数据库上执行? (y/N): ").strip().lower()
            if ans != 'y':
                print("已取消执行。")
                return 1

        # 5. 加载配置并执行
        configs = load_config(args.config)
        if not configs:
            print("数据库配置为空。", file=sys.stderr)
            return 1

        results = execute_all(statements, configs)
        summarize(results, output_json=args.output_json)

        # 返回码：全部成功 0，否则 2
        all_ok = all(r.connected and all(s.success for s in r.statements) for r in results)
        return 0 if all_ok else 2

    except Exception as e:
        print(f"运行失败: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())