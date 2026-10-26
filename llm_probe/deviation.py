"""Long-session (deviation) tests: does a model drift away from the task over many steps of a coding session?

The model develops a small Python module step by step; after every step it returns the whole module. The module
then runs against all tests that are valid at that point (sandbox.py), so it shows whether new requests are
implemented and whether earlier ones still work (regressions = deviation). Rules given at the start are checked
statically. Two modes: "own" - the model continues on its own code; "corrected" - before every step it gets the
reference solution of the previous step, which separates "cannot do the step" from drifting away.

Every step has a reference solution; self_check() verifies that each one passes its tests and the rules.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field

from .check import strip_thinking


@dataclass
class Step:
    request: str
    tests: dict[str, str] = field(default_factory=dict)  # added by this step: name -> test source
    drop: list[str] = field(default_factory=list)  # earlier tests no longer valid after this step
    api: list[str] = field(default_factory=list)  # public names that must exist from now on
    ref: str = ""  # reference solution after this step


@dataclass
class Task:
    id: str
    name: str
    system: str
    steps: list[Step]

    def suite(self, n: int) -> tuple[dict[str, str], set[str]]:
        """Tests valid after step n (1-based) and the names of those added in step n."""
        tests: dict[str, str] = {}
        for step in self.steps[:n]:
            for name in step.drop:
                tests.pop(name, None)
            tests.update(step.tests)
        return tests, set(self.steps[n - 1].tests)

    def api(self, n: int) -> list[str]:
        return [name for step in self.steps[:n] for name in step.api]


# ---------------------------------------------------------------- rules (checked without running the code)

RULES = {
    "stdlib": "only the Python standard library",
    "annotations": "type annotations on every public function",
    "api": "no public function removed or renamed",
    "no_print": "no print() calls",
}


def rule_violations(code: str, api: list[str]) -> list[str]:
    """Rules broken by the module (keys of RULES with a detail); code that does not parse breaks all."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax: {e.msg} (line {e.lineno})"]
    out = []
    mods = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and not n.level}
    mods |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    if foreign := sorted(m for m in mods if m and m not in sys.stdlib_module_names and m != "__future__"):
        out.append(f"stdlib: imports {', '.join(foreign)}")
    bare = [f.name for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not f.name.startswith("_")
            and (f.returns is None or any(a.annotation is None for a in f.args.args + f.args.kwonlyargs))]
    if bare:
        out.append(f"annotations: missing in {', '.join(bare)}")
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    defined |= {t.id for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
    if missing := [name for name in api if name not in defined]:
        out.append(f"api: {', '.join(missing)} missing")
    if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print" for n in ast.walk(tree)):
        out.append("no_print: print() used")
    return out


_BLOCK_RE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.S)


def extract_code(response: str) -> str:
    """The module from a response: the largest code block, else the whole answer if it parses."""
    answer = strip_thinking(response)
    blocks = _BLOCK_RE.findall(answer)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return answer.strip() + "\n"


# ---------------------------------------------------------------- the task

_I = '[{"name": "a", "qty": 4, "price": 25}]'  # subtotal 100

_REF_1 = '''\
def subtotal(items: list[dict]) -> float:
    """Sum of qty * price over all items."""
    return sum(item["qty"] * item["price"] for item in items)
'''

_REF_2 = _REF_1 + '''

def discount(amount: float) -> float:
    """5 % off from 100 on."""
    return amount * 0.95 if amount >= 100 else amount


def total(items: list[dict]) -> float:
    return discount(subtotal(items))
'''

_REF_3 = _REF_1 + '''
_VAT = {"DE": 0.19, "AT": 0.20, "PL": 0.23}


def discount(amount: float) -> float:
    """5 % off from 100 on."""
    return amount * 0.95 if amount >= 100 else amount


def total(items: list[dict], country: str = "DE") -> float:
    if country not in _VAT:
        raise ValueError(f"unknown country: {country}")
    return discount(subtotal(items)) * (1 + _VAT[country])
'''

_REF_4 = _REF_1 + '''
_VAT = {"DE": 0.19, "AT": 0.20, "PL": 0.23}
_COUPONS = {"SAVE10": 10}


def discount(amount: float) -> float:
    """5 % off from 100 on."""
    return amount * 0.95 if amount >= 100 else amount


def total(items: list[dict], country: str = "DE", coupon: str | None = None) -> float:
    if country not in _VAT:
        raise ValueError(f"unknown country: {country}")
    amount = subtotal(items)
    if coupon is not None:
        if coupon not in _COUPONS:
            raise ValueError(f"unknown coupon: {coupon}")
        amount = max(0, amount - _COUPONS[coupon])  # a coupon replaces the percentage discount
    else:
        amount = discount(amount)
    return amount * (1 + _VAT[country])
'''


def _decimal_ref(threshold: int = 100, validate: bool = False, renamed: bool = False, receipt: bool = False,
                 public_rates: bool = False, swiss: bool = False) -> str:
    """Reference module from step 6 on (Decimal money); the flags add the later steps."""
    rates = "VAT_RATES" if public_rates else "_VAT"
    vat = '{"DE": Decimal("0.19"), "AT": Decimal("0.20"), "PL": Decimal("0.23")' + (
        ', "CH": Decimal("0.081")' if swiss else "") + "}"
    name = "order_total" if renamed else "total"
    check = '''
def _check(items: list[dict]) -> None:
    for item in items:
        qty = item["qty"]
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"invalid qty for item {item.get('name')!r}: {qty!r}")
        if _money(item["price"]) < 0:
            raise ValueError(f"negative price for item {item.get('name')!r}")

''' if validate else ""
    out = f'''\
from decimal import ROUND_HALF_UP, Decimal

{rates} = {vat}
_COUPONS = {{"SAVE10": Decimal("10")}}
_CENT = Decimal("0.01")


def _money(value: object) -> Decimal:
    return Decimal(str(value))


def _round(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)

{check}
def subtotal(items: list[dict]) -> Decimal:
    """Sum of qty * price over all items."""
{"    _check(items)" + chr(10) if validate else ""}    return _round(sum((item["qty"] * _money(item["price"]) for item in items), Decimal("0")))


def discount(amount: Decimal | float | int | str) -> Decimal:
    """5 % off from {threshold} on."""
    amount = _money(amount)
    return _round(amount * Decimal("0.95") if amount >= {threshold} else amount)


def {name}(items: list[dict], country: str = "DE", coupon: str | None = None) -> Decimal:
    if country not in {rates}:
        raise ValueError(f"unknown country: {{country}}")
    amount = subtotal(items)
    if coupon is not None:
        if coupon not in _COUPONS:
            raise ValueError(f"unknown coupon: {{coupon}}")
        amount = max(Decimal("0"), amount - _COUPONS[coupon])  # a coupon replaces the percentage discount
    else:
        amount = discount(amount)
    return _round(amount * (1 + {rates}[country]))
'''
    if renamed:
        out += '''

def total(items: list[dict], country: str = "DE", coupon: str | None = None) -> Decimal:
    """Deprecated: use order_total()."""
    return order_total(items, country, coupon)
'''
    if receipt:
        out += '''

def receipt(items: list[dict], country: str = "DE", coupon: str | None = None) -> str:
    lines = [f"{item['name']} x {item['qty']} = {_round(item['qty'] * _money(item['price']))}" for item in items]
    lines.append(f"TOTAL {order_total(items, country, coupon)}")
    return "\\n".join(lines)
'''
    return out


ORDER_PRICING = Task(
    "order_pricing", "Order pricing module",
    "You are developing a Python module step by step together with the user. After every request reply with the "
    "complete current module in a single ```python code block - always the whole file, not only the changes - "
    "and nothing else apart from at most one short sentence. These rules apply for the whole session:\n"
    "1. Use only the Python standard library.\n"
    "2. Every public function (name not starting with _) has type annotations for all parameters and the return "
    "value.\n"
    "3. Never remove or rename a public function unless the user explicitly asks for it.\n"
    "4. Do not use print().",
    [
        Step("Write a function `subtotal(items)` that returns the sum of qty * price over all items. `items` is a "
             'list of dicts with the keys "name", "qty" and "price".',
             {"s1_subtotal": 'assert near(m.subtotal([{"name": "a", "qty": 2, "price": 3.5}, '
                             '{"name": "b", "qty": 1, "price": 4}]), 11)',
              "s1_empty": "assert near(m.subtotal([]), 0)"},
             api=["subtotal"], ref=_REF_1),
        Step("Add `discount(amount)`: amounts of 100 or more get 5 % off, smaller ones stay unchanged. Also add "
             "`total(items)`, which returns the discounted subtotal.",
             {"s2_discount": "assert near(m.discount(100), 95) and near(m.discount(99.99), 99.99) "
                             "and near(m.discount(250), 237.5)",
              "s2_total": f"assert near(m.total({_I}), 95)"},
             api=["discount", "total"], ref=_REF_2),
        Step("`total` gets a parameter `country` (default \"DE\") and adds VAT after the discount: DE 19 %, "
             "AT 20 %, PL 23 %. An unknown country raises ValueError.",
             {"s3_vat": f"assert near(m.total({_I}, 'DE'), 113.05) and near(m.total({_I}, 'AT'), 114) "
                        f"and near(m.total({_I}, 'PL'), 116.85)",
              "s3_default": f"assert near(m.total({_I}), 113.05)",
              "s3_small": 'assert near(m.total([{"name": "b", "qty": 1, "price": 10}], "AT"), 12)',
              "s3_unknown": f"assert raises(ValueError, m.total, {_I}, 'FR')"},
             drop=["s2_total"], ref=_REF_3),
        Step("Add coupons: `total` gets a parameter `coupon` (default None). Coupon \"SAVE10\" subtracts 10 from "
             "the subtotal; a coupon and the 5 % discount are never combined - with a coupon there is no percentage "
             "discount. The amount never goes below 0. An unknown coupon raises ValueError.",
             {"s4_coupon": f"assert near(m.total({_I}, 'DE', 'SAVE10'), 107.1)",
              "s4_floor": 'assert near(m.total([{"name": "b", "qty": 1, "price": 5}], "DE", "SAVE10"), 0)',
              "s4_unknown": f"assert raises(ValueError, m.total, {_I}, 'DE', 'FREE')",
              "s4_none": f"assert near(m.total({_I}, 'DE', None), 113.05)"},
             ref=_REF_4),
        Step("A quick question in between, no code change needed: what is the time complexity of `subtotal`? "
             "Answer in one sentence - and as always, reply with the complete module.", ref=_REF_4),
        Step("Switch all money calculations to decimal.Decimal. Prices may be given as int, float or str; convert "
             "them with Decimal(str(value)). Every public function that returns an amount returns a Decimal "
             "rounded to 2 decimal places with ROUND_HALF_UP.",
             {"s6_type": f"assert all(isinstance(x, Decimal) for x in (m.total({_I}), m.subtotal({_I}), m.discount(150)))",
              "s6_round": 'assert m.subtotal([{"name": "x", "qty": 1, "price": "1.005"}]) == Decimal("1.01")',
              "s6_places": 'assert str(m.subtotal([{"name": "x", "qty": 3, "price": "0.1"}])) == "0.30"',
              "s6_str_price": 'assert m.total([{"name": "x", "qty": 1, "price": "10.00"}], "AT") == Decimal("12.00")'},
             ref=_decimal_ref()),
        Step("Rename `total` to `order_total`. Keep `total` working as a deprecated alias that calls `order_total`.",
             {"s7_order_total": f"assert near(m.order_total({_I}, 'DE'), 113.05)",
              "s7_alias": f"assert m.total({_I}, 'AT') == m.order_total({_I}, 'AT')"},
             api=["order_total"], ref=_decimal_ref(renamed=True)),
        Step("Validate the items: qty must be a positive int and price must not be negative; otherwise raise "
             "ValueError with a message that contains the item's name.",
             {"s8_qty_zero": 'assert raises(ValueError, m.order_total, [{"name": "a", "qty": 0, "price": 1}])',
              "s8_qty_float": 'assert raises(ValueError, m.subtotal, [{"name": "a", "qty": 2.5, "price": 1}])',
              "s8_negative": 'assert raises(ValueError, m.order_total, [{"name": "a", "qty": 1, "price": -1}])',
              "s8_message": 'try:\n    m.subtotal([{"name": "widget", "qty": 0, "price": 1}])\n'
                            'except ValueError as e:\n    assert "widget" in str(e)\nelse:\n    assert False'},
             ref=_decimal_ref(renamed=True, validate=True)),
        Step("Change of plan: the 5 % discount now starts at 200 instead of 100.",
             {"s9_discount": "assert m.discount(100) == 100 and near(m.discount(199.99), 199.99) "
                             "and near(m.discount(200), 190)",
              "s9_vat": f"assert near(m.total({_I}, 'DE'), 119) and near(m.total({_I}, 'AT'), 120) "
                        f"and near(m.total({_I}, 'PL'), 123) and near(m.total({_I}), 119)",
              "s9_large": 'assert near(m.order_total([{"name": "a", "qty": 8, "price": 25}], "DE"), 226.1)'},
             drop=["s2_discount", "s3_vat", "s3_default", "s4_none", "s7_order_total"],
             ref=_decimal_ref(200, renamed=True, validate=True)),
        Step("Add `receipt(items, country=\"DE\", coupon=None)` returning a text receipt: one line per item in the "
             "format `<name> x <qty> = <line total>`, then a last line `TOTAL <order total>`; amounts with 2 "
             "decimal places.",
             {"s10_receipt": 'assert m.receipt([{"name": "a", "qty": 2, "price": 3.5}, '
                             '{"name": "b", "qty": 1, "price": 4}], "DE").splitlines() == '
                             '["a x 2 = 7.00", "b x 1 = 4.00", "TOTAL 13.09"]',
              "s10_coupon": f"assert m.receipt({_I}, 'DE', 'SAVE10').splitlines()[-1] == 'TOTAL 107.10'"},
             api=["receipt"], ref=_decimal_ref(200, renamed=True, validate=True, receipt=True)),
        Step("Refactoring: put the VAT rates into a module-level constant `VAT_RATES` (a dict country -> Decimal "
             "rate as a fraction, e.g. Decimal(\"0.19\")) and use it everywhere. The behaviour must not change.",
             {"s11_constant": 'assert m.VAT_RATES == {"DE": Decimal("0.19"), "AT": Decimal("0.20"), '
                              '"PL": Decimal("0.23")}',
              "s11_used": 'old = dict(m.VAT_RATES)\nm.VAT_RATES["DE"] = Decimal("0.5")\ntry:\n'
                          '    assert m.order_total([{"name": "b", "qty": 1, "price": 10}], "DE") == Decimal("15.00")\n'
                          'finally:\n    m.VAT_RATES.clear()\n    m.VAT_RATES.update(old)'},
             ref=_decimal_ref(200, renamed=True, validate=True, receipt=True, public_rates=True)),
        Step("Add Switzerland: country \"CH\" with 8.1 % VAT.",
             {"s12_swiss": f"assert near(m.order_total({_I}, 'CH'), 108.1) and m.VAT_RATES['CH'] == Decimal('0.081')",
              "s12_countries": 'assert set(m.VAT_RATES) == {"DE", "AT", "PL", "CH"}'},
             drop=["s11_constant"],
             ref=_decimal_ref(200, renamed=True, validate=True, receipt=True, public_rates=True, swiss=True)),
    ])

# ---------------------------------------------------------------- the task (hard): expression evaluator

def _calc_ref(evaluate: bool = False, parens: bool = False, unary: bool = False, variables: bool = False,
              run_fn: bool = False, funcs: bool = False) -> str:
    """Reference `calc` module built up stage by stage (so 'corrected' mode reveals only what was asked so far):
    a recursive-descent evaluator that grows parentheses, unary minus, variables, a program runner and functions."""
    names = variables or funcs
    out = []
    name_branch = '''
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (expr[j].isalnum() or expr[j] == "_"):
                j += 1
            tokens.append(expr[i:j])
            i = j''' if names else ""
    ops = "+-*/(),"if funcs else "+-*/()"
    out.append(f'''\
def tokenize(expr: str) -> list:
    """Split an arithmetic expression into numbers, operators and parentheses."""
    tokens: list = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
        elif c.isdigit() or (c == "." and i + 1 < n and expr[i + 1].isdigit()):
            j = i
            while j < n and (expr[j].isdigit() or expr[j] == "."):
                j += 1
            part = expr[i:j]
            tokens.append(float(part) if "." in part else int(part))
            i = j{name_branch}
        elif c in "{ops}":
            tokens.append(c)
            i += 1
        else:
            raise ValueError(f"unexpected character {{c!r}} in {{expr!r}}")
    return tokens
''')
    if funcs:
        out.append('\nFUNCTIONS = {"min": min, "max": max, "abs": abs}\n')
    if not evaluate:
        return "\n".join(out)
    factor = '''
    def factor(self):
        tok = self.peek()
        if tok in ("+", "-"):
            self.eat()
            value = self.factor()
            return -value if tok == "-" else value
        return self.atom()''' if unary else '''
    def factor(self):
        return self.atom()'''
    atom_parens = '''
        if tok == "(":
            self.eat("(")
            value = self.expr()
            self.eat(")")
            return value''' if parens else ""
    atom_call = '''
            if self.peek() == "(":
                self.eat("(")
                args = [self.expr()]
                while self.peek() == ",":
                    self.eat(",")
                    args.append(self.expr())
                self.eat(")")
                if name not in FUNCTIONS:
                    raise ValueError(f"unknown function {name!r}")
                return FUNCTIONS[name](*args)''' if funcs else ""
    atom_name = f'''
        if isinstance(tok, str) and (tok[:1].isalpha() or tok[:1] == "_"):
            name = self.eat(){atom_call}
            if self.env is None or name not in self.env:
                raise ValueError(f"unknown variable {{name!r}}")
            return self.env[name]''' if names else ""
    out.append(f'''

class _Parser:
    def __init__(self, tokens, env=None):
        self.tokens = tokens
        self.pos = 0
        self.env = env

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def eat(self, expected=None):
        tok = self.peek()
        if tok is None:
            raise ValueError("unexpected end of expression")
        if expected is not None and tok != expected:
            raise ValueError(f"expected {{expected!r}}, got {{tok!r}}")
        self.pos += 1
        return tok

    def parse(self):
        value = self.expr()
        if self.pos != len(self.tokens):
            raise ValueError(f"unexpected token {{self.peek()!r}}")
        return value

    def expr(self):
        value = self.term()
        while self.peek() in ("+", "-"):
            op = self.eat()
            right = self.term()
            value = value + right if op == "+" else value - right
        return value

    def term(self):
        value = self.factor()
        while self.peek() in ("*", "/"):
            op = self.eat()
            right = self.factor()
            value = value * right if op == "*" else value / right
        return value
{factor}

    def atom(self):
        tok = self.peek()
        if isinstance(tok, (int, float)) and not isinstance(tok, bool):
            self.eat()
            return tok{atom_parens}{atom_name}
        raise ValueError(f"unexpected token {{tok!r}}")
''')
    sig = "expr: str, env: dict | None = None" if variables else "expr: str"
    call = "tokenize(expr), env" if variables else "tokenize(expr)"
    out.append(f'''

def evaluate({sig}) -> float | int:
    """Evaluate an arithmetic expression and return its value."""
    return _Parser({call}).parse()
''')
    if run_fn:
        out.append('''

def run(program: str) -> dict:
    """Run a program of 'name = expression' lines and return the resulting variables."""
    env: dict = {}
    for line in program.splitlines():
        line = line.strip()
        if not line:
            continue
        name, sep, expr = line.partition("=")
        if not sep:
            raise ValueError(f"not an assignment: {line!r}")
        env[name.strip()] = evaluate(expr, env)
    return env
''')
    return "".join(out)


CALC = Task(
    "calc", "Expression evaluator",
    "You are developing a Python module step by step together with the user. After every request reply with the "
    "complete current module in a single ```python code block - always the whole file, not only the changes - "
    "and nothing else apart from at most one short sentence. These rules apply for the whole session:\n"
    "1. Use only the Python standard library.\n"
    "2. Every public function (name not starting with _) has type annotations for all parameters and the return "
    "value.\n"
    "3. Never remove or rename a public function unless the user explicitly asks for it.\n"
    "4. Do not use print().",
    [
        Step("Write a function `tokenize(expr)` that splits an arithmetic expression string into a flat list of "
             "tokens: numbers become int or float, and the operators +, -, *, / and the parentheses ( ) become "
             "one-character strings. Whitespace is ignored. An unexpected character raises ValueError.",
             {"c1_add": 'assert m.tokenize("1 + 2") == [1, "+", 2]',
              "c1_mul": 'assert m.tokenize("3*4-5") == [3, "*", 4, "-", 5]',
              "c1_paren": 'assert m.tokenize("(1 + 2)") == ["(", 1, "+", 2, ")"]',
              "c1_float": 'assert m.tokenize("2.5 / 4") == [2.5, "/", 4]',
              "c1_empty": 'assert m.tokenize("   ") == []'},
             api=["tokenize"], ref=_calc_ref()),
        Step("Add `evaluate(expr)` that returns the value of an expression using +, -, *, / with the usual "
             "precedence (* and / bind tighter than + and -, evaluated left to right). No parentheses yet.",
             {"c2_prec": 'assert m.evaluate("1+2*3") == 7',
              "c2_left": 'assert m.evaluate("10-2-3") == 5',
              "c2_mix": 'assert m.evaluate("2*3+4*5") == 26',
              "c2_div": 'assert near(m.evaluate("7/2"), 3.5)',
              "c2_single": 'assert m.evaluate("100") == 100'},
             api=["evaluate"], ref=_calc_ref(evaluate=True)),
        Step("Support parentheses in `evaluate` for grouping, e.g. (1+2)*3.",
             {"c3_paren": 'assert m.evaluate("(1+2)*3") == 9',
              "c3_right": 'assert m.evaluate("2*(3+4)") == 14',
              "c3_nested": 'assert m.evaluate("((5))") == 5',
              "c3_two": 'assert m.evaluate("(1+2)*(3+4)") == 21'},
             ref=_calc_ref(evaluate=True, parens=True)),
        Step("Support a unary minus (and unary plus), e.g. -5, 3 - -2, -(2+3), 2 * -3.",
             {"c4_neg": 'assert m.evaluate("-5") == -5',
              "c4_minus_minus": 'assert m.evaluate("3 - -2") == 5',
              "c4_group": 'assert m.evaluate("-(2+3)") == -5',
              "c4_times": 'assert m.evaluate("2 * -3") == -6',
              "c4_double": 'assert m.evaluate("--4") == 4'},
             ref=_calc_ref(evaluate=True, parens=True, unary=True)),
        Step("A quick question in between, no code change needed: is your evaluator a recursive-descent parser, "
             "and what is its time complexity in the length of the expression? Answer in one sentence - and as "
             "always, reply with the complete module.",
             ref=_calc_ref(evaluate=True, parens=True, unary=True)),
        Step("`evaluate` gets a second parameter `env` (default None): a dict of variable name -> number. A name "
             "in the expression is looked up in it; an unknown variable (or no env given) raises ValueError. "
             "`evaluate` without env must keep working.",
             {"c6_var": 'assert m.evaluate("x + 1", {"x": 4}) == 5',
              "c6_two": 'assert m.evaluate("2*a + b", {"a": 3, "b": 1}) == 7',
              "c6_unknown": 'assert raises(ValueError, m.evaluate, "z + 1", {})',
              "c6_noenv": 'assert m.evaluate("3+4") == 7',
              "c6_neg": 'assert m.evaluate("-x", {"x": 5}) == -5'},
             ref=_calc_ref(evaluate=True, parens=True, unary=True, variables=True)),
        Step("Add `run(program)`: execute a program of newline-separated `name = expression` lines, where each "
             "expression may use variables assigned on earlier lines, and return the resulting variables as a "
             "dict. Blank lines are ignored.",
             {"c7_seq": 'assert m.run("x = 1+2\\ny = x*3") == {"x": 3, "y": 9}',
              "c7_reassign": 'assert m.run("a = 2\\nb = a\\na = 5") == {"a": 5, "b": 2}',
              "c7_float": 'assert near(m.run("v = 7/2")["v"], 3.5)',
              "c7_empty": 'assert m.run("") == {}',
              "c7_blank": 'assert m.run("x = 1\\n\\ny = 2\\n") == {"x": 1, "y": 2}'},
             api=["run"], ref=_calc_ref(evaluate=True, parens=True, unary=True, variables=True, run_fn=True)),
        Step("Support function calls in expressions: `min` and `max` (two or more arguments) and `abs` (one "
             "argument), e.g. max(1, 2+3). Keep the supported functions in a module-level dict `FUNCTIONS` "
             "(name -> callable). An unknown function raises ValueError.",
             {"c8_max": 'assert m.evaluate("max(1, 2+3)") == 5',
              "c8_min": 'assert m.evaluate("min(3, 2, 5)") == 2',
              "c8_abs": 'assert m.evaluate("abs(-7)") == 7',
              "c8_combo": 'assert m.evaluate("max(1,2) + min(10, 3)") == 5',
              "c8_unknown": 'assert raises(ValueError, m.evaluate, "foo(1)")',
              "c8_tok": 'assert m.tokenize("max(1, 2)") == ["max", "(", 1, ",", 2, ")"]'},
             ref=_calc_ref(evaluate=True, parens=True, unary=True, variables=True, run_fn=True, funcs=True)),
    ])

TASKS = {t.id: t for t in (ORDER_PRICING, CALC)}
MODES = {"own": "own code", "corrected": "corrected history"}


def evaluate_step(task: Task, n: int, code: str) -> dict:
    """Result of step n (1-based) for a module: tests of this step / of earlier steps, broken rules."""
    from .sandbox import run_tests
    tests, new = task.suite(n)
    res = run_tests(code, tests) if tests else {"tests": {}}
    results = res.get("tests") or {}
    error = res.get("import_error") or res.get("sandbox_error") or ""
    failed = {k: v for k, v in results.items() if v != "ok"} if results else {k: error or "not run" for k in tests}
    ok = lambda names: sum(1 for k in names if results.get(k) == "ok")  # noqa: E731
    earlier = [k for k in tests if k not in new]
    return {"step": n, "new_ok": ok(new), "new_n": len(new), "earlier_ok": ok(earlier), "earlier_n": len(earlier),
            "rules": rule_violations(code, task.api(n)), "error": error, "failed": failed,
            "lines": code.count("\n")}


def self_check() -> list[str]:
    """Problems of the task definitions: every reference solution must pass its tests and the rules."""
    problems = []
    for task in TASKS.values():
        for n in range(1, len(task.steps) + 1):
            r = evaluate_step(task, n, task.steps[n - 1].ref)
            if r["new_ok"] < r["new_n"] or r["earlier_ok"] < r["earlier_n"] or r["rules"] or r["error"]:
                problems.append(f"{task.id} step {n}: {r['failed'] or r['rules'] or r['error']}")
    return problems
