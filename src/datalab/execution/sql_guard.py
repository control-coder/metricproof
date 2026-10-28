"""run_sql 的服务端白名单校验：模型写的 SQL 先在宿主用 sqlglot（duckdb 方言）解析，只放行单条只读 SELECT，
表与列在 Olist 规范化表白名单内，外层必须有不超过上限的 LIMIT；校验通过后输出规范化 SQL 与每张表需要投影的列，
沙箱内只放这些列。沙箱里的 DuckDB 另有关闭外部访问的配置，作为第二道防线。
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from datalab.datasets import olist

MAX_LIMIT = 200
MAX_SQL_CHARS = 4096

WHITELIST: dict[str, tuple[str, ...]] = {name: olist.columns(name) for name in olist.TABLES}

# 读文件、环境、元数据、扩展等越界函数；read_/duckdb_/pragma_/sniff_/parquet_/iceberg_/delta_ 前缀同样拒绝。
FORBIDDEN_FUNCTIONS = frozenset({
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_text", "read_blob",
    "read_ndjson", "glob", "getenv", "current_setting", "query", "query_table", "sql_auto_complete",
    "which_secret", "load_extension", "install_extension", "sniff_csv", "parquet_scan", "csv_scan",
})
FORBIDDEN_FUNCTION_PREFIXES = ("read_", "duckdb_", "pragma_", "sniff_", "parquet_", "iceberg_", "delta_",
                               "json_execute")

# 树中任何位置出现即拒绝的节点（含数据修改 CTE、SELECT INTO 等）；类名按 sqlglot 版本防御性获取。
_FORBIDDEN_NODE_NAMES = ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command", "Copy",
                         "Pragma", "Set", "Use", "Attach", "Detach", "Into", "Install", "Load", "Export")
FORBIDDEN_NODES = tuple(node for node in (getattr(exp, name, None) for name in _FORBIDDEN_NODE_NAMES)
                        if node is not None)


class SqlRejected(ValueError):
    """SQL 未通过白名单校验；code 为稳定的机器可读原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GuardedQuery:
    """校验通过的查询：规范化 SQL、每张白名单基表需投影的列（排序后）、外层 LIMIT。"""

    sql: str
    tables: dict[str, tuple[str, ...]]
    limit: int


def _reject(code: str, message: str):
    raise SqlRejected(code, message)


def _function_name(func: exp.Func) -> str:
    """取函数的 SQL 名称：已知函数用 sql_name()，匿名函数用其 name 参数。"""
    if isinstance(func, exp.Anonymous):
        return (func.name or "").lower()
    return func.sql_name().lower()


def _check_functions(statement: exp.Expression):
    for func in statement.find_all(exp.Func):
        name = _function_name(func)
        if name in FORBIDDEN_FUNCTIONS or any(name.startswith(prefix) for prefix in FORBIDDEN_FUNCTION_PREFIXES):
            _reject("forbidden_function", f"函数 {name} 不在允许范围内，可能读取文件、环境或数据库元数据")


def _check_nodes(statement: exp.Expression):
    for node in statement.walk():
        if isinstance(node, exp.Expression) and isinstance(node, FORBIDDEN_NODES):
            _reject("forbidden_node", f"不允许出现 {type(node).__name__} 节点：只放行只读 SELECT")


def _collect_cte_names(statement: exp.Expression) -> set[str]:
    return {cte.alias.lower() for cte in statement.find_all(exp.CTE)}


def _check_tables(statement: exp.Expression, whitelist: dict[str, tuple[str, ...]], cte_names: set[str]):
    for table in statement.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            # read_csv('...')、glob('*')、duckdb_tables() 等表函数
            _reject("table_function", "FROM 中不允许使用表函数或文件路径，只能引用白名单表")
        if table.db or table.catalog:
            _reject("qualified_table", f"表 {table.sql(dialect='duckdb')} 带了库名前缀，只能直接使用白名单表名")
        if not table.name.replace("_", "").isalnum():
            # FROM '/input/orders.csv' 这类字符串路径会被解析成标识符，按表函数同样拒绝
            _reject("table_function", "FROM 中不允许使用表函数或文件路径，只能引用白名单表")
        name = table.name.lower()
        if name in cte_names:
            continue
        if name not in whitelist:
            _reject("unknown_table", f"表 {table.name} 不在白名单内")


def _resolve_columns(statement: exp.Expression, whitelist: dict[str, tuple[str, ...]]) -> dict[str, set[str]]:
    """qualify 后按作用域把列引用归到基表；星号已展开，未知列在此抛出。"""
    schema = {table: {column: "VARCHAR" for column in columns} for table, columns in whitelist.items()}
    try:
        qualified = qualify(statement.copy(), schema=schema, dialect="duckdb", validate_qualify_columns=True,
                            quote_identifiers=False, identify=False)
    except sqlglot.errors.OptimizeError as exc:
        _reject("unknown_column", f"列无法解析：{exc}")
    used: dict[str, set[str]] = {}
    for scope in traverse_scope(qualified):
        # 基表来源：别名 → 白名单表名
        base_sources = {alias: source.name.lower() for alias, source in scope.sources.items()
                        if isinstance(source, exp.Table)}
        for column in scope.columns:
            table_name = base_sources.get(column.table)
            if table_name in whitelist:
                used.setdefault(table_name, set()).add(column.name)
    return used


def _check_limit(statement: exp.Expression, max_limit: int) -> int:
    limit = statement.args.get("limit")
    if limit is None:
        _reject("missing_limit", "最外层查询必须有整数字面量 LIMIT")
    value = limit.expression
    if not isinstance(value, exp.Literal) or not value.is_int:
        _reject("missing_limit", "LIMIT 必须是整数字面量")
    number = int(value.this)
    if number < 1 or number > max_limit:
        _reject("limit_too_large", f"LIMIT {number} 超出 1–{max_limit} 的允许范围")
    return number


def guard_sql(sql: str, whitelist: dict[str, tuple[str, ...]] = WHITELIST,
              max_limit: int = MAX_LIMIT) -> GuardedQuery:
    """校验模型提交的 SQL，返回规范化语句与每张基表需要投影的列；不通过时抛出 SqlRejected。"""
    if len(sql) > MAX_SQL_CHARS:
        _reject("too_long", f"SQL 超过 {MAX_SQL_CHARS} 字符上限")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.SqlglotError as exc:
        _reject("parse_error", f"SQL 解析失败：{exc}")
    # 结尾分号会产生 None 占位，忽略
    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        _reject("multiple_statements", "一次只允许一条 SELECT 语句")
    statement = statements[0]
    if not isinstance(statement, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        _reject("not_select", "只允许只读 SELECT 查询（含 WITH 与集合运算）")

    _check_nodes(statement)
    cte_names = _collect_cte_names(statement)
    _check_tables(statement, whitelist, cte_names)
    _check_functions(statement)
    used = _resolve_columns(statement, whitelist)
    limit = _check_limit(statement, max_limit)

    # 表被引用但没有任何列（如 count(*)）：给它的第一列，保证沙箱里仍能加载该表
    referenced = {table.name.lower() for table in statement.find_all(exp.Table)
                  if isinstance(table.this, exp.Identifier) and not table.db and not table.catalog
                  and table.name.lower() not in cte_names}
    tables = {}
    for name in sorted(referenced):
        columns = sorted(used.get(name, set()))
        if not columns:
            columns = [whitelist[name][0]]
        tables[name] = tuple(columns)
    return GuardedQuery(sql=statement.sql(dialect="duckdb"), tables=tables, limit=limit)
