"""Resolve raw import specifiers to files inside the project (best effort, per language family)."""
from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from typing import Iterable

JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte")
PY_EXTS = (".py", ".pyi")
JVM_EXTS = (".java", ".kt", ".kts", ".scala", ".groovy")
C_EXTS = (".h", ".hpp", ".hh", ".hxx", ".c", ".cc", ".cpp", ".cxx", ".m", ".mm")


def _noext(p: str) -> str:
    base, ext = posixpath.splitext(p)
    return base if ext else p


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


class Resolver:
    def __init__(self, paths: Iterable[str], aux: dict[str, str] | None = None) -> None:
        """paths: indexed code files. aux: {relpath: text} for go.mod / Cargo.toml files."""
        self.paths = set(paths)
        self.by_key: dict[str, list[str]] = defaultdict(list)    # lowercase last comp -> paths
        self.dirs: dict[str, list[str]] = defaultdict(list)
        self.py_pkgs: set[str] = set()
        for p in self.paths:
            d = posixpath.dirname(p)
            self.dirs[d].append(p)
            key = _noext(p)
            stem = key.rsplit("/", 1)[-1]
            if stem in ("__init__", "index", "mod") and d:
                if stem == "__init__":
                    self.py_pkgs.add(d)
                self.by_key[d.rsplit("/", 1)[-1].lower()].append(p)
            self.by_key[stem.lower()].append(p)
        self.go_mods: list[tuple[str, str]] = []  # (dir, module path)
        self.cargo_dirs: set[str] = set()
        for rel, text in (aux or {}).items():
            d = posixpath.dirname(rel)
            if rel.endswith("go.mod"):
                m = re.search(r"^\s*module\s+(\S+)", text, re.MULTILINE)
                if m:
                    self.go_mods.append((d, m.group(1)))
            elif rel.endswith("Cargo.toml"):
                self.cargo_dirs.add(d)
        self.go_mods.sort(key=lambda x: -len(x[1]))

    # ------------------------------------------------------------------ helpers
    def _first(self, cands: Iterable[str]) -> list[str]:
        for c in cands:
            if c in self.paths:
                return [c]
        return []

    def _with_exts(self, base: str, exts: tuple[str, ...], index: tuple[str, ...] = ()) -> list[str]:
        base = posixpath.normpath(base) if base else base
        if base.startswith("../") or base == "..":
            return []
        if base == ".":
            base = ""
        cands = [base] if posixpath.splitext(base)[1] else []
        cands += [base + e for e in exts]
        for idx in index:
            cands += [posixpath.join(base, idx + e) for e in exts]
        return self._first(c.lstrip("/") for c in cands if c)

    def _suffix(self, frag: str, exts: tuple[str, ...], ci: bool = False,
                ok=None) -> list[str]:
        frag = frag.strip("/")
        if not frag:
            return []
        last = frag.rsplit("/", 1)[-1].lower()
        want = frag.lower() if ci else frag
        out = []
        for p in self.by_key.get(last, ()):
            if not p.endswith(exts):
                continue
            key = _noext(p)
            if key.rsplit("/", 1)[-1] in ("__init__", "index", "mod"):
                key = posixpath.dirname(key)
            k = key.lower() if ci else key
            if k == want or k.endswith("/" + want):
                prefix = key[: len(key) - len(frag)].rstrip("/")
                if ok is None or ok(prefix, p):
                    out.append(p)
        return sorted(out, key=len)

    # ------------------------------------------------------------------ main entry
    def resolve(self, src: str, family: str, spec: str) -> list[str]:
        try:
            fn = getattr(self, "_r_" + family, None)
            res = fn(src, spec) if fn else []
        except Exception:
            return []
        return [r for r in res if r != src]

    def _r_python(self, src: str, spec: str) -> list[str]:
        dots = len(spec) - len(spec.lstrip("."))
        mod = spec[dots:]
        frag = mod.replace(".", "/")
        if dots:
            base = posixpath.dirname(src)
            for _ in range(dots - 1):
                base = posixpath.dirname(base)
            target = posixpath.join(base, frag) if frag else base
            return self._with_exts(target, PY_EXTS, ("__init__",)) if target else \
                self._first(["__init__.py"])
        if not frag:
            return []
        for root in ("", "src"):
            r = self._with_exts(posixpath.join(root, frag), PY_EXTS, ("__init__",))
            if r:
                return r
        # source-root rule: the prefix before the module path must not itself be a package
        return self._suffix(frag, PY_EXTS, ok=lambda prefix, _p: prefix not in self.py_pkgs)[:1]

    def _r_js(self, src: str, spec: str) -> list[str]:
        spec = spec.split("?", 1)[0]
        if spec.startswith("."):
            base = posixpath.join(posixpath.dirname(src), spec)
        elif spec.startswith(("@/", "~/")):
            rest = spec[2:]
            return (self._with_exts("src/" + rest, JS_EXTS, ("index",))
                    or self._with_exts(rest, JS_EXTS, ("index",)))
        elif spec.startswith("/"):
            base = spec.lstrip("/")
        else:
            return []
        r = self._with_exts(base, JS_EXTS, ("index",))
        if not r and base.endswith((".js", ".jsx", ".mjs", ".cjs")):
            r = self._with_exts(_noext(base), JS_EXTS, ("index",))
        return r

    def _r_go(self, src: str, spec: str) -> list[str]:
        for d, mod in self.go_mods:
            if spec == mod or spec.startswith(mod + "/"):
                rel = spec[len(mod):].lstrip("/")
                target = posixpath.normpath(posixpath.join(d, rel)) if (d or rel) else ""
                target = "" if target == "." else target
                return sorted(p for p in self.dirs.get(target, ())
                              if p.endswith(".go") and not p.endswith("_test.go"))
        return []

    def _r_jvm(self, src: str, spec: str) -> list[str]:
        if spec.endswith(".*"):
            frag = spec[:-2].replace(".", "/")
            out = []
            for d, files in self.dirs.items():
                if d == frag or d.endswith("/" + frag):
                    out += [p for p in files if p.endswith(JVM_EXTS)]
            return sorted(out)
        frag = spec.replace(".", "/")
        r = self._suffix(frag, JVM_EXTS)
        if not r and "/" in frag:  # static member or nested class import
            r = self._suffix(frag.rsplit("/", 1)[0], JVM_EXTS)
        return r[:1]

    def _rust_mod_dir(self, src: str) -> str:
        d, stem = posixpath.dirname(src), _noext(posixpath.basename(src))
        return d if stem in ("mod", "lib", "main") else posixpath.join(d, stem)

    def _rust_crate_src(self, src: str) -> str:
        d = posixpath.dirname(src)
        while True:
            if d in self.cargo_dirs:
                return posixpath.join(d, "src") if d else "src"
            if not d:
                break
            d = posixpath.dirname(d)
        parts = src.split("/")
        if "src" in parts:
            return "/".join(parts[: parts.index("src") + 1])
        return posixpath.dirname(src)

    def _r_rust(self, src: str, spec: str) -> list[str]:
        if spec.startswith("mod:"):
            base = posixpath.join(self._rust_mod_dir(src), spec[4:])
            return self._first([base + ".rs", base + "/mod.rs"])
        parts = [p for p in spec.split("::") if p]
        if not parts:
            return []
        head = parts[0]
        if head == "crate":
            base, rest = self._rust_crate_src(src), parts[1:]
        elif head == "self":
            base, rest = self._rust_mod_dir(src), parts[1:]
        elif head == "super":
            base = posixpath.dirname(self._rust_mod_dir(src))
            rest = parts[1:]
            while rest and rest[0] == "super":
                base, rest = posixpath.dirname(base), rest[1:]
        else:
            return []
        for i in range(len(rest), 0, -1):
            target = posixpath.join(base, *rest[:i])
            r = self._first([target + ".rs", target + "/mod.rs"])
            if r:
                return r
        return []

    def _r_c(self, src: str, spec: str) -> list[str]:
        system = spec.startswith("<")
        spec = spec.lstrip("<")
        cands = [] if system else [posixpath.join(posixpath.dirname(src), spec)]
        cands += [spec, "include/" + spec, "src/" + spec]
        r = self._first(posixpath.normpath(c) for c in cands)
        if r:
            return r
        hits = self._suffix(_noext(spec), C_EXTS)
        hits = [h for h in hits if h.endswith("/" + spec) or h == spec]
        return hits[:1] if len(hits) == 1 or not system else []

    def _r_ruby(self, src: str, spec: str) -> list[str]:
        if spec.startswith("."):
            return self._with_exts(posixpath.join(posixpath.dirname(src), spec), (".rb",))
        return (self._with_exts("lib/" + spec, (".rb",)) or self._with_exts(spec, (".rb",))
                or self._suffix(_noext(spec), (".rb",))[:1])

    def _r_php(self, src: str, spec: str) -> list[str]:
        if spec.startswith("ns:"):
            frag = spec[3:].replace("\\", "/").strip("/")
            return self._suffix(frag, (".php",), ci=True)[:1] or \
                self._suffix(frag.split("/", 1)[-1], (".php",), ci=True)[:1]
        return self._first([posixpath.normpath(posixpath.join(posixpath.dirname(src), spec)),
                            posixpath.normpath(spec)])

    def _r_zig(self, src: str, spec: str) -> list[str]:
        if not spec.endswith(".zig"):
            return []
        return self._first([posixpath.normpath(posixpath.join(posixpath.dirname(src), spec))])

    def _r_lua(self, src: str, spec: str) -> list[str]:
        frag = spec.replace(".", "/")
        return (self._with_exts(frag, (".lua",), ("init",))
                or self._suffix(frag, (".lua",))[:1])

    def _r_elixir(self, src: str, spec: str) -> list[str]:
        frag = "/".join(_snake(p) for p in spec.split("."))
        return self._suffix(frag, (".ex", ".exs"))[:1]

    def _r_dart(self, src: str, spec: str) -> list[str]:
        if spec.startswith("package:"):
            rest = spec[len("package:"):].split("/", 1)
            return self._first(["lib/" + rest[1]]) if len(rest) == 2 else []
        if spec.startswith("dart:"):
            return []
        return self._first([posixpath.normpath(posixpath.join(posixpath.dirname(src), spec))])

    def _r_shell(self, src: str, spec: str) -> list[str]:
        return self._first([posixpath.normpath(posixpath.join(posixpath.dirname(src), spec)),
                            posixpath.normpath(spec)])
