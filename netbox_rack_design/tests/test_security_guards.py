"""
Standing security guards (1.2.2): checks that run with the normal suite so the
classes of bug the 1.2.2 audit fixed cannot quietly come back.

1. ``JsSinkScannerTest`` -- no DB. Scans the plugin's JavaScript for HTML sinks
   (``.innerHTML =``, ``.innerHTML +=``, ``.outerHTML =``, ``insertAdjacentHTML(``,
   ``document.write(``) and fails when the HTML handed to one is built from an
   expression that is not provably markup-safe.

   What "provably safe" means (the scanner is a small tokenizer + expression
   analyser, not a JS engine, and errs on the side of flagging):

   * string literals, numbers, template literals whose ``${}`` parts are safe;
   * calls to the escaping/number helpers: ``escapeHtml(...)``, ``esc(...)``,
     ``safeToken(...)``, ``Number``/``parseInt``/``parseFloat``/``Math.*``,
     ``encodeURIComponent``; ``.length`` and ``.toFixed(...)``; arithmetic and
     comparison results; the branches of ``a ? b : c`` and ``a || b``;
   * a variable whose EVERY assignment in the enclosing function scope (else the
     file) is itself safe -- so ``var html = ''; html += '<b>' + escapeHtml(x)``
     is proven, ``html += '<b>' + x`` is not; function parameters are unproven;
   * a function parameter whose every call in the file passes a safe argument;
   * a call to a function defined in the same file whose every ``return`` is
     safe, and ``list.map(callback).join(...)`` whose callback returns are all
     safe; ``x.innerHTML`` read back (markup the DOM already holds).

   Escape hatch, for markup the analyser cannot follow: put the comment
   ``// security: html-built-safely`` on any line of the statement or on the
   line directly above it, and say WHY in the same comment. Use it sparingly --
   prefer rewriting to ``createElement``/``textContent`` or routing the value
   through ``escapeHtml``. The scanner tests below feed it synthetic snippets, so
   a loosened rule shows up there first.

2. ``PermissionMatrixTest`` -- every route of the plugin REST API, as (a) a user
   whose design permissions are constrained to a DIFFERENT design and (b) a user
   with full permissions holding a read-only token. A new ``@action`` that is in
   neither the matrix nor the skip list fails ``test_every_action_is_classified``,
   which forces the author to put the endpoint through both checks.

3. ``GraphQLLineageTest`` -- the lineage fields as a constrained user never name
   a design the user cannot view.
"""

import json
import re
from collections import namedtuple
from pathlib import Path

from core.models import ObjectType
from dcim.models import Rack, Site
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from users.models import ObjectPermission
from utilities.testing import APITestCase, create_test_device

from ..api import urls as api_urls
from ..api.views import DesignViewSet
from ..choices import DesignStatusChoices
from ..models import Design, DesignPlacement, DesignPowerFeed
from .utils import create_dcim_environment, make_design

# =============================================================================
# 1. JavaScript HTML-sink scanner
# =============================================================================

JS_ROOT = Path(__file__).resolve().parent.parent / "static" / "netbox_rack_design" / "js"
# Vendored third-party bundle: not ours to police.
JS_SKIP = {"gridstack-all.js"}
ALLOW_MARKER = "security: html-built-safely"

# Calls whose RESULT is markup-safe whatever the arguments are.
SAFE_CALLS = {
    "escapeHtml", "esc", "safeToken", "Number", "parseInt", "parseFloat", "Boolean",
    "encodeURIComponent",
}
SAFE_CALL_PREFIXES = ("Math.",)
SAFE_METHOD_SUFFIXES = {"toFixed", "toLocaleString"}
ITERATION_METHODS = {"map", "forEach", "filter", "some", "every", "find", "findIndex", "flatMap"}
LITERAL_IDS = {"true", "false", "null", "undefined", "NaN", "Infinity"}
NOT_FUNCTION_HEADS = {"if", "for", "while", "switch", "catch", "with", "function", "return", "typeof"}
REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%<>~^")
REGEX_AFTER_IDS = {"return", "typeof", "case", "in", "of", "delete", "void", "throw"}
COMPARISON_OPS = {"<", ">", "<=", ">=", "==", "===", "!=", "!==", "instanceof", "in"}

Tok = namedtuple("Tok", "kind text line idx parts")  # parts: template ${} token lists
Finding = namedtuple("Finding", "path line sink snippet reason")

_PUNCT = re.compile(
    r">>>=|\.\.\.|===|!==|\*\*=|<<=|>>=|>>>|&&=|\|\|=|\?\?=|=>|==|!=|<=|>=|&&|\|\||\?\?|\?\.(?!\d)"
    r"|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|\*\*|<<|>>|[-+*/%=<>!&|^~?:;,.(){}\[\]@#]"
)
_NUM = re.compile(r"0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?|\.\d[\d_]*")
_ID = re.compile(r"[A-Za-z_$][\w$]*")


def lex(src, line=1, idx0=None):
    """Tokenise JavaScript. Returns ``(tokens, allow_lines)``; comments are
    dropped but an allowlist marker inside one records its line numbers."""
    toks, allow = [], set()
    i, n = 0, len(src)

    def prev_sig():
        return toks[-1] if toks else None

    def emit(kind, text, parts=None):
        toks.append(Tok(kind, text, line, idx0 if idx0 is not None else len(toks), parts))

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            if ALLOW_MARKER in src[i:j]:
                allow.add(line)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            chunk = src[i:j]
            if ALLOW_MARKER in chunk:
                allow.update(range(line, line + chunk.count("\n") + 1))
            line += chunk.count("\n")
            i = j
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                    if j < n and src[j] == "\n":
                        line += 1
                j += 1
            emit("str", src[i:j + 1])
            i = j + 1
        elif c == "`":
            j, depth_parts, start_line = i + 1, [], line
            while j < n and src[j] != "`":
                if src[j] == "\\":
                    j += 2
                    continue
                if src.startswith("${", j):
                    k, depth = j + 2, 1
                    while k < n and depth:
                        if src[k] in "'\"`":
                            q = src[k]
                            k += 1
                            while k < n and src[k] != q:
                                k += 2 if src[k] == "\\" else 1
                        elif src[k] == "{":
                            depth += 1
                        elif src[k] == "}":
                            depth -= 1
                        k += 1
                    sub, _ = lex(src[j + 2:k - 1], line, idx0=len(toks))
                    depth_parts.append(sub)
                    line += src[j:k].count("\n")
                    j = k
                    continue
                if src[j] == "\n":
                    line += 1
                j += 1
            toks.append(Tok("tpl", src[i:j + 1], start_line, len(toks), depth_parts))
            i = j + 1
        elif c == "/" and _regex_allowed(prev_sig()):
            j, in_class = i + 1, False
            while j < n and (src[j] != "/" or in_class):
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            emit("regex", src[i:j])
            i = j
        elif c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUM.match(src, i)
            emit("num", m.group())
            i = m.end()
        elif c.isalpha() or c in "_$":
            m = _ID.match(src, i)
            emit("id", m.group())
            i = m.end()
        else:
            m = _PUNCT.match(src, i)
            if not m:
                i += 1
                continue
            emit("punct", m.group())
            i = m.end()
    return toks, allow


def _regex_allowed(prev):
    if prev is None:
        return True
    if prev.kind == "punct":
        return prev.text in REGEX_PRECEDERS or prev.text == "=>"
    if prev.kind == "id":
        return prev.text in REGEX_AFTER_IDS
    return False


Func = namedtuple("Func", "start end params plist name head")  # token index range of the body


class Scanner:
    """Finds HTML sinks in one JS source and proves (or fails to prove) the markup safe."""

    def __init__(self, src, path="<snippet>"):
        self.path = path
        self.toks, self.allow = lex(src)
        self.match = self._match_brackets()
        self.funcs = self._find_functions()
        self.assigns = self._index_assignments()
        self._busy = set()
        self._fn_cache = {}

    # -- structure ------------------------------------------------------------
    def _match_brackets(self):
        pairs, stack, match = {"(": ")", "[": "]", "{": "}"}, [], {}
        for i, t in enumerate(self.toks):
            if t.kind != "punct":
                continue
            if t.text in pairs:
                stack.append(i)
            elif t.text in pairs.values() and stack:
                o = stack.pop()
                match[o], match[i] = i, o
        return match

    def _find_functions(self):
        toks, funcs = self.toks, []

        def plist_of(open_idx):
            """Ordered parameter names of the ``(...)`` group opening at ``open_idx``."""
            close = self.match.get(open_idx, open_idx)
            out, depth, expect = [], 0, True
            for t in toks[open_idx + 1:close]:
                if t.kind == "punct" and t.text in "([{":
                    depth += 1
                elif t.kind == "punct" and t.text in ")]}":
                    depth -= 1
                elif depth == 0 and t.kind == "punct" and t.text == ",":
                    expect = True
                elif depth == 0 and expect and t.kind == "id":
                    out.append(t.text)
                    expect = False
                else:
                    expect = False
            return out

        def bound_name(head):
            """Name a function expression starting at token ``head`` is bound to
            (``name = function``, ``name: function``, ``name = (a) =>``)."""
            if head >= 2 and toks[head - 1].text in {"=", ":"} and toks[head - 2].kind == "id":
                return toks[head - 2].text
            return None

        for i, t in enumerate(toks):
            if t.kind == "id" and t.text == "function":
                name = toks[i + 1].text if i + 1 < len(toks) and toks[i + 1].kind == "id" else bound_name(i)
                j = i + 1
                while j < len(toks) and not (toks[j].kind == "punct" and toks[j].text == "("):
                    j += 1
                if j < len(toks) and j in self.match:
                    k = self.match[j] + 1
                    if k < len(toks) and toks[k].text == "{" and k in self.match:
                        pl = plist_of(j)
                        funcs.append(Func(k, self.match[k], set(pl), pl, name, i))
            elif t.kind == "punct" and t.text == "=>":
                p = toks[i - 1]
                if p.kind == "id":
                    pl, head = [p.text], i - 1
                elif p.text == ")" and (i - 1) in self.match:
                    head = self.match[i - 1]
                    pl = plist_of(head)
                else:
                    pl, head = [], i
                name = bound_name(head)
                k = i + 1
                if k < len(toks) and toks[k].text == "{" and k in self.match:
                    funcs.append(Func(k, self.match[k], set(pl), pl, name, head))
                else:
                    funcs.append(Func(k, self._expr_end(k) - 1, set(pl), pl, name, head))
            elif t.kind == "punct" and t.text == "{" and i >= 1 and toks[i - 1].text == ")":
                o = self.match.get(i - 1)
                if o is not None and o >= 1 and toks[o - 1].kind == "id" \
                        and toks[o - 1].text not in NOT_FUNCTION_HEADS \
                        and not (o >= 2 and toks[o - 2].text in {"function", "."}) and i in self.match:
                    pl = plist_of(o)
                    funcs.append(Func(i, self.match[i], set(pl), pl, toks[o - 1].text, o - 1))  # method shorthand
        return funcs

    def _expr_end(self, i):
        """Index just past the expression starting at ``i`` (stops at a depth-0
        ``;`` / ``,`` or at the bracket that closes the enclosing group)."""
        toks, depth = self.toks, 0
        while i < len(toks):
            t = toks[i]
            if t.kind == "punct":
                if t.text in "([{":
                    depth += 1
                elif t.text in ")]}":
                    depth -= 1
                    if depth < 0:
                        return i
                elif depth == 0 and t.text in ";,":
                    return i
            i += 1
        return i

    def _index_assignments(self):
        idx = {}
        toks = self.toks
        for i, t in enumerate(toks[:-1]):
            if t.kind == "id" and toks[i + 1].kind == "punct" and toks[i + 1].text in {"=", "+=", "||=", "??="}:
                if i and toks[i - 1].text == ".":
                    continue
                idx.setdefault(t.text, []).append(i)
        return idx

    def _scopes_at(self, pos):
        inside = [f for f in self.funcs if f.start <= pos <= f.end]
        return sorted(inside, key=lambda f: f.end - f.start)

    # -- proving ----------------------------------------------------------------
    def expr_safe(self, toks):
        """``(True, "")`` or ``(False, reason)`` for an expression's token list."""
        toks = list(toks)
        if not toks:
            return True, ""
        # outer parens
        while toks[0].text == "(" and toks[0].kind == "punct" and self._closes_all(toks):
            toks = toks[1:-1]
            if not toks:
                return True, ""
        # a ? b : c  (lowest precedence)
        q = self._find_top(toks, {"?"})
        if q is not None:
            depth = 0
            for j in range(q + 1, len(toks)):
                t = toks[j]
                if t.kind == "punct":
                    if t.text == "?":
                        depth += 1
                    elif t.text == ":":
                        if depth == 0:
                            return self._all([toks[q + 1:j], toks[j + 1:]])
                        depth -= 1
            return False, "unparsed ternary: " + self._src(toks)
        # a || b / a ?? b  -> every operand may be the result
        parts = self._split_top(toks, {"||", "??"})
        if len(parts) > 1:
            return self._all(parts)
        # a && b -> only the last operand can be a non-falsy result
        parts = self._split_top(toks, {"&&"})
        if len(parts) > 1:
            return self.expr_safe(parts[-1])
        # comparisons produce booleans
        if self._find_top(toks, COMPARISON_OPS) is not None:
            return True, ""
        # last binary + / -
        cut = self._last_binary_pm(toks)
        if cut is not None:
            if toks[cut].text == "-":
                return True, ""  # arithmetic result
            return self._all([toks[:cut], toks[cut + 1:]])
        if self._find_top(toks, {"*", "/", "%", "**", "<<", ">>", ">>>", "&", "|", "^"}, binary=True) is not None:
            return True, ""
        return self._atom_safe(toks)

    def _all(self, parts):
        for p in parts:
            ok, why = self.expr_safe(p)
            if not ok:
                return False, why
        return True, ""

    def _atom_safe(self, toks):
        first = toks[0]
        if len(toks) == 1:
            if first.kind in {"str", "num"}:
                return True, ""
            if first.kind == "tpl":
                return self._all(first.parts) if first.parts else (True, "")
            if first.kind == "id":
                if first.text in LITERAL_IDS:
                    return True, ""
                return self._var_safe(first)
            return False, self._src(toks)
        if first.kind == "punct" and first.text in {"!", "-", "+", "~"}:
            return True, ""  # boolean / numeric result
        if first.kind == "id" and first.text == "typeof":
            return True, ""
        last2 = [t.text for t in toks[-2:]]
        if last2[0] == "." and last2[1] == "length":
            return True, ""
        if last2[0] == "." and last2[1] == "innerHTML":
            return True, ""  # markup the DOM already holds, re-set as it was
        if toks[-1].text == ")" and toks[-1].kind == "punct":
            return self._call_safe(toks)
        if toks[-1].text == "]" and toks[0].kind == "id" and toks[1].text == "[" and self.match.get(toks[1].idx) is not None \
                and toks[1].kind == "punct" and len(toks) > 3:
            return self._table_safe(toks[0])
        return False, self._src(toks)

    def _table_safe(self, base):
        """``TABLE[key]`` where ``TABLE`` is only ever assigned an object/array
        literal whose values are all safe (a constant lookup table)."""
        sites = self.assigns.get(base.text, [])
        if not sites:
            return False, f"`{base.text}` is not a constant table this scanner can see"
        for i in sites:
            rhs = self.toks[i + 2:self._expr_end(i + 2)]
            if not rhs or rhs[0].text not in {"{", "["} or not self._closes_all(rhs):
                return False, f"`{base.text}` is not a plain literal table"
            for entry in self._split_top(rhs[1:-1], {","}):
                value = entry
                for j, t in self._top_level(entry):
                    if t.kind == "punct" and t.text == ":":
                        value = entry[j + 1:]
                        break
                ok, why = self.expr_safe(value)
                if not ok:
                    return False, f"`{base.text}` holds an unsafe value: {why}"
        return True, ""

    def _call_safe(self, toks):
        # find the "(" matching the final ")"
        depth, o = 0, None
        for j in range(len(toks) - 1, -1, -1):
            t = toks[j]
            if t.kind == "punct" and t.text == ")":
                depth += 1
            elif t.kind == "punct" and t.text == "(":
                depth -= 1
                if depth == 0:
                    o = j
                    break
        if o is None or o == 0:
            return False, self._src(toks)
        callee = toks[:o]
        name = "".join(t.text for t in callee)
        if name in SAFE_CALLS or name.startswith(SAFE_CALL_PREFIXES):
            return True, ""
        if len(callee) >= 2 and callee[-2].text == "." and callee[-1].text in SAFE_METHOD_SUFFIXES:
            return True, ""
        if len(callee) >= 2 and callee[-2].text == "." and callee[-1].text == "join":
            return self._map_join_safe(callee[:-2], toks)
        if len(callee) == 1 and callee[0].kind == "id":
            if self._function_returns_safe(callee[0].text):
                return True, ""
            return False, f"{callee[0].text}() is unknown or returns something not provably safe"
        return False, self._src(toks)

    def _map_join_safe(self, target, whole):
        # target: an array built by .push(...) calls, or ends with  .map( callback )
        if target and target[0].kind == "id" and (
                len(target) == 1 or (target[1].text == "[" and self._closes_all(target[1:]))):
            return self._pushed_parts_safe(target[0].text, whole)
        if not target or target[-1].text != ")":
            return False, self._src(whole)
        depth, o = 0, None
        for j in range(len(target) - 1, -1, -1):
            t = target[j]
            if t.kind == "punct" and t.text == ")":
                depth += 1
            elif t.kind == "punct" and t.text == "(":
                depth -= 1
                if depth == 0:
                    o = j
                    break
        if o is None or o < 2 or target[o - 1].text != "map" or target[o - 2].text != ".":
            return False, self._src(whole)
        cb = target[o + 1:-1]
        return self._callback_safe(cb, whole)

    def _pushed_parts_safe(self, name, whole):
        """``parts.join(...)`` / ``byKey[k].join(...)``: every ``name.push(x)`` /
        ``name[..].push(x)`` / ``.unshift`` in the file adds a safe part."""
        toks, seen = self.toks, 0
        for i, t in enumerate(toks):
            if t.kind != "id" or t.text != name or (i and toks[i - 1].text == "."):
                continue
            j = i + 1
            if j < len(toks) and toks[j].text == "[" and j in self.match:
                j = self.match[j] + 1
            if j + 2 < len(toks) and toks[j].text == "." and toks[j + 1].text in {"push", "unshift"} \
                    and toks[j + 2].text == "(" and (j + 2) in self.match:
                seen += 1
                for arg in self._split_top(toks[j + 3:self.match[j + 2]], {","}):
                    ok, why = self.expr_safe(arg)
                    if not ok:
                        return False, f"`{name}` gets an unsafe part (line {t.line}): {why}"
        for i in self.assigns.get(name, []):
            rhs = toks[i + 2:self._expr_end(i + 2)]
            if [t.text for t in rhs] not in (["[", "]"], ["{", "}"]):
                ok, why = self.expr_safe(rhs)
                if not ok:
                    return False, f"`{name}` is assigned an unsafe value (line {toks[i].line}): {why}"
        if not seen:
            return False, f"`{name}` is joined but nothing visible pushes safe parts into it"
        return True, ""

    def _callback_safe(self, cb, whole):
        if not cb:
            return False, self._src(whole)
        for j, t in enumerate(cb):
            if t.kind == "punct" and t.text == "=>" and not self._depth_at(cb, j):
                body = cb[j + 1:]
                if body and body[0].text == "{":
                    return self._returns_safe_in(body[0].idx)
                return self.expr_safe(body)
        if cb[0].kind == "id" and cb[0].text == "function":
            for t in cb:
                if t.kind == "punct" and t.text == "{":
                    return self._returns_safe_in(t.idx)
            return False, self._src(whole)
        if len(cb) == 1 and cb[0].kind == "id":
            ok = self._function_returns_safe(cb[0].text)
            return (True, "") if ok else (False, f"{cb[0].text} has a return that is not provably safe")
        return False, self._src(whole)

    def _returns_safe_in(self, body_start):
        func = next((f for f in self.funcs if f.start == body_start), None)
        if func is None:
            return False, "callback body not found"
        found = False
        for i in range(func.start, func.end + 1):
            t = self.toks[i]
            if t.kind == "id" and t.text == "return":
                inner = self._scopes_at(i)
                if not inner or inner[0].start != func.start:
                    continue  # a nested function's return
                found = True
                ok, why = self.expr_safe(self.toks[i + 1:self._expr_end(i + 1)])
                if not ok:
                    return False, why
        return (True, "") if found else (False, "callback never returns markup")

    def _function_returns_safe(self, name, ctx=None):
        """Every function named ``name`` in this file returns only provably safe markup."""
        if name in self._fn_cache:
            return self._fn_cache[name]
        key = ("fn", name)
        if key in self._busy:
            return True  # recursion: judged by its other returns
        self._busy.add(key)
        try:
            defs = [f for f in self.funcs if f.name == name]
            ok = bool(defs)
            for f in defs:
                if self.toks[f.start].text == "{" and self.match.get(f.start) == f.end:
                    r, _ = self._returns_safe_in(f.start)
                else:
                    r, _ = self.expr_safe(self.toks[f.start:f.end + 1])
                ok = ok and r
            self._fn_cache[name] = ok
            return ok
        finally:
            self._busy.discard(key)

    def _is_iteration_callback(self, func):
        """Is ``func`` the callback argument of ``.map(`` / ``.forEach(`` ... ?"""
        h = func.head
        toks = self.toks
        return (h >= 3 and toks[h - 1].text == "(" and toks[h - 2].text in ITERATION_METHODS
                and toks[h - 3].text == ".")

    def _call_sites(self, name):
        """Argument token lists of every call ``name(...)`` in the file, as
        ``[[arg0_toks, arg1_toks, ...], ...]``; None when a call uses spread."""
        out = []
        for i, t in enumerate(self.toks[:-1]):
            if t.kind == "id" and t.text == name and self.toks[i + 1].text == "(" and i + 1 in self.match \
                    and not (i and self.toks[i - 1].text == "function"):
                args = self._split_top(self.toks[i + 2:self.match[i + 1]], {","})
                if any(a and a[0].text == "..." for a in args):
                    return None
                out.append([a for a in args if a])
        return out

    def _var_safe(self, tok):
        name, pos = tok.text, tok.idx
        for func in self._scopes_at(pos):
            if name in func.params:
                ok, why = self._param_safe(func, name)
                if not ok:
                    return False, why
            sites = [i for i in self.assigns.get(name, []) if func.start <= i <= func.end]
            if sites:
                return self._assignments_safe(name, sites, func.start)
            if name in func.params:
                return True, ""
        sites = self.assigns.get(name, [])
        if sites:
            return self._assignments_safe(name, sites, -1)
        return False, f"`{name}` has no assignment this scanner can see"

    def _param_safe(self, func, name):
        """A parameter is safe when every call of the (named) function in this
        file passes a safe argument in its position."""
        if name not in func.plist:
            return False, f"`{name}` is a function parameter (unproven)"
        if not func.name and not (func.plist.index(name) == 1 and self._is_iteration_callback(func)):
            return False, f"`{name}` is a parameter of an anonymous function (unproven)"
        pos = func.plist.index(name)
        if pos == 1 and self._is_iteration_callback(func):
            return True, ""  # the element index: a number
        key = ("param", func.start, pos)
        if key in self._busy:
            return True, ""
        self._busy.add(key)
        try:
            calls = self._call_sites(func.name)
            if not calls:
                return False, f"`{name}` is a parameter of {func.name}(), which has no visible caller"
            for args in calls:
                if pos < len(args):
                    ok, why = self.expr_safe(args[pos])
                    if not ok:
                        return False, f"{func.name}() is called with an unsafe `{name}`: {why}"
            return True, ""
        finally:
            self._busy.discard(key)

    def _assignments_safe(self, name, sites, scope):
        key = ("var", name, scope)
        if key in self._busy:
            return True, ""  # self-reference such as `html = html + x`
        self._busy.add(key)
        try:
            for i in sites:
                rhs = self.toks[i + 2:self._expr_end(i + 2)]
                ok, why = self.expr_safe(rhs)
                if not ok:
                    return False, f"`{name}` is assigned an unsafe value (line {self.toks[i].line}): {why}"
            return True, ""
        finally:
            self._busy.discard(key)

    # -- token helpers ------------------------------------------------------------
    @staticmethod
    def _depth_at(toks, j):
        depth = 0
        for t in toks[:j]:
            if t.kind == "punct":
                if t.text in "([{":
                    depth += 1
                elif t.text in ")]}":
                    depth -= 1
        return depth

    @staticmethod
    def _closes_all(toks):
        depth = 0
        for j, t in enumerate(toks):
            if t.kind == "punct":
                if t.text in "([{":
                    depth += 1
                elif t.text in ")]}":
                    depth -= 1
                    if depth == 0:
                        return j == len(toks) - 1
        return False

    @staticmethod
    def _is_binary_position(toks, j):
        if j == 0:
            return False
        p = toks[j - 1]
        return p.kind in {"str", "num", "tpl", "regex"} or (p.kind == "id" and p.text not in REGEX_AFTER_IDS) \
            or (p.kind == "punct" and p.text in {")", "]", "}"})

    def _top_level(self, toks):
        """Yield ``(j, tok)`` for tokens at bracket depth 0."""
        depth = 0
        for j, t in enumerate(toks):
            if t.kind == "punct" and t.text in ")]}":
                depth -= 1
            if depth == 0:
                yield j, t
            if t.kind == "punct" and t.text in "([{":
                depth += 1

    def _find_top(self, toks, ops, binary=None):
        """Index of the first depth-0 token in ``ops`` (``binary=True``: only in
        binary-operator position), else None."""
        for j, t in self._top_level(toks):
            if t.kind not in {"punct", "id"} or t.text not in ops:
                continue
            if t.text == "?" and j + 1 < len(toks) and toks[j + 1].text == ".":
                continue
            if binary and not self._is_binary_position(toks, j):
                continue
            return j
        return None

    def _split_top(self, toks, ops):
        parts, cur = [], []
        tops = {j for j, t in self._top_level(toks) if t.kind == "punct" and t.text in ops}
        for j, t in enumerate(toks):
            if j in tops:
                parts.append(cur)
                cur = []
            else:
                cur.append(t)
        parts.append(cur)
        return parts

    def _last_binary_pm(self, toks):
        cut = None
        for j, t in self._top_level(toks):
            if t.kind == "punct" and t.text in {"+", "-"} and self._is_binary_position(toks, j):
                cut = j
        return cut

    @staticmethod
    def _src(toks, limit=90):
        text = " ".join(t.text for t in toks)
        return text if len(text) <= limit else text[:limit] + "..."

    # -- sinks -------------------------------------------------------------------
    def findings(self):
        out, toks = [], self.toks
        for i, t in enumerate(toks):
            sink = rhs = None
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if t.kind == "id" and t.text in {"innerHTML", "outerHTML"} and i >= 1 and toks[i - 1].text == "." \
                    and nxt and nxt.text in {"=", "+="}:
                sink, start = f".{t.text} {nxt.text}", i + 2
                rhs = toks[start:self._expr_end(start)]
            elif t.kind == "str" and t.text.strip("'\"") in {"innerHTML", "outerHTML"} and i >= 1 \
                    and toks[i - 1].text == "[" and i + 2 < len(toks) and toks[i + 1].text == "]" \
                    and toks[i + 2].text in {"=", "+="}:
                sink, start = f"[{t.text}] {toks[i + 2].text}", i + 3
                rhs = toks[start:self._expr_end(start)]
            elif t.kind == "id" and t.text == "insertAdjacentHTML" and nxt and nxt.text == "(" and i + 1 in self.match:
                args = self._split_top(toks[i + 2:self.match[i + 1]], {","})
                sink = "insertAdjacentHTML("
                rhs = args[1] if len(args) > 1 else []
                start = i
            elif t.kind == "id" and t.text in {"write", "writeln"} and i >= 2 and toks[i - 1].text == "." \
                    and toks[i - 2].text == "document" and nxt and nxt.text == "(" and i + 1 in self.match:
                sink = f"document.{t.text}("
                rhs = toks[i + 2:self.match[i + 1]]
                start = i
            if sink is None:
                continue
            first_line = t.line
            last_line = (rhs[-1].line if rhs else first_line)
            if any(first_line - 1 <= ln <= last_line for ln in self.allow):
                continue
            ok, why = self.expr_safe(rhs)
            if not ok:
                out.append(Finding(self.path, first_line, sink, self._src(rhs, 120), why))
        return out


def scan_source(src, path="<snippet>"):
    return Scanner(src, path).findings()


def scan_js_tree():
    findings = []
    for path in sorted(JS_ROOT.rglob("*.js")):
        if path.name in JS_SKIP:
            continue
        findings += scan_source(path.read_text(encoding="utf-8"), str(path.relative_to(JS_ROOT)))
    return findings


class JsSinkScannerTest(SimpleTestCase):

    def test_plugin_js_has_no_unescaped_html_sinks(self):
        findings = scan_js_tree()
        self.assertEqual(
            findings, [],
            "HTML sink fed by an expression that is not provably escaped. Route the value "
            "through escapeHtml(), build the node with createElement/textContent, or -- only "
            f"when the markup is demonstrably safe -- add `// {ALLOW_MARKER}: <why>`:\n"
            + "\n".join(f"  {f.path}:{f.line} {f.sink} {f.snippet}\n      -> {f.reason}" for f in findings),
        )

    def test_scanner_actually_sees_the_sinks(self):
        # A scanner that tokenises nothing would pass the check above for free.
        total = 0
        for path in JS_ROOT.rglob("*.js"):
            if path.name in JS_SKIP:
                continue
            total += len(re.findall(r"\.(?:innerHTML|outerHTML)\s*\+?=|insertAdjacentHTML\(",
                                    path.read_text(encoding="utf-8")))
        self.assertGreater(total, 30)
        for path in JS_ROOT.rglob("*.js"):
            if path.name in JS_SKIP:
                continue
            scanner = Scanner(path.read_text(encoding="utf-8"), path.name)
            self.assertTrue(scanner.toks, path.name)

    def test_escape_helpers_escape_quotes_too(self):
        # escapeHtml() output lands in attribute values as well as text nodes.
        seen = 0
        for path in JS_ROOT.rglob("*.js"):
            if path.name in JS_SKIP:
                continue
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"function (?:escapeHtml|esc)\s*\([^)]*\)\s*\{", text):
                body = text[m.end():m.end() + 500]
                body = body[:body.find("\n}") if "\n}" in body else len(body)]
                seen += 1
                for needle in ("&amp;", "&lt;", "&gt;", "&quot;"):
                    self.assertIn(needle, body, f"{path.name}: {m.group().strip()} must emit {needle}")
                self.assertTrue("&#39;" in body or "&#x27;" in body or "&apos;" in body,
                                f"{path.name}: {m.group().strip()} must escape single quotes")
        self.assertGreaterEqual(seen, 3)

    # -- the scanner catches regressions ---------------------------------------
    def assertFlagged(self, src):
        self.assertTrue(scan_source(src), f"expected a finding for:\n{src}")

    def assertClean(self, src):
        self.assertEqual(scan_source(src), [], f"unexpected finding for:\n{src}")

    def test_flags_concatenated_name(self):
        self.assertFlagged("el.innerHTML = '<b>' + name + '</b>';")

    def test_flags_member_expression(self):
        self.assertFlagged("el.innerHTML = '<b>' + design.title + '</b>';")

    def test_flags_template_literal(self):
        self.assertFlagged("el.innerHTML = `<b>${name}</b>`;")

    def test_flags_append_assignment(self):
        self.assertFlagged("el.innerHTML += '<i>' + label + '</i>';")

    def test_flags_outer_html_and_insert_adjacent(self):
        self.assertFlagged("el.outerHTML = '<p>' + text + '</p>';")
        self.assertFlagged("el.insertAdjacentHTML('beforeend', '<li>' + item.name + '</li>');")
        self.assertFlagged("document.write('<p>' + text + '</p>');")

    def test_flags_unescaped_branch_of_a_ternary(self):
        self.assertFlagged("el.innerHTML = ok ? '<b>fine</b>' : name;")

    def test_flags_html_variable_built_with_a_raw_value(self):
        self.assertFlagged(
            "function f(rows) { var html = '';\n"
            "  rows.forEach(function (r) { html += '<li>' + r.name + '</li>'; });\n"
            "  el.innerHTML = html; }")

    def test_flags_function_parameter(self):
        self.assertFlagged("function show(el, html) { el.innerHTML = html; }")

    def test_flags_map_callback_returning_raw_value(self):
        self.assertFlagged(
            "el.innerHTML = rows.map(function (r) { return '<li>' + r.name + '</li>'; }).join('');")
        self.assertFlagged("el.innerHTML = rows.map(r => '<li>' + r.name + '</li>').join('');")

    def test_flags_html_builder_with_a_raw_return(self):
        self.assertFlagged(
            "function rowHtml(r) { return '<li>' + r.name + '</li>'; }\n"
            "el.innerHTML = rowHtml(r);")

    def test_flags_unescaped_value_in_a_template_nested_in_a_call(self):
        self.assertFlagged("el.innerHTML = `<ul>${rows.map(r => `<li>${r.name}</li>`).join('')}</ul>`;")

    def test_clean_literals_and_escaped_values(self):
        self.assertClean("el.innerHTML = '';")
        self.assertClean("el.innerHTML = '<b>' + escapeHtml(name) + '</b>';")
        self.assertClean("el.innerHTML = '<b>' + esc(a.b.c) + '</b>' + '<i>' + safeToken(x) + '</i>';")
        self.assertClean("el.innerHTML = `<b>${escapeHtml(name)}</b>`;")
        self.assertClean("el.innerHTML = '<b>' + rows.length + ' / ' + n.toFixed(1) + Math.round(x) + '</b>';")
        self.assertClean("el.innerHTML = '<b>' + (a > b ? 'x' : 'y') + '</b>';")
        self.assertClean("el.innerHTML = 'a' + (n > 1 && '<b>on</b>') + ('' || 'none');")
        self.assertClean("el.innerHTML = '<td>' + (w - 3) + '</td>';")

    def test_clean_html_variable_built_from_safe_parts(self):
        self.assertClean(
            "function f(rows) { var html = '<ul>';\n"
            "  rows.forEach(function (r) { html += '<li>' + escapeHtml(r.name) + '</li>'; });\n"
            "  html += '</ul>'; el.innerHTML = html; }")
        self.assertClean(
            "function g(r) { var on = r.on ? ' selected' : '';\n"
            "  el.innerHTML = '<option' + on + '>' + escapeHtml(r.name) + '</option>'; }")

    def test_clean_map_join_and_html_builder(self):
        self.assertClean(
            "el.innerHTML = rows.map(function (r) { var s = escapeHtml(r.name);\n"
            "  return '<li>' + s + '</li>'; }).join('');")
        self.assertClean("el.innerHTML = rows.map(r => '<li>' + escapeHtml(r.name) + '</li>').join('');")
        self.assertClean(
            "function rowHtml(r) { return '<li>' + escapeHtml(r.name) + '</li>'; }\n"
            "el.innerHTML = '<ul>' + rowHtml(r) + '</ul>';")

    def test_pushed_parts_must_be_safe(self):
        self.assertClean(
            "var parts = [];\nrows.forEach(function (r) { parts.push('<li>' + escapeHtml(r) + '</li>'); });\n"
            "el.innerHTML = parts.join('');")
        self.assertFlagged(
            "var parts = [];\nrows.forEach(function (r) { parts.push('<li>' + r.name + '</li>'); });\n"
            "el.innerHTML = parts.join('');")

    def test_a_parameter_is_judged_by_its_callers(self):
        self.assertClean(
            "function show(el, body) { el.innerHTML = '<p>' + body + '</p>'; }\n"
            "show(a, 'hello'); show(b, '<b>' + escapeHtml(x) + '</b>');")
        self.assertFlagged(
            "function show(el, body) { el.innerHTML = '<p>' + body + '</p>'; }\n"
            "show(a, 'hello'); show(b, user.bio);")

    def test_clean_restoring_previous_markup(self):
        self.assertClean("var label = btn ? btn.innerHTML : '';\nbtn.innerHTML = label;")

    def test_allowlist_comment_suppresses_a_finding(self):
        self.assertClean(
            "el.innerHTML = '<b>' + name + '</b>'; // security: html-built-safely (name is a constant)")
        self.assertClean(
            "// security: html-built-safely: icon is one of three literals\n"
            "el.innerHTML = '<b>' + icon + '</b>';")
        self.assertClean(
            "el.innerHTML = '<b>'\n  + name // security: html-built-safely\n  + '</b>';")

    def test_a_marker_two_lines_away_does_not_suppress(self):
        self.assertFlagged(
            "// security: html-built-safely\n\nel.innerHTML = '<b>' + name + '</b>';")

    def test_comments_and_regex_do_not_confuse_the_lexer(self):
        self.assertClean(
            "// el.innerHTML = '<b>' + name;\n"
            "var re = /<[^>]+>/g; var s = a / b; /* el.innerHTML += x */\n"
            "el.innerHTML = '<b>ok</b>';")
        self.assertFlagged("var re = /'/g;\nel.innerHTML = '<b>' + name;")


# =============================================================================
# 2. REST permission matrix
# =============================================================================

NS = "plugins-api:netbox_rack_design-api"
SECRET_TITLE = "ZZ-SECRET-TARGET-DESIGN"
SECRET_PLACEMENT = "ZZ-SECRET-PLACEMENT"
SECRET_FEED = "ZZ-SECRET-FEED"
SAFE_METHODS = {"get", "head", "options"}
UNSAFE_METHODS = {"post", "put", "patch", "delete"}


def A(methods, body=None, query="", readonly_token_ok=False):
    """One DesignViewSet detail action in the matrix. ``readonly_token_ok``: the
    POST writes nothing durable (a preview/simulation, rolled back), so a
    read-only token may call it by design (ViewDesignPermissions)."""
    return {"methods": set(methods), "body": body or {}, "query": query, "readonly_token_ok": readonly_token_ok}


# Every ``@action`` of DesignViewSet. A new one fails ``test_every_action_is_classified``
# until it is listed here (and so goes through both the constrained-user and the
# read-only-token checks) or in SKIPPED_DESIGN_ACTIONS with a reason.
DESIGN_ACTION_MATRIX = {
    "preview_name": A({"post"}, {"item": {}}, readonly_token_ok=True),
    "rerun_naming_preview": A({"post"}, readonly_token_ok=True),
    "rerun_naming": A({"post"}),
    "recompute_distribution": A({"post"}, {"rack_id": 1}, readonly_token_ok=True),
    "save_steps": A({"post"}, {"steps": []}),
    "simulate_steps": A({"post"}, {"steps": []}, readonly_token_ok=True),
    "auto_order": A({"post"}, readonly_token_ok=True),
    "work_order": A({"get"}),
    "preview_template": A({"post"}, {"template": 1, "rack_id": 1}, readonly_token_ok=True),
    "add_rack": A({"post"}, {"rack_id": 1}),
    "create_planned_rack": A({"post"}, {"name": "x"}),
    "remove_rack": A({"post"}, {"rack_id": 1}),
    "rack_power": A({"get", "post"}, {"rack_id": 1}, query="?rack_id=1"),
    "apply": A({"get", "post"}),
    "power_source": A({"get"}, query="?rack_id=1"),
    "copy_feeds": A({"post"}, {"source_rack_id": 1, "target_rack_id": 1}),
    "feeds": A({"get"}, query="?rack_id=1"),
    "planned_feed": A({"get", "post", "delete"}, {"rack_id": 1, "name": "x"}, query="?rack_id=1"),
    "save_layout": A({"post"}, {"placements": []}),
    "chain": A({"get"}),
    "conflicts": A({"get"}),
    "derive": A({"post"}, {"title": "derived"}),
    "new_version": A({"post"}),
    "rebase": A({"post"}, {"based_on": 1}),
}

# Detail @actions deliberately left out of the matrix, with why. Empty today:
# every DesignViewSet @action is a detail route and goes through the matrix.
SKIPPED_DESIGN_ACTIONS = {}

# @actions of the OTHER registered viewsets, all collection-level (no design in
# the URL, so there is nothing for a design-constrained user to be hidden from).
# They still go through the read-only-token sweep below. Format: viewset -> actions.
OTHER_VIEWSET_ACTIONS = {
    "TemplateViewSet": {"from_design", "from_rack"},
    "FavoriteDeviceTypeViewSet": {"toggle"},
    "HiddenDesignChassisViewSet": {"toggle"},
    "HiddenDesignRackViewSet": {"toggle", "show_all"},
}

# ViewSets with no extra @action whose unsafe methods the sweep covers through the
# standard routes. A new ViewSet registered on the router that is not a
# NetBoxModelViewSet / ReadOnly viewset also lands in the sweep automatically.


def registry():
    """``[(prefix, viewset_class, basename)]`` of the plugin API router."""
    return [(p, v, b) for p, v, b in api_urls.router.registry]


class MatrixBase(APITestCase):
    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.other_site = Site.objects.create(name="Other site", slug="other-site")
        cls.other_rack = Rack.objects.create(name="Other rack", site=cls.other_site)
        create_test_device("Other dev", site=cls.other_site, rack=cls.other_rack, position=1, face="front")

        # The design the constrained user must never learn anything about: titled,
        # based on another design, with a placement, a planned feed and a rack.
        base = make_design(title="ZZ-SECRET-BASE", site=cls.site, status=DesignStatusChoices.STATUS_APPROVED)
        cls.target = make_design(title=SECRET_TITLE, site=cls.site, based_on=base, description="ZZ-SECRET-DESC")
        cls.target.racks.add(cls.racks[0])
        DesignPlacement.objects.create(
            design=cls.target, kind="add", target_rack=cls.racks[0], target_position=5, target_face="front",
            device_type=env["device_type"], proposed_name=SECRET_PLACEMENT,
        )
        DesignPowerFeed.objects.create(design=cls.target, rack=cls.racks[0], name=SECRET_FEED)
        # The design the constrained user IS allowed to see.
        cls.mine = make_design(title="Mine", site=cls.site)

    def grant(self, model, actions, constraints=None):
        permission = ObjectPermission(
            name=f"guard-{model.__name__}-{'-'.join(actions)}-{ObjectPermission.objects.count()}",
            actions=list(actions), constraints=constraints,
        )
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))
        return permission

    def request(self, method, url, body=None):
        kwargs = {"format": "json", **self.header}
        if method in {"post", "put", "patch"}:
            return getattr(self.client, method)(url, body or {}, **kwargs)
        return getattr(self.client, method)(url, **kwargs)

    def design_url(self, name, pk, query=""):
        return reverse(f"{NS}:design-{name}", args=[pk]) + query


class ActionClassificationTest(SimpleTestCase):

    def test_every_action_is_classified(self):
        actual = {a.__name__ for a in DesignViewSet.get_extra_actions()}
        known = set(DESIGN_ACTION_MATRIX) | set(SKIPPED_DESIGN_ACTIONS)
        self.assertEqual(
            actual - known, set(),
            "New DesignViewSet @action(s) not in the security matrix. Add each to "
            "DESIGN_ACTION_MATRIX in tests/test_security_guards.py (so a design-constrained user "
            "and a read-only token are both tried against it) or to SKIPPED_DESIGN_ACTIONS with a reason.",
        )
        self.assertEqual(known - actual, set(), "Matrix entries for actions that no longer exist.")

    def test_every_matrix_action_is_a_detail_route_with_the_listed_methods(self):
        by_name = {a.__name__: a for a in DesignViewSet.get_extra_actions()}
        for name, spec in DESIGN_ACTION_MATRIX.items():
            action = by_name[name]
            self.assertTrue(action.detail, f"{name}: the matrix assumes a detail route")
            self.assertEqual({m.lower() for m in action.mapping}, spec["methods"], name)

    def test_every_other_viewset_action_is_classified(self):
        actual = {}
        for _prefix, viewset, _basename in registry():
            if viewset is DesignViewSet:
                continue
            for action in viewset.get_extra_actions():
                actual.setdefault(viewset.__name__, set()).add(action.__name__)
        self.assertEqual(
            actual, OTHER_VIEWSET_ACTIONS,
            "A viewset @action appeared or vanished. List it in OTHER_VIEWSET_ACTIONS in "
            "tests/test_security_guards.py; the read-only-token sweep covers it automatically, but "
            "decide whether it also needs a design-constrained check.",
        )

    def test_every_matrix_action_has_a_reachable_route(self):
        # reverse() each URL so a renamed url_path cannot leave a dead matrix row.
        for name in DESIGN_ACTION_MATRIX:
            url_name = next(a.url_name for a in DesignViewSet.get_extra_actions() if a.__name__ == name)
            reverse(f"{NS}:design-{url_name}", args=[1])


@override_settings(LOGIN_REQUIRED=True)
class ConstrainedUserMatrixTest(MatrixBase):
    """A user whose design permissions name only ``Mine`` meets every detail
    action aimed at another design: 403/404, never a 2xx and never the target's data."""

    def setUp(self):
        super().setUp()
        for verb in ("view", "change", "add", "delete"):
            self.grant(Design, [verb], {"pk": self.mine.pk})
        self.assertTrue(Design.objects.restrict(self.user, "view").filter(pk=self.mine.pk).exists())
        self.assertFalse(Design.objects.restrict(self.user, "view").filter(pk=self.target.pk).exists())

    def _by_name(self):
        return {a.__name__: a for a in DesignViewSet.get_extra_actions()}

    def _assert_blocked(self, label, response):
        body = response.content.decode()
        self.assertLess(response.status_code, 500, f"{label}: server error: {body[:300]}")
        self.assertNotEqual(response.status_code // 100, 2, f"{label}: succeeded against a hidden design")
        self.assertIn(response.status_code, {400, 403, 404, 405}, f"{label}: {response.status_code} {body[:300]}")
        for secret in (SECRET_TITLE, SECRET_PLACEMENT, SECRET_FEED, "ZZ-SECRET"):
            self.assertNotIn(secret, body, f"{label}: leaked {secret!r}")

    def test_detail_actions_hide_the_other_design(self):
        by_name = self._by_name()
        for name, spec in DESIGN_ACTION_MATRIX.items():
            url = self.design_url(by_name[name].url_name, self.target.pk, spec["query"])
            for method in sorted(spec["methods"]):
                with self.subTest(action=name, method=method):
                    response = self.request(method, url, spec["body"])
                    self._assert_blocked(f"{method.upper()} {name}", response)
                    if response.status_code == 400:
                        self.fail(f"{method.upper()} {name}: 400 before the object was hidden "
                                  f"(body validated first): {response.content.decode()[:200]}")

    def test_standard_routes_hide_the_other_design(self):
        detail = reverse(f"{NS}:design-detail", args=[self.target.pk])
        for method, body in (("get", None), ("put", {"title": "x", "sites": [self.site.pk]}),
                             ("patch", {"title": "x"}), ("delete", None)):
            with self.subTest(method=method):
                self._assert_blocked(f"{method.upper()} design-detail", self.request(method, detail, body))
        self.target.refresh_from_db()
        self.assertEqual(self.target.title, SECRET_TITLE)

    def test_list_does_not_contain_the_other_design(self):
        response = self.request("get", reverse(f"{NS}:design-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("ZZ-SECRET", response.content.decode())
        self.assertEqual([d["id"] for d in response.data["results"]], [self.mine.pk])

    def test_nothing_was_written_to_the_hidden_design(self):
        before = (Design.objects.count(), DesignPlacement.objects.filter(design=self.target).count(),
                  DesignPowerFeed.objects.filter(design=self.target).count())
        self.test_detail_actions_hide_the_other_design()
        self.test_standard_routes_hide_the_other_design()
        after = (Design.objects.count(), DesignPlacement.objects.filter(design=self.target).count(),
                 DesignPowerFeed.objects.filter(design=self.target).count())
        self.assertEqual(before, after)

    def test_the_matrix_is_not_vacuous(self):
        # The same requests against the design the user MAY see must get past the
        # object lookup (anything but 404/405): otherwise a typo in the URL or
        # body would make every "hidden" assertion above pass for the wrong reason.
        by_name = self._by_name()
        reachable = 0
        for name, spec in DESIGN_ACTION_MATRIX.items():
            url = self.design_url(by_name[name].url_name, self.mine.pk, spec["query"])
            for method in sorted(spec["methods"]):
                response = self.request(method, url, spec["body"])
                self.assertNotEqual(response.status_code, 405, f"{method} {name}")
                self.assertLess(response.status_code, 500, f"{method} {name}: {response.content[:300]}")
                if response.status_code != 404:
                    reachable += 1
        self.assertGreaterEqual(reachable, len(DESIGN_ACTION_MATRIX) - 3)


def _sweep_routes():
    """Every ``(label, viewset, url_name, kwargs, method)`` the router exposes on
    which an UNSAFE method is possible (standard routes + extra actions)."""
    routes = []
    for _prefix, viewset, basename in registry():
        actions = {m for m in ("create", "update", "partial_update", "destroy", "bulk_update",
                               "bulk_partial_update", "bulk_destroy") if hasattr(viewset, m)}
        if hasattr(viewset, "create"):
            routes.append((basename, viewset, f"{basename}-list", False, "post", "create"))
        if "bulk_update" in actions:
            routes.append((basename, viewset, f"{basename}-list", False, "put", "bulk_update"))
        if "bulk_partial_update" in actions:
            routes.append((basename, viewset, f"{basename}-list", False, "patch", "bulk_partial_update"))
        if "bulk_destroy" in actions:
            routes.append((basename, viewset, f"{basename}-list", False, "delete", "bulk_destroy"))
        for verb, method in (("update", "put"), ("partial_update", "patch"), ("destroy", "delete")):
            if verb in actions:
                routes.append((basename, viewset, f"{basename}-detail", True, method, verb))
        for action in viewset.get_extra_actions():
            for method in action.mapping:
                if method.lower() in UNSAFE_METHODS:
                    routes.append((basename, viewset, f"{basename}-{action.url_name}", action.detail,
                                   method.lower(), action.__name__))
    return routes


# Unsafe routes a read-only token MAY use: they write nothing durable (they
# preview or simulate inside a rolled-back transaction) and the MCP server runs
# with a read-only token. ``ViewDesignPermissions`` is the mechanism.
READONLY_TOKEN_OK = {
    ("design", "preview_name", "post"),
    ("design", "rerun_naming_preview", "post"),
    ("design", "recompute_distribution", "post"),
    ("design", "simulate_steps", "post"),
    ("design", "auto_order", "post"),
    ("design", "preview_template", "post"),
}


@override_settings(LOGIN_REQUIRED=True)
class ReadOnlyTokenMatrixTest(MatrixBase):
    """A superuser-grade account behind a ``write_enabled=False`` token may not
    use ANY unsafe method on ANY plugin route (except the documented preview
    POSTs): every ViewSet and every ``@action`` is enumerated from the router."""

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.token.write_enabled = False
        self.token.save()

    def _url(self, url_name, detail):
        args = [self.target.pk] if detail else []
        url = reverse(f"{NS}:{url_name}", args=args)
        return url + "?rack_id=1" if detail else url

    def test_unsafe_methods_are_403_for_a_read_only_token(self):
        routes = _sweep_routes()
        self.assertGreater(len(routes), 40)  # the sweep found the API, not nothing
        for basename, viewset, url_name, detail, method, label in routes:
            key = (basename, label, method)
            with self.subTest(route=url_name, method=method, handler=label):
                url = self._url(url_name, detail)
                response = self.request(method, url, {"rack_id": 1, "device_type_id": 1, "design_id": 1})
                if key in READONLY_TOKEN_OK:
                    self.assertNotEqual(response.status_code, 403, f"{key}: documented as allowed")
                    continue
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN,
                                 f"{method.upper()} {url_name} ({viewset.__name__}) -> "
                                 f"{response.status_code} {response.content.decode()[:200]}")

    def test_the_read_only_token_still_reads(self):
        for url_name, args in (("design-list", []), ("design-detail", [self.target.pk]),
                               ("design-chain", [self.target.pk]), ("design-conflicts", [self.target.pk])):
            with self.subTest(route=url_name):
                response = self.request("get", reverse(f"{NS}:{url_name}", args=args))
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_documented_readonly_ok_routes_exist(self):
        known = {(b, label, m) for b, _v, _u, _d, m, label in _sweep_routes()}
        self.assertEqual(READONLY_TOKEN_OK - known, set())

    def test_nothing_was_written(self):
        before = (Design.objects.count(), DesignPlacement.objects.count(), DesignPowerFeed.objects.count())
        self.test_unsafe_methods_are_403_for_a_read_only_token()
        self.assertEqual(
            before, (Design.objects.count(), DesignPlacement.objects.count(), DesignPowerFeed.objects.count()))
        self.target.refresh_from_db()
        self.assertEqual(self.target.title, SECRET_TITLE)


# =============================================================================
# 3. GraphQL lineage fields
# =============================================================================

@override_settings(LOGIN_REQUIRED=True)
class GraphQLLineageTest(MatrixBase):
    """``design { children ancestors lineage based_on root depends_on }`` as a
    user who may view only part of a design family never yields a hidden design's
    id or title (a hidden ancestor is a null / ``restricted`` placeholder)."""

    HIDDEN_MARKER = "ZZ-HIDDEN"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        APPROVED = DesignStatusChoices.STATUS_APPROVED
        make = lambda title, **kw: make_design(title=title, site=cls.site, **kw)  # noqa: E731
        cls.h1 = make("ZZ-HIDDEN-1", status=APPROVED)
        cls.h2 = make("ZZ-HIDDEN-2", status=APPROVED, based_on=cls.h1)
        cls.v = make("Visible mid", status=APPROVED, based_on=cls.h2)
        cls.leaf = make("Visible leaf", based_on=cls.v)
        cls.hidden_child = make("ZZ-HIDDEN-CHILD", based_on=cls.leaf)
        cls.visible_child = make("Visible child", based_on=cls.leaf)
        # root / depends_on pointing at hidden designs as well.
        Design.objects.filter(pk=cls.v.pk).update(root=cls.h1, version=2)
        Design.objects.filter(pk=cls.leaf.pk).update(root=cls.h1, version=3)
        cls.leaf.depends_on.add(cls.h1)
        cls.leaf.depends_on.add(cls.v)

    def setUp(self):
        super().setUp()
        self.grant(Design, ["view"], {"pk__in": [self.v.pk, self.leaf.pk, self.visible_child.pk]})

    def gql(self, query):
        response = self.client.post(reverse("graphql"), data={"query": query}, format="json", **self.header)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return json.loads(response.content)

    def test_lineage_fields_never_name_a_hidden_design(self):
        entry = "id title description sites { name } based_on { id title } root { id title } depends_on { id title }"
        query = (
            f"{{ design(id: {self.leaf.pk}) {{ id title "
            f"children {{ {entry} }} ancestors {{ {entry} }} "
            f"lineage {{ id title version status restricted design {{ {entry} }} }} "
            f"based_on {{ {entry} }} root {{ {entry} }} depends_on {{ {entry} }} }} }}"
        )
        data = self.gql(query)
        self.assertNotIn("errors", data, data.get("errors"))
        raw = json.dumps(data)
        self.assertNotIn(self.HIDDEN_MARKER, raw)
        for hidden in (self.h1, self.h2, self.hidden_child):
            self.assertNotIn(f'"id": "{hidden.pk}"', raw, f"hidden design id {hidden.pk} leaked")
        design = data["data"]["design"]
        # The visible parts still resolve (the checks above are not passing on an empty answer).
        self.assertEqual([c["title"] for c in design["children"]], ["Visible child"])
        self.assertEqual(len(design["lineage"]), 3)  # chain length kept, hidden entries redacted
        self.assertEqual([e["restricted"] for e in design["lineage"]], [True, True, False])
        self.assertEqual(design["based_on"]["title"], "Visible mid")
        self.assertEqual([d["title"] for d in design["depends_on"]], ["Visible mid"])
        self.assertIsNone(design["root"])

    def test_a_hidden_design_cannot_be_queried_directly(self):
        data = self.gql(f"{{ design(id: {self.h1.pk}) {{ id title }} }}")
        self.assertNotIn(self.HIDDEN_MARKER, json.dumps(data))
        self.assertFalse((data.get("data") or {}).get("design"))
