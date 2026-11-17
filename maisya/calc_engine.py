"""无容器环境可用的零特权确定性计算与密码学真随机引擎。"""

from __future__ import annotations

import ast
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import math
import re
import secrets
from typing import Any

_RNG = secrets.SystemRandom()
_MAX_AST_NODES = 128
_MAX_AST_DEPTH = 24
_ROLL_RE = re.compile(
    r"^\s*(\d{0,3})[dD](\d{1,5})(?:\s*(kh|kl|dh|dl)\s*(\d{1,3}))?(?:\s*([+-])\s*(\d{1,6}))?\s*$"
)
_SAFE_MATH_FUNCS = frozenset(
    {"abs", "round", "min", "max", "sum", "ceil", "floor", "sqrt", "log", "log10"}
)
_CONSTANTS = {
    "pi": Decimal(str(math.pi)),
    "e": Decimal(str(math.e)),
}


def _decimal_to_num(val: Decimal) -> int | float:
    if not val.is_finite():
        raise ValueError("计算结果超出有限数值范围")
    normalized = val.normalize()
    if normalized == normalized.to_integral_value():
        try:
            return int(normalized)
        except (ValueError, OverflowError):
            pass
    return float(val)


def _format_decimal(val: Decimal, precision: int | None = None) -> tuple[int | float, str]:
    if not val.is_finite():
        raise ValueError("计算结果无效或无穷大")
    if precision is not None:
        if type(precision) is not int or not 0 <= precision <= 12:
            raise ValueError("precision 必须为 0 到 12 的整数")
        q = Decimal("1") if precision == 0 else Decimal(f"1e-{precision}")
        rounded = val.quantize(q, rounding=ROUND_HALF_UP)
        if rounded == 0:
            rounded = Decimal("0") if precision == 0 else Decimal("0." + "0" * precision)
        return _decimal_to_num(rounded), f"{rounded:f}"
    text = format(val.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0", "-0.0"}:
        text = "0"
    return _decimal_to_num(val), text


def _count_and_check_depth(node: ast.AST, depth: int = 1) -> int:
    if depth > _MAX_AST_DEPTH:
        raise ValueError("表达式嵌套深度超过安全上限")
    total = 1
    for child in ast.iter_child_nodes(node):
        total += _count_and_check_depth(child, depth + 1)
        if total > _MAX_AST_NODES:
            raise ValueError("表达式节点过多，已超过安全上限")
    return total


def _to_decimal(val: Any) -> Decimal:
    if type(val) is bool or type(val) not in (int, float, Decimal):
        raise ValueError("变量与数值必须为有限实数")
    if isinstance(val, float) and not math.isfinite(val):
        raise ValueError("不允许 NaN 或 Inf 数值")
    try:
        dec = val if isinstance(val, Decimal) else Decimal(str(val))
    except InvalidOperation as exc:
        raise ValueError("无法解析数值") from exc
    if not dec.is_finite():
        raise ValueError("数值超出有限范围")
    return dec


def _eval_ast(node: ast.AST, env: dict[str, Decimal]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, env)
    if isinstance(node, ast.Constant):
        if type(node.value) not in (int, float):
            raise ValueError("表达式仅允许整数或浮点字面量")
        return _to_decimal(node.value)
    if isinstance(node, ast.Name):
        if node.id.startswith("_"):
            raise ValueError("禁止访问下划线标识")
        if node.id in env:
            return env[node.id]
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError(f"未定义变量：{node.id}")
    if isinstance(node, (ast.List, ast.Tuple)):
        if len(node.elts) > 128:
            raise ValueError("序列字面量元素过多")
        return [_to_decimal(_eval_ast(el, env)) for el in node.elts]
    if isinstance(node, ast.UnaryOp):
        val = _to_decimal(_eval_ast(node.operand, env))
        if isinstance(node.op, ast.UAdd):
            return +val
        if isinstance(node.op, ast.USub):
            return -val
        raise ValueError("不支持的一元运算符")
    if isinstance(node, ast.BinOp):
        left = _to_decimal(_eval_ast(node.left, env))
        right = _to_decimal(_eval_ast(node.right, env))
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError("除数不能为零")
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ValueError("除数不能为零")
            return left // right
        if isinstance(node.op, ast.Mod):
            if right == 0:
                raise ValueError("模数不能为零")
            return left % right
        if isinstance(node.op, ast.Pow):
            if abs(right) > Decimal("1000"):
                raise ValueError("幂指数绝对值不能超过 1000")
            if abs(left) > Decimal("1000000") and abs(right) > Decimal("10"):
                raise ValueError("大底数高次幂超过安全预算")
            if left < 0 and right != right.to_integral_value():
                raise ValueError("负数不支持非整数次幂")
            try:
                res = Decimal(str(float(left) ** float(right))) if right != right.to_integral_value() else (left ** right)
            except (OverflowError, InvalidOperation, ValueError) as exc:
                raise ValueError("幂运算溢出或无效") from exc
            if not res.is_finite():
                raise ValueError("幂运算结果无穷大")
            return res
        raise ValueError("不支持的二元运算符")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ValueError("仅支持白名单内置函数且不接受关键字参数")
        fname = node.func.id
        if fname not in _SAFE_MATH_FUNCS:
            raise ValueError(f"不支持函数：{fname}")
        args = [_eval_ast(arg, env) for arg in node.args]
        if fname == "abs":
            if len(args) != 1:
                raise ValueError("abs 需要 1 个参数")
            return abs(_to_decimal(args[0]))
        if fname == "round":
            if not 1 <= len(args) <= 2:
                raise ValueError("round 需要 1 到 2 个参数")
            val = _to_decimal(args[0])
            digits = int(_to_decimal(args[1])) if len(args) == 2 else 0
            if not -12 <= digits <= 12:
                raise ValueError("round 精度必须在 -12 到 12 之间")
            if digits <= 0:
                q = Decimal("1") if digits == 0 else Decimal(f"1e{-digits}")
            else:
                q = Decimal(f"1e-{digits}")
            return val.quantize(q, rounding=ROUND_HALF_UP)
        if fname in {"min", "max", "sum"}:
            if len(args) == 1 and isinstance(args[0], list):
                seq = args[0]
            else:
                seq = [_to_decimal(x) for x in args]
            if not seq:
                raise ValueError(f"{fname} 参数不能为空")
            if fname == "min":
                return min(seq)
            if fname == "max":
                return max(seq)
            return sum(seq, Decimal("0"))
        if len(args) != 1:
            raise ValueError(f"{fname} 需要 1 个参数")
        val = _to_decimal(args[0])
        if fname == "ceil":
            return Decimal(math.ceil(val))
        if fname == "floor":
            return Decimal(math.floor(val))
        if fname == "sqrt":
            if val < 0:
                raise ValueError("sqrt 不能对负数开方")
            return val.sqrt()
        fval = float(val)
        if fval <= 0:
            raise ValueError(f"{fname} 输入必须大于 0")
        return _to_decimal(math.log(fval) if fname == "log" else math.log10(fval))
    raise ValueError(f"禁止的表达式语法：{type(node).__name__}")


def eval_expression(
    expression: str,
    *,
    variables: dict[str, Any] | None = None,
    precision: int | None = None,
) -> dict[str, Any]:
    expr = str(expression or "").strip()
    if not expr or len(expr) > 1024:
        raise ValueError("expression 必须为 1 到 1024 字符的非空表达式")
    env: dict[str, Decimal] = {}
    if variables is not None:
        if not isinstance(variables, dict) or len(variables) > 32:
            raise ValueError("variables 必须是最多 32 项的字典")
        for k, v in variables.items():
            if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", k) or k.startswith("_"):
                raise ValueError(f"非法变量名：{k!r}")
            env[k] = _to_decimal(v)
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError("表达式语法无效") from exc
    _count_and_check_depth(tree)
    with localcontext() as ctx:
        ctx.prec = 28
        raw_val = _to_decimal(_eval_ast(tree, env))
    num_val, str_val = _format_decimal(raw_val, precision=precision)
    return {"result": num_val, "result_str": str_val}


def compute_stats(numbers: list[Any], *, precision: int | None = None) -> dict[str, Any]:
    if not isinstance(numbers, list) or not 1 <= len(numbers) <= 1000:
        raise ValueError("numbers 必须是 1 到 1000 项的数字列表")
    decs = [_to_decimal(x) for x in numbers]
    with localcontext() as ctx:
        ctx.prec = 28
        total = sum(decs, Decimal("0"))
        n = Decimal(len(decs))
        mean = total / n
        ordered = sorted(decs)
        mid = len(ordered) // 2
        if len(ordered) % 2 == 1:
            median = ordered[mid]
        else:
            median = (ordered[mid - 1] + ordered[mid]) / Decimal("2")
        variance = sum(((x - mean) ** 2 for x in decs), Decimal("0")) / n
        stdev = variance.sqrt()
    s_num, _ = _format_decimal(total, precision=precision)
    m_num, _ = _format_decimal(mean, precision=precision)
    med_num, _ = _format_decimal(median, precision=precision)
    sd_num, _ = _format_decimal(stdev, precision=precision)
    return {
        "count": len(decs),
        "sum": s_num,
        "mean": m_num,
        "median": med_num,
        "min": _decimal_to_num(ordered[0]),
        "max": _decimal_to_num(ordered[-1]),
        "stdev": sd_num,
    }


def roll_dice_expr(raw_expr: str) -> dict[str, Any]:
    expr = str(raw_expr or "").strip()
    match = _ROLL_RE.fullmatch(expr)
    if match is None:
        raise ValueError(f"骰点表达式格式无效：{expr!r}（示例：1d20+5、2d20kh1+3、4d6dl1）")
    count = int(match.group(1) or "1")
    sides = int(match.group(2))
    op = str(match.group(3) or "").lower()
    op_n = int(match.group(4)) if match.group(4) else 0
    sign = match.group(5) or ""
    mod_val = int(match.group(6)) if match.group(6) else 0
    modifier = -mod_val if sign == "-" else mod_val

    if not 1 <= count <= 100:
        raise ValueError("单次投掷骰数必须在 1 到 100 之间")
    if not 2 <= sides <= 10000:
        raise ValueError("骰子面数必须在 2 到 10000 之间")
    if op and (op_n <= 0 or op_n > count or (op in {"dh", "dl"} and op_n >= count)):
        raise ValueError("保留或舍弃骰子数量超出本次投掷范围")

    rolls = [_RNG.randrange(1, sides + 1) for _ in range(count)]
    indexed = sorted(enumerate(rolls), key=lambda pair: (pair[1], pair[0]))
    if op == "kh":
        keep_idx = {idx for idx, _ in indexed[-op_n:]}
    elif op == "kl":
        keep_idx = {idx for idx, _ in indexed[:op_n]}
    elif op == "dh":
        keep_idx = {idx for idx, _ in indexed[:-op_n]}
    elif op == "dl":
        keep_idx = {idx for idx, _ in indexed[op_n:]}
    else:
        keep_idx = set(range(count))

    kept = [val for i, val in enumerate(rolls) if i in keep_idx]
    dropped = [val for i, val in enumerate(rolls) if i not in keep_idx]
    total = sum(kept) + modifier
    return {
        "expr": expr,
        "count": count,
        "sides": sides,
        "rolls": rolls,
        "kept": kept,
        "dropped": dropped,
        "modifier": modifier,
        "total": total,
    }


def draw_items(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("draw 必须为配置对象")
    mode = str(spec.get("mode") or "pick").strip().lower()
    if mode not in {"pick", "shuffle", "weighted"}:
        raise ValueError("draw.mode 必须为 pick、shuffle 或 weighted")
    items = spec.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 500:
        raise ValueError("draw.items 必须是 1 到 500 项的列表")
    pool = list(items)
    if mode == "shuffle":
        _RNG.shuffle(pool)
        return {"mode": "shuffle", "items": pool, "picked": pool}
    count = spec.get("count", 1)
    if type(count) is not int or not 1 <= count <= len(pool):
        raise ValueError("draw.count 必须在 1 到 items 长度之间")
    if mode == "pick":
        picked = _RNG.sample(pool, count)
        return {"mode": "pick", "picked": picked}
    weights_raw = spec.get("weights")
    if not isinstance(weights_raw, list) or len(weights_raw) != len(pool):
        raise ValueError("weighted 模式必须提供与 items 等长的 weights")
    weights = [float(_to_decimal(w)) for w in weights_raw]
    if any(w < 0 or not math.isfinite(w) for w in weights) or sum(weights) <= 0:
        raise ValueError("weights 必须全非负且总和大于 0")
    remaining_items = list(pool)
    remaining_weights = list(weights)
    picked = []
    for _ in range(count):
        pos = _RNG.choices(range(len(remaining_items)), weights=remaining_weights, k=1)[0]
        picked.append(remaining_items.pop(pos))
        remaining_weights.pop(pos)
    return {"mode": "weighted", "picked": picked}


def run_calc_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("calc 参数必须为对象")
    has_expr = "expression" in arguments and arguments["expression"] is not None
    has_nums = "numbers" in arguments and arguments["numbers"] is not None
    has_roll = "roll" in arguments and arguments["roll"] is not None
    has_draw = "draw" in arguments and arguments["draw"] is not None
    if not (has_expr or has_nums or has_roll or has_draw):
        raise ValueError("calc 至少需要提供 expression、numbers、roll 或 draw 之一")

    out: dict[str, Any] = {}
    precision = arguments.get("precision")
    if has_expr:
        out.update(
            eval_expression(
                arguments["expression"],
                variables=arguments.get("variables"),
                precision=precision,
            )
        )
    if has_nums:
        out["stats"] = compute_stats(arguments["numbers"], precision=precision)
    if has_roll:
        raw_roll = arguments["roll"]
        exprs = [raw_roll] if isinstance(raw_roll, str) else list(raw_roll)
        if not 1 <= len(exprs) <= 32:
            raise ValueError("roll 最多一次投掷 32 组表达式")
        out["rolls"] = [roll_dice_expr(e) for e in exprs]
    if has_draw:
        out["draw"] = draw_items(arguments["draw"])
    return out
