"""
tests/test_url_integrity.py — Guarda contra `werkzeug.routing.BuildError`.

Motivação:
    Templates Jinja chamam `url_for('endpoint_name')`. Se um blueprint deixa
    de ser registrado (ou rota é renomeada), o erro só aparece em runtime —
    geralmente em produção, em qualquer página que herda do template afetado.

Estratégia:
    1. Carrega a app (via `app:app`).
    2. Faz parse estático de todos os arquivos `templates/**/*.html` extraindo
       chamadas `url_for('<endpoint>')` ou `url_for("<endpoint>")`.
    3. Confirma que cada endpoint extraído existe em `app.url_map`
       (via `app.view_functions`).

Limitações conscientes (documentadas, não bugs):
    - Endpoints construídos dinamicamente em runtime (ex.:
      `url_for('dashboard' if cond else 'login')`) ficam fora do parser
      estático — listamos esses casos em IGNORED_DYNAMIC se vierem a aparecer.
    - O blueprint 'static' do Flask sempre existe; chamadas
      `url_for('static', ...)` são tratadas como válidas.

Execução:
    DATABASE_URL=... pytest tests/test_url_integrity.py -v

Pré-requisito:
    A app precisa importar — ou seja, DATABASE_URL configurada (mesma
    convenção dos demais testes Postgres-only).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


# Fail-fast se DATABASE_URL ausente — app.py exige Postgres no boot.
if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL não configurada — este teste requer Postgres "
        "porque app.py inicializa db no import.",
        allow_module_level=True,
    )


# Endpoints conhecidos como dinâmicos — strings que aparecem em ternários
# ou montagem condicional dentro do template. Adicionar aqui só quando
# manualmente verificado que os candidatos reais estão cobertos.
IGNORED_DYNAMIC: set[str] = set()

# Diretório de templates a varrer.
TEMPLATES_DIR = ROOT / "templates"

# Regex captura url_for('nome') e url_for("nome"). Não tenta interpretar
# expressões — string-literal apenas. Endpoints dinâmicos (ternários,
# variáveis) são deliberadamente ignorados; não conseguimos resolvê-los
# estaticamente sem rodar Jinja.
URL_FOR_PATTERN = re.compile(
    r"""url_for\(\s*['"]([A-Za-z_][\w.]*)['"]"""
)


def _extract_endpoints_from_template(path: Path) -> set[str]:
    """Retorna conjunto de endpoints citados em um template."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return set(URL_FOR_PATTERN.findall(text))


def _collect_all_template_endpoints() -> dict[str, set[Path]]:
    """Mapeia endpoint -> conjunto de templates onde aparece."""
    found: dict[str, set[Path]] = {}
    for tpl in TEMPLATES_DIR.rglob("*.html"):
        for ep in _extract_endpoints_from_template(tpl):
            found.setdefault(ep, set()).add(tpl)
    return found


@pytest.fixture(scope="module")
def flask_app():
    """Importa a app de produção exatamente como o gunicorn faz."""
    from app import app as _app
    return _app


def test_templates_directory_exists():
    assert TEMPLATES_DIR.is_dir(), (
        f"Diretório de templates ausente: {TEMPLATES_DIR}"
    )


def test_at_least_one_template_scanned():
    """Sanity-check: parser está achando templates."""
    templates = list(TEMPLATES_DIR.rglob("*.html"))
    assert templates, "Nenhum template encontrado — parser quebrado?"


def test_all_template_endpoints_are_registered(flask_app):
    """
    Para cada endpoint citado em um template, exige que ele esteja
    registrado no Flask app. Falha listando endpoints órfãos e os
    arquivos onde foram encontrados.
    """
    used = _collect_all_template_endpoints()
    registered = set(flask_app.view_functions.keys())

    orphans: dict[str, set[Path]] = {
        ep: tpls
        for ep, tpls in used.items()
        if ep not in registered and ep not in IGNORED_DYNAMIC
    }

    if orphans:
        report_lines = ["Endpoints citados em templates mas não registrados na app:"]
        for ep in sorted(orphans):
            files = ", ".join(sorted(p.relative_to(ROOT).as_posix() for p in orphans[ep]))
            report_lines.append(f"  - {ep!r} usado em: {files}")
        report_lines.append("")
        report_lines.append(
            "Causa provável: blueprint não registrado em create_app(), "
            "ou rota renomeada sem atualizar o template."
        )
        pytest.fail("\n".join(report_lines))


def test_lookbook_endpoints_registered(flask_app):
    """
    Regressão dirigida: o bug que motivou esta suíte foi exatamente
    'lookbook.list_looks' não estar no url_map. Mantemos check explícito.
    """
    expected = {
        "lookbook.list_looks",
        "lookbook.create_look",
        "lookbook.edit_look",
        "lookbook.delete_look",
    }
    missing = expected - set(flask_app.view_functions.keys())
    assert not missing, (
        f"Endpoints lookbook ausentes: {sorted(missing)}. "
        "O blueprint foi registrado em app.py?"
    )


# ─────────────────────────────────────────────────────────────────────────
# Auditoria estática de blueprints/*.py
# Detecta NameError de globais antes que o erro chegue ao runtime.
# Motivação: extração de blueprints pode esquecer imports — bug típico
# após refactor. CI deve falhar nesse caso.
# ─────────────────────────────────────────────────────────────────────────
import ast
import builtins as _builtins

BLUEPRINTS_DIR = ROOT / "blueprints"


def _collect_module_defined(tree):
    """Nomes definidos no escopo do módulo (def, class, import, atribuição)."""
    defined = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                defined.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                defined.add(alias.asname or alias.name)
    return defined


def _collect_function_locals(func_node):
    """Variáveis locais visíveis dentro de uma função (params, atribuições,
    imports locais, alvos de for/with/except, comprehensions, etc.).

    Atenção: dict/set/list/generator-comprehensions têm escopo PRÓPRIO em
    Python 3, mas as variáveis vinculadas pelo comprehension não vazam para
    o escopo da função. Mesmo assim incluímos os targets aqui porque a
    auditoria opera sobre `ast.walk(func_node)` — varre tudo dentro da
    função, e `Name(Load)` dentro do comprehension precisa enxergar seu
    próprio target. Como não distinguimos escopos aninhados, a inclusão é
    pessimista: aceita o nome como local. Isto é seguro: o pior caso é
    deixar passar um NameError dentro de um comprehension, mas a varredura
    em `ast.walk` da função externa não confunde escopos para nomes
    realmente indefinidos no nível do módulo.
    """
    locs = set()
    for arg in func_node.args.args:
        locs.add(arg.arg)
    for arg in func_node.args.kwonlyargs:
        locs.add(arg.arg)
    if func_node.args.vararg:
        locs.add(func_node.args.vararg.arg)
    if func_node.args.kwarg:
        locs.add(func_node.args.kwarg.arg)
    for sub in ast.walk(func_node):
        if sub is func_node:
            continue
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            locs.add(sub.name)
        elif isinstance(sub, ast.Assign):
            for t in sub.targets:
                if isinstance(t, ast.Name):
                    locs.add(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for el in t.elts:
                        if isinstance(el, ast.Name):
                            locs.add(el.id)
        elif isinstance(sub, ast.AnnAssign):
            if isinstance(sub.target, ast.Name):
                locs.add(sub.target.id)
        elif isinstance(sub, ast.AugAssign):
            if isinstance(sub.target, ast.Name):
                locs.add(sub.target.id)
        elif isinstance(sub, (ast.For, ast.AsyncFor)):
            tgt = sub.target
            if isinstance(tgt, ast.Name):
                locs.add(tgt.id)
            elif isinstance(tgt, (ast.Tuple, ast.List)):
                for el in tgt.elts:
                    if isinstance(el, ast.Name):
                        locs.add(el.id)
        elif isinstance(sub, ast.comprehension):
            # Captura targets de list/dict/set/gen comprehensions
            tgt = sub.target
            if isinstance(tgt, ast.Name):
                locs.add(tgt.id)
            elif isinstance(tgt, (ast.Tuple, ast.List)):
                for el in tgt.elts:
                    if isinstance(el, ast.Name):
                        locs.add(el.id)
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                locs.add(alias.asname or alias.name)
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                locs.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                    locs.add(item.optional_vars.id)
        elif isinstance(sub, ast.ExceptHandler):
            if sub.name:
                locs.add(sub.name)
        elif isinstance(sub, ast.Lambda):
            for arg in sub.args.args:
                locs.add(arg.arg)
            for arg in sub.args.kwonlyargs:
                locs.add(arg.arg)
    return locs


def test_blueprint_modules_have_no_unresolved_names():
    """
    Cada arquivo .py em blueprints/ deve ter todos os Name (Load) referenciados
    em escopo de função resolvíveis para: definidos no módulo, locais da
    função, ou builtins. Caso contrário é NameError em runtime.
    """
    if not BLUEPRINTS_DIR.is_dir():
        pytest.skip("Diretório blueprints/ não existe ainda.")

    builtin_names = set(dir(_builtins))
    failures: list[str] = []

    for py in sorted(BLUEPRINTS_DIR.rglob("*.py")):
        if py.name == "__init__.py":
            continue
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(py))
        defined = _collect_module_defined(tree)

        for func in [n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            locs = _collect_function_locals(func)
            for sub in ast.walk(func):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    n = sub.id
                    if n in defined or n in locs or n in builtin_names:
                        continue
                    failures.append(
                        f"{py.relative_to(ROOT).as_posix()}:{sub.lineno} "
                        f"em {func.name}() referencia '{n}' que não foi "
                        f"definido nem importado."
                    )

    if failures:
        pytest.fail(
            "Símbolos não resolvidos em blueprints (causariam NameError em runtime):\n  "
            + "\n  ".join(failures)
        )
