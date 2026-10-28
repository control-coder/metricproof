"""run_sql 的沙箱执行：服务端只投影已通过白名单校验的表与列写入只读输入，容器内由服务端生成的固定脚本
（不是模型代码）用 DuckDB 载入并执行；同一容器里再在行序打乱的表上执行一次（蜕变关系：行序置换不变），
并返回每张表的载入行数供宿主比对。"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from uuid import uuid4

from datalab.datasets import olist
from datalab.execution.runner import DockerRunner, ExecutionError, OutputSpec

# 蜕变关系行序打乱的固定种子：同一次分析的两遍执行与复算使用同一种子，结果可复现。
SEED = 20261028


def _validate_tables(tables: dict[str, tuple[str, ...]]):
    if not isinstance(tables, dict) or not 1 <= len(tables) <= 8:
        raise ValueError("表集合无效")
    for name, cols in tables.items():
        if name not in olist.TABLES:
            raise ValueError(f"表 {name} 不在白名单内")
        known = {column for column, _, _ in olist.TABLES[name]["columns"]}
        if not cols or any(not isinstance(col, str) or col not in known for col in cols):
            raise ValueError(f"表 {name} 的列不在白名单内")


def project_inputs(folder: Path, tables: dict[str, tuple[str, ...]]) -> tuple[dict[str, bytes], dict[str, int]]:
    """从数据版本目录只读取所需表的所需列，返回（文件名 → CSV 字节，表 → 宿主侧行数）。"""
    _validate_tables(tables)
    rows = olist.read_tables(folder, list(tables))
    inputs = {f"{name}.csv": olist.encode(rows[name], cols) for name, cols in tables.items()}
    return inputs, {name: len(rows[name]) for name in tables}


def build_script(sql: str, tables: dict[str, tuple[str, ...]], limit: int, seed: int = SEED) -> str:
    """生成容器内运行的固定分析脚本。SQL 与表规格只经 JSON 载荷传入，不做字符串拼接进源码。"""
    _validate_tables(tables)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("行数上限无效")
    specs = {name: [[col, kind] for col, _, kind in olist.TABLES[name]["columns"] if col in set(cols)]
             for name, cols in tables.items()}
    payload = json.dumps({"sql": sql, "tables": specs, "limit": limit, "seed": seed},
                         ensure_ascii=False, separators=(",", ":"))
    # base64 只含字母数字与 +/=，嵌入源码字面量不可能与引号冲突。
    return _TEMPLATE.replace("__PAYLOAD__", base64.b64encode(payload.encode("utf-8")).decode("ascii"))


def run_query(runner: DockerRunner, folder: Path, sql: str, tables: dict[str, tuple[str, ...]], limit: int,
              *, metric_version: str, seed: int = SEED, timeout: float = 30.0) -> dict:
    """投影输入后在沙箱执行 SQL；返回容器结果（含 error 时原样返回），并附带宿主行数与耗时。"""
    inputs, host_counts = project_inputs(folder, tables)
    run_id = str(uuid4())
    started = time.monotonic()
    manifest = runner.execute_inputs(run_id, inputs, build_script(sql, tables, limit, seed), timeout,
                                     OutputSpec(files=("result.json",), max_bytes=4 * 1024 * 1024),
                                     dataset_version=folder.name, metric_version=metric_version)
    if manifest.status != "COMPLETED":
        raise ExecutionError(manifest.reason or manifest.status)
    result = json.loads((runner.root / run_id / "output" / "result.json").read_text(encoding="utf-8"))
    result["host_counts"] = host_counts
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


# 容器内固定脚本：只从 JSON 载荷读取 SQL 与表规格，DuckDB 只读 /input，配置加载后锁定。
# 载荷是 base64 编码的 JSON，容器内解码。
_TEMPLATE = r"""
import base64
import datetime
import decimal
import json
import math

import duckdb

payload = json.loads(base64.b64decode("__PAYLOAD__").decode("utf-8"))

con = duckdb.connect(":memory:")
con.execute("SET threads = 1")
con.execute("SET memory_limit = '160MB'")
con.execute("SET allowed_directories = ['/input']")
con.execute("SET enable_external_access = false")
con.execute("SET autoinstall_known_extensions = false")
con.execute("SET autoload_known_extensions = false")
con.execute("SET lock_configuration = true")

INT_KINDS = {"int", "score", "cents"}


def load_expr(name, kind):
    column = '"' + name + '"'
    base = kind.rstrip("?")
    duck = "BIGINT" if base in INT_KINDS else "TIMESTAMP" if base == "time" else "VARCHAR"
    if kind.endswith("?"):
        return "CAST(NULLIF(" + column + ", '') AS " + duck + ")"
    return column if duck == "VARCHAR" else "CAST(" + column + " AS " + duck + ")"


def convert(value):
    if value is None or isinstance(value, (int, str, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    return str(value)


def run(schema, sql, limit):
    con.execute("SET search_path = '" + schema + "'")
    cursor = con.execute(sql)
    names = [entry[0] for entry in cursor.description]
    types = [str(entry[1]) for entry in cursor.description]
    rows = [[convert(value) for value in row] for row in cursor.fetchmany(limit + 1)]
    return names, types, rows


try:
    for table, columns in payload["tables"].items():
        select = ", ".join(load_expr(name, kind) + ' AS "' + name + '"' for name, kind in columns)
        con.execute('CREATE TABLE main."' + table + '" AS SELECT ' + select
                    + " FROM read_csv('/input/" + table + ".csv', header=true, all_varchar=true)")
    con.execute("CREATE SCHEMA perm")
    for table in payload["tables"]:
        con.execute('CREATE TABLE perm."' + table + '" AS SELECT * FROM main."' + table
                    + '" ORDER BY hash(rowid, ' + str(payload["seed"]) + ")")
    columns, types, rows = run("main", payload["sql"], payload["limit"])
    _, _, permuted = run("perm", payload["sql"], payload["limit"])
    counts = {table: con.execute('SELECT count(*) FROM main."' + table + '"').fetchone()[0]
              for table in payload["tables"]}
    result = {"columns": columns, "types": types, "rows": rows, "permuted_rows": permuted,
              "table_counts": counts}
except Exception as exc:
    message = str(exc).replace("\n", " ")[:300]
    result = {"error": type(exc).__name__ + ": " + message}

with open("/output/result.json", "w", encoding="utf-8") as handle:
    json.dump(result, handle, ensure_ascii=False)
"""
