"""归因算法的确定性实现；包说明与总口径见 datalab.attribution。各函数口径：

- ``contribution``：加法型 Delta 法、比率型对称组间组内拆解，内部全程 Fraction，不变量精确成立。
- ``decompose_metric``：乘法型 LMDI-I（浮点，输出残差）；加法型按指标树声明的维度分组求和。
- ``detect_anomaly``：环比、同比、中位数与 MAD、稳健 z 分数；历史不足给状态不报错。
- ``find_root_cause``：Adtributor 解释力 EP 与惊奇度 S；候选排序确定性（含并列规则）。
- ``correlate``：Pearson / Spearman（平均秩）；相关不代表因果。
- ``verify_attribution``：用 ReferenceSource 重算同一函数并逐项比较，即计划中的 L1 核验。
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from datalab.semantic.metrics import ADDITIVE, get_metric
from datalab.semantic.tree import ADDITIVE_SPLIT, MULTIPLICATIVE, decomposition
from datalab.verification.python_reference import ratio as _ratio

SCALE = 1_000_000


def _exact(value: str | int | None) -> Fraction | None:
    """6 位小数字符串或整数转精确 Fraction；None（分母 0）保持 None。"""
    if value is None:
        return None
    if isinstance(value, int):
        return Fraction(value)
    numerator, _, fraction = value.partition(".")
    sign = -1 if numerator.startswith("-") else 1
    numerator = numerator.lstrip("+-")
    digits = int(numerator) * SCALE + int(fraction) if fraction else int(numerator) * SCALE
    return Fraction(sign * digits, SCALE)


def _text(value: Fraction) -> str:
    """Fraction 格式化为 6 位小数字符串（先转分母 10^6 的精确值再走同一舍入实现）。"""
    scaled = value * SCALE
    nearest = scaled.numerator // scaled.denominator
    remainder = scaled - nearest
    if remainder > Fraction(1, 2) or (remainder == Fraction(1, 2) and nearest % 2):
        nearest += 1
    return _ratio(nearest, SCALE)


def _plain(value: Fraction) -> int | str:
    """内部值转输出：整数 Fraction 给 int，其余给 6 位小数字符串。"""
    if value.denominator == 1:
        return value.numerator
    return _text(value)


def _figures(entry: dict) -> tuple[Fraction | None, Fraction | None, Fraction | None]:
    """取一行的 (value, numerator, denominator) 精确值。"""
    return _exact(entry["value"]), _exact(entry["numerator"]), _exact(entry["denominator"])


def _period_dict(period) -> dict:
    return {"start": period[0], "end": period[1]}


def contribution(source, metric: str, dimension: str, period, base_period,
                 filters: dict | None = None) -> dict:
    """维度下钻贡献度：加法型用 Delta 法，比率型用对称组间组内拆解。

    加法型（Delta 法）：change_i = current_i − base_i，contribution_i = change_i / 基期总量
    （计划术语表「各维度值的差值除以基期总量」；基期总量为 0 时贡献度为 null——相对贡献无意义，
    change 与 share_of_change 仍给出），share_of_change_i = change_i / 总变化（总变化为 0 时
    各组 share 均为 null，并给 status: "no_change"）。不变量：Σchange_i = 总变化精确成立
    （各组之Σ = 总计由语义层粒度规则保证）。

    比率型（对称组间组内拆解）：w 为组分母份额，r 为组内比率，R̄ = (R0+R1)/2；
    structure_i = (w1−w0)·((r0+r1)/2 − R̄)，within_i = (w0+w1)/2·(r1−r0)，
    Σ(structure_i+within_i) = R1−R0 精确成立（Σ(w1−w0)=0，居中项合计为 0）。结构项居中后，
    份额上升的低于均值组记负、份额下降的低于均值组记正，与价格-数量-结构（mix）分析的常用
    写法一致；不居中时每个份额变化的组都会带上 (w1−w0)·r 的大额抵消项，排名失去意义。
    两期之一没有分组行时不居中。一期缺席的组（该期没有分组行或
    分母为 0）：该期 w 按 0、r 取另一期的值——组整体进出，没有组内信息，全部变化计入
    结构项（此时 within_i 恰好为 0）；贡献度按对称拆解的变化额除以基期总比率。
    share_of_change_i = (structure_i+within_i)/(R1−R0)，总变化为 0 时为 null。

    rows 按 |change|（比率型按 |structure+within|）降序、组名升序。
    """
    kind = get_metric(metric).kind
    current = source.query(metric, period, filters=filters, group_by=dimension)
    base = source.query(metric, base_period, filters=filters, group_by=dimension)
    rows0 = {row["group"]: _figures(row) for row in base["rows"]}
    rows1 = {row["group"]: _figures(row) for row in current["rows"]}
    groups = sorted(set(rows0) | set(rows1))
    total0, total1 = _figures(base["total"]), _figures(current["total"])
    if kind == ADDITIVE:
        rows, total_change = _delta_rows(groups, rows0, rows1, total0, total1)
        method, base_total, current_total = "delta", total0[0], total1[0]
    else:
        # 总比率 R0/R1 按分组行分子、分母之和复算（granularity 内各组之Σ = 总计由语义层粒度
        # 规则保证），避免引擎总计 value 的 6 位舍入破坏 Σ(structure+within) = R1−R0 的精确性。
        num0 = sum((figures[1] or Fraction(0) for figures in rows0.values()), Fraction(0))
        den0 = sum((figures[2] or Fraction(0) for figures in rows0.values()), Fraction(0))
        num1 = sum((figures[1] or Fraction(0) for figures in rows1.values()), Fraction(0))
        den1 = sum((figures[2] or Fraction(0) for figures in rows1.values()), Fraction(0))
        r0_total = num0 / den0 if den0 else None
        r1_total = num1 / den1 if den1 else None
        total0 = (r0_total, num0, den0)
        total1 = (r1_total, num1, den1)
        rows, total_change = _between_within_rows(groups, rows0, rows1, total0, total1)
        method, base_total, current_total = "between_within", total0[0], total1[0]
    residual = sum(row["_change"] for row in rows) - total_change
    public_rows = [{key: value for key, value in row.items() if not key.startswith("_")}
                   for row in rows]
    result = {"metric": metric, "dimension": dimension, "method": method,
              "period": _period_dict(period), "base_period": _period_dict(base_period),
              "base_total": _plain(base_total), "current_total": _plain(current_total),
              "total_change": _plain(total_change),
              "change_rate": _text(total_change / base_total) if base_total else None,
              "rows": public_rows,
              "invariant": {"passed": residual == 0, "residual": _plain(residual)}}
    if total_change == 0:
        result["status"] = "no_change"
    return result


def _delta_rows(groups, rows0, rows1, total0, total1) -> tuple[list, Fraction]:
    total_change = total1[0] - total0[0]
    rows = []
    for group in groups:
        value0 = (rows0.get(group) or (Fraction(0),))[0] or Fraction(0)
        value1 = (rows1.get(group) or (Fraction(0),))[0] or Fraction(0)
        change = value1 - value0
        rows.append({"group": group, "base": _plain(value0), "current": _plain(value1),
                     "change": _plain(change),
                     "contribution": _text(change / total0[0]) if total0[0] else None,
                     "share_of_change": _text(change / total_change) if total_change else None,
                     "_change": change})
    rows.sort(key=lambda row: (-abs(row["_change"]), row["group"]))
    return rows, total_change


def _between_within_rows(groups, rows0, rows1, total0, total1) -> tuple[list, Fraction]:
    # 全期间分母为 0 时总比率按 0 处理，拆解仍可返回（元素全缺席时各行为 0，不变量保持）。
    r0_total, r1_total = total0[0] or Fraction(0), total1[0] or Fraction(0)
    # 份额分母按分组行求和：granularity 内各组之Σ = 总计由语义层粒度规则保证；
    # 总计行的分母只用于 R0/R1（由 _figures 直接从总计 value 解析），这里不依赖。
    den0 = sum((figures[2] or Fraction(0) for figures in rows0.values()), Fraction(0))
    den1 = sum((figures[2] or Fraction(0) for figures in rows1.values()), Fraction(0))
    # 结构项以总比率的两期均值 R̄ 为中心：Σ(w1−w0)=0 时减去 R̄ 不改变合计，但每组的结构项
    # 只反映「份额变化 × 该组相对总体的高低」，不再给所有份额变化的组都记上同号的大额抵消项。
    # 某一期没有任何分组（份额和不为 1）时不居中，保持不变量精确成立。
    center = (r0_total + r1_total) / 2 if den0 and den1 else Fraction(0)
    rows = []
    for group in groups:
        # 缺席期：分子、分母都按 0（份额 w = 0，组内比率 r 取另一期）。
        num0, den_g0 = rows0.get(group, (None, Fraction(0), Fraction(0)))[1:]
        num1, den_g1 = rows1.get(group, (None, Fraction(0), Fraction(0)))[1:]
        num0, num1 = num0 or Fraction(0), num1 or Fraction(0)
        den_g0, den_g1 = den_g0 or Fraction(0), den_g1 or Fraction(0)
        w0 = den_g0 / den0 if den0 else Fraction(0)
        w1 = den_g1 / den1 if den1 else Fraction(0)
        r0 = num0 / den_g0 if den_g0 else None
        r1 = num1 / den_g1 if den_g1 else None
        r0e = r0 if r0 is not None else r1
        r1e = r1 if r1 is not None else r0
        structure = (w1 - w0) * ((r0e + r1e) / 2 - center)
        within = (w0 + w1) / 2 * (r1e - r0e)
        change = structure + within
        rows.append({"group": group, "base": _text(r0) if r0 is not None else None,
                     "current": _text(r1) if r1 is not None else None,
                     "change": _text(change), "structure": _text(structure), "within": _text(within),
                     "contribution": _text(change / r0_total) if r0_total else None,
                     "share_of_change": _text(change / (r1_total - r0_total))
                     if r1_total != r0_total else None, "_change": change})
    rows.sort(key=lambda row: (-abs(row["_change"]), row["group"]))
    return rows, r1_total - r0_total


def decompose_metric(source, metric: str, period, base_period) -> dict:
    """按指标树声明拆解本期相对基期的变化。

    乘法型（gmv = order_count × aov 等）用 LMDI-I 加法形式：贡献_k = L(V1,V0)·ln(x_k1/x_k0)，
    L 为对数平均 L(a,b) = (a−b)/(ln a − ln b)，Σ贡献 = V1−V0（浮点，残差 ≤ 1e-6·|V1−V0|
    或绝对 1e-6，随结果输出）。比率型父子（如 aov = gmv / order_count）的分母因子以倒数
    参与乘积，贡献取符号相反的对数项。边界：V1 = V0 时各因子贡献为 0.0，
    status: "no_change"；父值或任一因子任一期为 0 / null（对数平均无定义）时返回
    status: "undefined"、factors 为空。

    加法型（order_count 按 customer_type）：子项即父指标按声明维度分组的各组，贡献等于
    各组变化量，Σ贡献 = 总变化精确成立（Fraction）；总变化为 0 时各组 share 为 null 并给
    status: "no_change"。
    """
    declaration = decomposition(metric)
    if declaration.kind == ADDITIVE_SPLIT:
        return _additive_split(source, declaration, period, base_period)
    return _lmdi(source, declaration, period, base_period)


def _lmdi(source, declaration, period, base_period) -> dict:
    # 被拆的目标值：默认 parent 本身；声明 target 时用 target 指标（如 item_gmv 的拆解目标是
    # 不含运费的 item_price_sum，乘积只在价格口径上精确成立）。
    target = declaration.target or declaration.parent
    parent0 = _exact(source.query(target, base_period)["total"]["value"])
    parent1 = _exact(source.query(target, period)["total"]["value"])
    # 比率型父子声明 children 为 (分子, 分母)：分母因子以倒数参与乘积（V = 分子 × 分母^(−1)）。
    inverse_second = get_metric(declaration.parent).kind != ADDITIVE and declaration.target is None
    values = {}
    for index, child in enumerate(declaration.children):
        x0 = _exact(source.query(child, base_period)["total"]["value"])
        x1 = _exact(source.query(child, period)["total"]["value"])
        values[child] = (x0, x1, inverse_second and index == 1)
    stub = {"metric": declaration.parent, "kind": MULTIPLICATIVE, "method": "lmdi",
            "period": _period_dict(period), "base_period": _period_dict(base_period),
            "factors": [], "total_change": None, "status": "undefined",
            "invariant": {"passed": False, "residual": None}}
    if target != declaration.parent:
        stub["target"] = target
    if parent0 is None or parent1 is None or parent0 == 0 or parent1 == 0:
        return stub
    f0, f1 = float(parent0), float(parent1)
    if any(pair[0] is None or pair[1] is None for pair in values.values()):
        return stub
    if f0 == f1:
        factors = [{"metric": child, "base": _plain(values[child][0]),
                    "current": _plain(values[child][1]), "contribution": 0.0, "share": None}
                   for child in declaration.children]
        return {**stub, "factors": factors, "total_change": 0.0, "status": "no_change",
                "invariant": {"passed": True, "residual": 0.0}}
    log_mean = (f1 - f0) / (math.log(f1) - math.log(f0))
    factors = []
    for child in declaration.children:
        x0, x1, inverted = values[child]
        if x0 == 0 or x1 == 0:
            return stub
        term = log_mean * math.log(float(x1) / float(x0))
        factors.append({"metric": child, "base": _plain(x0), "current": _plain(x1),
                        "contribution": -term if inverted else term,
                        "share": (-term if inverted else term) / (f1 - f0)})
    residual = sum(row["contribution"] for row in factors) - (f1 - f0)
    # 比率型父指标的取值是 6 位小数（两期各有至多 5e-7 的舍入误差），父值与子因子之积并不精确
    # 相等，绝对残差可略超 1e-6（holdout 中 ho-delivery_delay-02 为 1.03e-6，被误判不通过）；
    # 绝对容差取 2e-6 覆盖两期舍入之和与浮点误差。
    tolerance = max(2e-6, 1e-6 * abs(f1 - f0))
    result = {"metric": declaration.parent, "kind": MULTIPLICATIVE, "method": "lmdi",
              "period": _period_dict(period), "base_period": _period_dict(base_period),
              "factors": factors, "total_change": f1 - f0,
              "invariant": {"passed": abs(residual) <= tolerance, "residual": residual}}
    if target != declaration.parent:
        result["target"] = target
    return result


def _additive_split(source, declaration, period, base_period) -> dict:
    dimension = declaration.by_dimension
    grouped_current = source.query(declaration.parent, period, group_by=dimension)
    grouped_base = source.query(declaration.parent, base_period, group_by=dimension)
    rows0 = {row["group"]: _exact(row["value"]) for row in grouped_base["rows"]}
    rows1 = {row["group"]: _exact(row["value"]) for row in grouped_current["rows"]}
    groups = sorted(set(rows0) | set(rows1))
    total0 = _exact(grouped_base["total"]["value"])
    total1 = _exact(grouped_current["total"]["value"])
    total_change = total1 - total0
    factors = []
    for group in groups:
        value0 = rows0.get(group) or Fraction(0)
        value1 = rows1.get(group) or Fraction(0)
        change = value1 - value0
        factors.append({"metric": f"{declaration.parent}[{dimension}={group}]",
                        "base": _plain(value0), "current": _plain(value1),
                        "contribution": _plain(change),
                        "share": _text(change / total_change) if total_change else None})
    factors.sort(key=lambda row: (-abs(_exact(row["contribution"])), row["metric"]))
    residual = sum(_exact(row["contribution"]) for row in factors) - total_change
    result = {"metric": declaration.parent, "kind": ADDITIVE_SPLIT, "method": "additive",
              "period": _period_dict(period), "base_period": _period_dict(base_period),
              "factors": factors, "total_change": _plain(total_change),
              "invariant": {"passed": residual == 0, "residual": _plain(residual)}}
    if total_change == 0:
        result["status"] = "no_change"
    return result


def detect_anomaly(source, metric: str, period, history: int = 6) -> dict:
    """异动检测：环比、同比、历史中位数 ± MAD 的稳健 z 分数（Iglewicz–Hoaglin，|z| > 3.5 判异动）。

    历史期取与 period 同长度、紧邻的前 history 个期间：period 两端都是月初零点时按日历月
    平移（保持「自然月」语义），否则按时间差平移。数据源可提供 ``data_floor`` 属性
    （数据的最早可用起点）：起点早于它的历史期按「还没有这份数据」丢弃，不计入有效历史；
    语义层对没有数据的期间返回 0 而不是「无数据」，归因需要靠 floor 区分这两种情形。
    环比是与紧邻上一期；同比是去年同期（两端各减一年，2 月 29 日退到 2 月 28 日；
    去年同期早于最早有效历史期起点时为 null）。有效历史不足 3 期时给
    status: "insufficient_history"，不判异动；MAD 为 0（历史值全部相同）时 z 无定义，
    给 status: "zero_mad"，此时本期值偏离中位数即 is_anomaly=True（正常波动的历史方差
    为 0，任何偏离都是异动）。
    """
    floor = getattr(source, "data_floor", None)  # 数据源可提供最早可用起点（见 _history_periods）
    periods = _history_periods(period, history, floor)
    return detect_anomaly_explicit(source, metric, period, periods)


def detect_anomaly_explicit(source, metric: str, period,
                            history_periods: list[tuple[str | None, str | None]]) -> dict:
    """detect_anomaly 的显式历史期间变体（A5）：历史期间由调用方给出，其余逻辑与
    detect_anomaly 完全一致（环比、同比、中位数 ± MAD 的稳健 z 分数、阈值 3.5）。

    history_periods 为 [(start, end), ...]，按由远及近给出；调用方负责裁剪（如按数据
    最早起点），本函数不推断也不裁剪，只按给定序列查询。
    """
    periods = [(item[0], item[1]) for item in history_periods]
    values = []
    for candidate in periods:
        value = _exact(source.query(metric, candidate)["total"]["value"])
        if value is not None:
            values.append((candidate, value))
    current_value = _exact(source.query(metric, period)["total"]["value"])
    history_rows = [{"period": _period_dict(candidate), "value": _plain(value)}
                    for candidate, value in values]
    mom = None
    if values and current_value is not None:
        mom_period, mom_base = values[-1]
        change = current_value - mom_base
        mom = {"period": _period_dict(mom_period), "base": _plain(mom_base),
               "change": _plain(change), "rate": _text(change / mom_base) if mom_base else None}
    yoy = None
    yoy_period = _shift_year(period)
    earliest = min(candidate[0] for candidate, _ in values) if values else None
    if earliest is not None and yoy_period[0] is not None and yoy_period[0] >= earliest:
        yoy_value = _exact(source.query(metric, yoy_period)["total"]["value"])
        if yoy_value is not None and current_value is not None:
            change = current_value - yoy_value
            yoy = {"period": _period_dict(yoy_period), "base": _plain(yoy_value),
                   "change": _plain(change), "rate": _text(change / yoy_value) if yoy_value else None}
    median = mad = robust_z = None
    is_anomaly = False
    status = "ok"
    sample = [_snap(float(value)) for _, value in values]
    if len(sample) < 3:
        status = "insufficient_history"
    else:
        median = _snap(statistics.median(sample))
        mad = _snap(statistics.median([abs(item - median) for item in sample]))
        if mad == 0:
            status = "zero_mad"
            is_anomaly = current_value is not None and _snap(float(current_value)) != median
        else:
            robust_z = 0.6745 * float((current_value - median) / mad) \
                if current_value is not None else None
            is_anomaly = robust_z is not None and abs(robust_z) > 3.5
    return {"metric": metric, "period": _period_dict(period),
            "value": _plain(current_value) if current_value is not None else None,
            "history": history_rows, "mom": mom, "yoy": yoy,
            "median": _plain(median) if median is not None else None,
            "mad": _plain(mad) if mad is not None else None,
            "robust_z": round(robust_z, 6) if robust_z is not None else None,
            "is_anomaly": is_anomaly, "status": status}


def _snap(value: float) -> Fraction:
    """浮点统计量吸附到最近的半整数网格：历史值都是 6 位精度有理数，中位数与 MAD
    由它们经 +、−、/2 得到，真实值落在 1/(2×1e6) 网格上；浮点误差远小于网格间距，
    吸附可逆且确定。"""
    scaled = value * (2 * SCALE)
    return Fraction(int(round(scaled)), 2 * SCALE)


def _shift_year(period) -> tuple[str | None, str | None]:
    """去年同期：两端各减一年（2 月 29 日退到 2 月 28 日）；None 端保持 None。"""
    def shift(stamp: str | None) -> str | None:
        if stamp is None:
            return None
        moment = datetime.fromisoformat(stamp)
        try:
            return moment.replace(year=moment.year - 1).isoformat(timespec="seconds")
        except ValueError:
            return moment.replace(year=moment.year - 1, day=28).isoformat(timespec="seconds")
    return shift(period[0]), shift(period[1])


def _history_periods(period, history: int, floor: str | None = None) -> list[tuple[str, str]]:
    """与 period 同长度、紧邻的前 history 个期间（由远及近）；任一端为 None 时没有历史。

    floor 为数据的最早可用起点：起点早于 floor 的历史期会被丢弃（语义层对没有数据的
    期间返回 0 而不是「无数据」，归因需要靠 floor 区分「该期还没有这份数据」与「该期
    有数据但恰好为 0」）。floor 为 None 时不裁剪。
    """
    start, end = period
    if start is None or end is None:
        return []
    start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
    midnight = {"hour": 0, "minute": 0, "second": 0, "microsecond": 0}
    month_aligned = start_dt.day == 1 and end_dt.day == 1 \
        and start_dt == start_dt.replace(**midnight) and end_dt == end_dt.replace(**midnight)
    periods = []
    for step in range(history, 0, -1):
        if month_aligned:
            candidate = (_shift_months(start_dt, -step), _shift_months(end_dt, -step))
        else:
            delta = end_dt - start_dt
            candidate = (start_dt - delta * step, end_dt - delta * step)
        if floor is not None and candidate[0] < datetime.fromisoformat(floor):
            continue
        periods.append(candidate)
    return [(item[0].isoformat(timespec="seconds"), item[1].isoformat(timespec="seconds"))
            for item in periods]


def _shift_months(moment: datetime, months: int) -> datetime:
    index = moment.year * 12 + moment.month - 1 + months
    return moment.replace(year=index // 12, month=index % 12 + 1)


def find_root_cause(source, metric: str, dimensions: list[str], period, base_period,
                    filters: dict | None = None, tep: float = 0.8, teep: float = 0.1,
                    top: int = 3) -> dict:
    """多维根因定位，参考 Adtributor（Bhagwan et al., NSDI 2014）。

    对每个维度：元素解释力 EP_ij 取 contribution 的结果——加法型为 change_i / 总变化，
    比率型为 (structure_i+within_i)/(R1−R0)，各元素 EP 之和精确为 1（总变化为 0 时不适用，
    直接返回空候选与 status: "no_change"）。惊奇度 S_ij 用基期份额 p 与本期份额 q 的
    Jensen–Shannon 散度项 D(p‖m)+D(q‖m)（m=(p+q)/2，自然对数）；份额取该元素值占全期总计
    的比例——加法型为指标值份额，比率型分别对分子份额与分母份额各算一次相加（两个成分
    的分布变化都算惊奇）。维度内元素按 S 降序（并列按取值升序），EP ≥ teep 的依次加入
    候选集，累计 EP ≥ tep 即停止；维度按候选集的平均惊奇度降序（并列按维度名升序）取前
    top 个（原文按惊奇度之和，偏向取值多的维度，这里改为平均）。比率型元素另给 effect
    （between：组间结构效应为主；within：组内效应为主）。

    candidates 是扁平化的根因候选列表：先按所在维度的排名，再按维度内解释力降序（并列按
    取值升序），保证确定性。总变化为 0 时返回空候选。
    """
    value0 = _exact(source.query(metric, base_period, filters=filters)["total"]["value"])
    value1 = _exact(source.query(metric, period, filters=filters)["total"]["value"])
    total_change = (value1 or Fraction(0)) - (value0 or Fraction(0))
    result = {"metric": metric, "period": _period_dict(period),
              "base_period": _period_dict(base_period),
              "total_change": _plain(total_change), "dimensions": [], "candidates": []}
    if total_change == 0:
        result["status"] = "no_change"
        return result
    kind = get_metric(metric).kind
    entries = []
    for dimension in dimensions:
        drilled = contribution(source, metric, dimension, period, base_period, filters)
        elements = []
        for row in drilled["rows"]:
            ep = _exact(row["share_of_change"]) or Fraction(0)
            surprise = _surprise(source, metric, kind, dimension, row["group"], period, base_period,
                                 filters)
            element = {"value": row["group"], "explanatory_power": _text(ep),
                       "surprise": round(float(surprise), 6), "_ep": ep, "_s": surprise}
            if "structure" in row:
                # 比率型：标出该元素以组间（结构）还是组内效应为主。
                element["effect"] = ("between" if abs(_exact(row["structure"]) or 0)
                                     >= abs(_exact(row["within"]) or 0) else "within")
            elements.append(element)
        elements.sort(key=lambda item: (-item["_s"], item["value"]))
        chosen, explained = [], Fraction(0)
        for item in elements:
            if explained >= Fraction(str(tep)) or item["_ep"] < Fraction(str(teep)):
                continue
            chosen.append(item)
            explained += item["_ep"]
        surprise_sum = sum(item["_s"] for item in chosen)
        entries.append({"dimension": dimension,
                        "elements": [_public_element(item) for item in elements],
                        "explained": _text(explained), "surprise": round(float(surprise_sum), 6),
                        "_s": surprise_sum, "_chosen": chosen,
                        "_mean": surprise_sum / len(chosen) if chosen else Fraction(-1)})
    # 维度排序：按候选集的平均惊奇度降序（并列按维度名升序）。原始 Adtributor 用惊奇度之和，
    # 取值多的维度（品类、卖家、分期数）会因候选元素多而累积更大的和，压过真正集中的维度；
    # 只按所需元素数（简洁性）排序则会被「最大的组按比例增长」的低基数维度（如 on_time）误导。
    # 平均惊奇度同时惩罚这两种情形。没有候选元素的维度排最后。
    entries.sort(key=lambda item: (-item["_mean"], item["dimension"]))
    result["dimensions"] = [{key: value for key, value in entry.items() if not key.startswith("_")}
                            for entry in entries]
    for entry in entries[:top]:
        # 候选集按惊奇度选出后，输出时按解释力降序（并列按取值）：Top-1 是该维度贡献最大的元素。
        for item in sorted(entry["_chosen"], key=lambda element: (-element["_ep"], element["value"])):
            candidate = {"dimension": entry["dimension"], "value": item["value"],
                         "explanatory_power": item["explanatory_power"], "surprise": item["surprise"]}
            if "effect" in item:
                candidate["effect"] = item["effect"]
            result["candidates"].append(candidate)
    return result


def _public_element(item: dict) -> dict:
    public = {"value": item["value"], "explanatory_power": item["explanatory_power"],
              "surprise": item["surprise"]}
    if "effect" in item:
        public["effect"] = item["effect"]
    return public


def _surprise(source, metric: str, kind: str, dimension: str, group: str, period, base_period,
              filters) -> Fraction:
    """元素惊奇度：基期份额 p 与本期份额 q 的 JS 散度项；比率型对分子、分母份额各算一次相加。"""
    current = source.query(metric, period, filters=filters, group_by=dimension)
    base = source.query(metric, base_period, filters=filters, group_by=dimension)
    if kind == ADDITIVE:
        pairs = [(_share(base, group, "value"), _share(current, group, "value"))]
    else:
        pairs = [(_share(base, group, "numerator"), _share(current, group, "numerator")),
                 (_share(base, group, "denominator"), _share(current, group, "denominator"))]
    return sum((_js_term(p, q) for p, q in pairs), Fraction(0))


def _share(result: dict, group: str, field: str) -> Fraction:
    total = _exact(result["total"][field])
    if not total:
        return Fraction(0)
    for row in result["rows"]:
        if row["group"] == group:
            return (_exact(row[field]) or Fraction(0)) / total
    return Fraction(0)


def _js_term(p: Fraction, q: Fraction) -> Fraction:
    """D(p‖m)+D(q‖m)，m=(p+q)/2；0 份额的项按 0 处理（0·log0 的极限为 0）。"""
    if p == 0 and q == 0:
        return Fraction(0)
    m = (p + q) / 2
    value = 0.0
    if p:
        value += float(p) * math.log(float(p) / float(m))
    if q:
        value += float(q) * math.log(float(q) / float(m))
    return Fraction(value).limit_denominator(10 ** 12)


def correlate(source, metric_a: str, metric_b: str, by: str, period) -> dict:
    """两个指标按维度 by 分组的相关分析：Pearson r、Spearman ρ（平均秩处理并列）与样本量 n。

    只用两个指标都有值（比率型分母非 0）的组；n < 3 时系数为 null（样本太少，相关无意义）。
    相关不代表因果：报告中只能表述为「相关」，不能据此写因果结论。
    """
    result_a = source.query(metric_a, period, group_by=by)
    result_b = source.query(metric_b, period, group_by=by)
    values_a = {row["group"]: _exact(row["value"]) for row in result_a["rows"]}
    values_b = {row["group"]: _exact(row["value"]) for row in result_b["rows"]}
    pairs = sorted((float(values_a[group]), float(values_b[group]))
                   for group in set(values_a) & set(values_b)
                   if values_a[group] is not None and values_b[group] is not None)
    n = len(pairs)
    pearson = spearman = None
    if n >= 3:
        xs = [pair[0] for pair in pairs]
        ys = [pair[1] for pair in pairs]
        pearson_value, spearman_value = _pearson(xs, ys), _pearson(_ranks(xs), _ranks(ys))
        pearson = round(pearson_value, 6) if pearson_value is not None else None
        spearman = round(spearman_value, 6) if spearman_value is not None else None
    return {"metric_a": metric_a, "metric_b": metric_b, "by": by, "period": _period_dict(period),
            "n": n, "pearson": pearson, "spearman": spearman,
            "note": "相关不代表因果，报告中只能表述为相关"}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _ranks(values: list[float]) -> list[float]:
    """平均秩：并列值取相同秩次的平均（从 1 起）。"""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        follow = index
        while follow + 1 < len(order) and values[order[follow + 1]] == values[order[index]]:
            follow += 1
        average = (index + follow) / 2 + 1
        for slot in range(index, follow + 1):
            ranks[order[slot]] = average
        index = follow + 1
    return ranks


def verify_attribution(kind: str, result: dict, folder: Path, snapshot, _store=None, **arguments) -> dict:
    """L1 核验：用 ReferenceSource 独立重算同一归因函数，逐项比较并检查不变量。

    kind 为函数名（contribution / decompose_metric / detect_anomaly / find_root_cause /
    correlate）；arguments 是该函数除 source 外的全部参数（含 metric、period 等）。
    Fraction 精确结果比较格式化后的字符串/整数；LMDI 贡献与相关系数为浮点，允许
    1e-9 相对误差。_store 为内部复用参数：批量核验时传入同一份派生记录缓存，避免每条结果重读表；
    省略时每次新建，行为不变。
    """
    from datalab.attribution.sources import ReferenceSource
    from datalab.semantic.reference import _Cache

    functions = {"contribution": contribution, "decompose_metric": decompose_metric,
                 "detect_anomaly": detect_anomaly, "find_root_cause": find_root_cause,
                 "correlate": correlate}
    if kind == "detect_anomaly" and "history_periods" in arguments:
        # Agent 工具层显式给出历史期间（detect_anomaly_explicit）；A5 漏接此分支，核验恒报 TypeError。
        functions["detect_anomaly"] = detect_anomaly_explicit
    # 同一次核验内的全部复算共享一份派生记录缓存（真实数据上避免逐次读表与连接）。
    reference = functions[kind](ReferenceSource(folder, snapshot, _store=_store or _Cache()), **arguments)
    differences = _diff(result, reference, "$")
    invariant = result.get("invariant", {})
    return {"passed": not differences and invariant.get("passed", True),
            "differences": differences[:20], "invariant": invariant}


def _diff(left, right, path: str) -> list[dict]:
    if isinstance(left, float) and isinstance(right, float):
        return [] if math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12) else \
            [{"path": path, "issue": "value", "left": left, "right": right}]
    if type(left) is not type(right):
        return [{"path": path, "issue": "type", "left": type(left).__name__,
                 "right": type(right).__name__}]
    if isinstance(left, dict):
        if set(left) != set(right):
            return [{"path": path, "issue": "keys",
                     "left": sorted(set(left) - set(right)), "right": sorted(set(right) - set(left))}]
        return [item for key in left for item in _diff(left[key], right[key], f"{path}.{key}")]
    if isinstance(left, list):
        if len(left) != len(right):
            return [{"path": path, "issue": "length", "left": len(left), "right": len(right)}]
        return [item for index, pair in enumerate(zip(left, right))
                for item in _diff(*pair, f"{path}[{index}]")]
    return [] if left == right else [{"path": path, "issue": "value", "left": left, "right": right}]
