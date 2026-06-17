r"""从 arXiv LaTeX 源码提取论文表格。

支持的表格类型
--------------
绝大多数论文使用 ``\begin{table}`` 浮动体包裹 ``tabular``、``tabulary`` 或
``tabularx`` 环境。极少数使用 ``\pgfplotstabletypeset``（pgfplotstable 宏包），
从外部数据文件生成表格，不包含任何 tabular 类环境。

渲染流程
--------
基于 tabular 的表格（常见情况）：
  1. 用 pdflatex 在 standalone 文档中编译提取的 tabular 主体。
     提取并注入 preamble 中用户自定义的宏（``\newcommand``、``\definecolor`` 等）；
     对样式宏包中 ``\renewcommand`` 产生的未定义命令创建 stub；
     fallback stub 覆盖表格单元格中常见的引用/图标命令
     （``\citep``、``\citet``、``\parencite``、``\faGithub`` 等）。
  2. 若 pdflatex 编译原始 body 失败，用更安全的变体重试
     （剥离 ``\resizebox`` / ``\scalebox`` / ``\adjustbox`` 包裹，
     移除 ``center`` / ``\centering``）。
  3. 若两次 LaTeX 尝试均失败，回退到 matplotlib —— 解析 tabular 行，
     清洗单元格标记，渲染为 booktabs 风格的 PNG。

基于 pgfplotstable 的表格：
  1. 内联外部数据文件（``\pgfplotstableread{file}``），
     使 standalone 编译自包含。
  2. 用 pdflatex 编译（无重试，无 matplotlib 回退——
     没有 tabular 可供解析）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).parent))
from _lib import FigureInfo
from _render_repair import repair_render

_MAX_FILE_BYTES = 18 * 1024 * 1024

# 匹配 \begin{table} ... \end{table}（含 table* 双栏浮动体）。
# 绝大多数论文在此浮动体内包裹 tabular/tabulary/tabularx 环境。
_TABLE_ENV = re.compile(
    r"\\begin\{table\*?\}(?:\[[^\]]*\])?(.*?)\\end\{table\*?\}",
    re.DOTALL,
)

# 表格浮动体内的 tabular 类环境。
# 覆盖三种主流 LaTeX 表格引擎：tabular、tabulary、tabularx。
# 不匹配 \pgfplotstabletypeset（单独处理）。
_TABULAR_FULL = re.compile(
    r"(\\begin\{(?:tabular|tabulary|tabularx)\*?\}(?:\{[^}]*\})?\{[^}]*\}.*?\\end\{(?:tabular|tabulary|tabularx)\*?\})",
    re.DOTALL,
)

# pgfplotstable 生成的表格：\pgfplotstabletypeset 从外部文件读取数据
# （通过 \pgfplotstableread 加载），直接渲染为表格，无 tabular 环境。
# 在 ML 论文中极少见（语料中约 1%）。
_PGFPLOTSTABLE = re.compile(r"\\pgfplotstabletypeset")

_LABEL = re.compile(r"\\label\{([^}]+)\}")
_TEX_COMMENT = re.compile(r"(?<!\\)%.*")

_LATEX_TEMPLATE = r"""\documentclass[border=6pt]{standalone}
\usepackage{booktabs}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{bm}
\usepackage{array}
\usepackage{multirow}
\usepackage{xcolor}
\usepackage{colortbl}
\usepackage{makecell}
\usepackage{rotating}
\usepackage{graphicx}
\usepackage{adjustbox}
\usepackage{tabularx}
\usepackage{tabulary}
\usepackage{caption}
\usepackage{pifont}
\usepackage{pgfplotstable}
\usepackage{siunitx}
\usepackage{xspace}
\setlength{\textwidth}{@@TEXT_WIDTH@@}
\setlength{\columnwidth}{\textwidth}
\setlength{\linewidth}{\textwidth}
@@RENEW_STUBS@@
@@PREAMBLE_MACROS@@
% 预备定义（stub）：仅在 preamble 宏之后仍未定义时生效。
% natbib / biblatex 引用命令，常见于表格单元格。替换为方括号引用键。
\providecommand{\parencite}[1]{[#1]}
\providecommand{\citep}[1]{[#1]}
\providecommand{\citet}[1]{#1}
% fontawesome / fontawesome5 图标命令，替换为空。
\providecommand{\faGithub}{}
\providecommand{\faEnvelopeO}{}
\begin{document}
\begin{minipage}{\textwidth}
@@TABLE_BODY@@
\end{minipage}
\end{document}
"""


# ── 辅助函数 ────────────────────────────────────────────────────────────────────


def _extract_renewcommand_stubs(tex: str) -> str:
    r"""为 preamble 中被 ``\renewcommand`` 重定义的命令创建 ``\providecommand`` 占位。

    没有占位时，若命令由未在我们 standalone 模板中加载的样式宏包定义，
    ``\renewcommand{\somecmd}`` 会因命令未定义而编译失败。
    """
    preamble = tex.split(r"\begin{document}", 1)[0]
    # 同时匹配 \renewcommand{\cmd} 和 \renewcommand\cmd（无括号形式）
    matches = re.findall(
        r"\\renewcommand\*?\s*(?:\{(\\[A-Za-z@]+)\}|(\\[A-Za-z@]+))", preamble
    )
    commands = sorted({cmd for pair in matches for cmd in pair if cmd})
    return "\n".join(f"\\providecommand{{{cmd}}}{{}}" for cmd in commands)


def _probe_textwidth(tex: str) -> str | None:
    r"""编译最小探测文档以实测 ``\textwidth``。"""
    if shutil.which("pdflatex") is None:
        return None
    preamble = (
        tex.split(r"\begin{document}", 1)[0] if r"\begin{document}" in tex else ""
    )
    dc_match = re.search(r"\\documentclass(?:\[[^\]]*\])?\{[^}]+\}", preamble)
    if not dc_match:
        return None

    probe_lines = [dc_match.group(0)]
    for m in re.finditer(r"\\usepackage(?:\[[^\]]*\])?\{geometry\}", preamble):
        probe_lines.append(m.group(0))
    for m in re.finditer(r"\\geometry\{[^}]+\}", preamble):
        probe_lines.append(m.group(0))
    for m in re.finditer(
        r"\\usepackage(?:\[[^\]]*\])?\{(?:neurips|iclr|acl|icml|nips|arxiv|aaai|jmlr|acmart)[^}]*\}",
        preamble,
    ):
        probe_lines.append(m.group(0))

    probe_src = "\n".join(probe_lines) + (
        "\n\\begin{document}\n"
        "\\typeout{PROBED_TEXTWIDTH=\\the\\textwidth}\n"
        "\\end{document}\n"
    )
    try:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            tex_file = tmpdir / "probe.tex"
            tex_file.write_text(probe_src, encoding="utf-8")
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-draftmode", "probe.tex"],
                cwd=tmpdir,
                capture_output=True,
                timeout=15,
            )
            stdout = result.stdout.decode("utf-8", errors="replace")
            tw_m = re.search(r"PROBED_TEXTWIDTH=([\d.]+)pt", stdout)
            if tw_m:
                inches = float(tw_m.group(1)) / 72.27
                if 2.5 <= inches <= 9.0:
                    return f"{inches:.2f}in"
    except Exception:
        pass
    return None


def _estimate_textwidth(tex: str) -> str:
    r"""通过启发式规则估算 ``\textwidth``。"""
    preamble = (
        tex.split(r"\begin{document}", 1)[0] if r"\begin{document}" in tex else ""
    )
    if not preamble:
        return "6.50in"

    geom = re.search(r"\\geometry\{([^}]+)\}", preamble)
    if geom:
        tw = re.search(r"textwidth\s*=\s*([\d.]+\s*(?:in|cm|mm|pt))", geom.group(1))
        if tw:
            return tw.group(1).replace(" ", "")

    style_widths = {
        "neurips": "5.50in",
        "nips": "5.50in",
        "iclr": "5.50in",
        "icml": "6.75in",
        "acl": "6.30in",
        "emnlp": "6.30in",
        "naacl": "6.30in",
        "aaai": "7.00in",
        "jmlr": "6.00in",
    }
    for pkg, width in style_widths.items():
        if re.search(rf"\\usepackage(?:\[[^\]]*\])?\{{{pkg}", preamble):
            return width

    dc = re.search(r"\\documentclass(?:\[([^\]]*)\])?\{([^}]+)\}", preamble)
    if dc:
        options = dc.group(1) or ""
        classname = dc.group(2).lower()
        if classname == "ieeetran":
            return "7.16in"
        if classname == "llncs":
            return "4.75in"
        if classname.startswith("acm") or classname == "sig-alternate":
            return "7.00in"
        if classname.startswith("revtex"):
            return "3.40in"
        if "twocolumn" in options:
            return "7.16in"

    return "6.50in"


def _detect_textwidth(tex: str) -> str:
    r"""确定论文的 ``\textwidth`` —— 优先探测，回退到启发式估算。"""
    probed = _probe_textwidth(tex)
    if probed is not None:
        return probed
    return _estimate_textwidth(tex)


def _strip_tex_comments(text: str) -> str:
    return _TEX_COMMENT.sub("", text)


def _extract_brace_content(text: str, pos: int) -> str:
    depth = 1
    i = pos
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[pos : i - 1]


def _find_caption(env_text: str) -> str:
    idx = env_text.find(r"\caption")
    if idx == -1:
        return ""
    after = env_text[idx + len(r"\caption") :].lstrip()
    if after.startswith("["):
        close = after.find("]")
        if close != -1:
            after = after[close + 1 :].lstrip()
    if not after.startswith("{"):
        return ""
    raw = _extract_brace_content(after, 1)
    raw = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", raw)
    raw = re.sub(r"\\[a-zA-Z]+", " ", raw)
    raw = re.sub(r"[{}]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _brace_delta(text: str) -> int:
    """粗略的括号平衡估算，用于多行宏提取。"""
    return text.count("{") - text.count("}")


def _is_incomplete_macro_definition(block: str) -> bool:
    patterns = (
        r"\\newcommand\*?\s*(?:\{\\[A-Za-z@]+\}|\\[A-Za-z@]+)\s*$",
        r"\\renewcommand\*?\s*(?:\{\\[A-Za-z@]+\}|\\[A-Za-z@]+)\s*$",
        r"\\providecommand\*?\s*(?:\{\\[A-Za-z@]+\}|\\[A-Za-z@]+)\s*$",
    )
    return any(re.fullmatch(pattern, block.strip()) for pattern in patterns)


def _macro_dedupe_key(block: str) -> str | None:
    stripped = block.strip()
    patterns = (
        r"\\(?:newcommand|renewcommand|providecommand)\*?\s*(?:\{(\\[A-Za-z@]+)\}|(\\[A-Za-z@]+))",
        r"\\def\s*(\\[A-Za-z@]+)",
        r"\\DeclareMathOperator\*?\s*\{(\\[A-Za-z@]+)\}",
        r"\\let\s*(\\[A-Za-z@]+)",
        r"\\newcolumntype\s*\{([^}]+)\}",
        r"\\definecolor\s*\{([^}]+)\}",
        r"\\colorlet\s*\{([^}]+)\}",
    )
    for pattern in patterns:
        m = re.match(pattern, stripped)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        if groups:
            return groups[0]
    return None


_DEFAULT_MACRO_STARTERS = (
    r"\newcommand",
    r"\renewcommand",
    r"\def",
    r"\DeclareMathOperator",
    r"\providecommand",
    r"\newcolumntype",
    r"\definecolor",
    r"\colorlet",
    r"\let",
)


def _extract_macros(
    text: str, starters: tuple[str, ...] = _DEFAULT_MACRO_STARTERS
) -> str:
    """从任意文本中提取用户自定义的命令。

    覆盖 ``\\newcommand``、``\\renewcommand``、``\\def``、
    ``\\DeclareMathOperator``、``\\providecommand``、``\\newcolumntype``、
    ``\\definecolor``、``\\colorlet``、``\\let``。
    跨行的括号平衡定义作为整体捕获，不完整的定义（只有头部、同行无内容）丢弃。
    去重逻辑：同名宏保留最后出现的定义。
    """
    lines = text.splitlines()
    macros: list[str | None] = []
    macro_positions: dict[str, int] = {}
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s.startswith(starters):
            i += 1
            continue

        block = s
        brace_balance = _brace_delta(s)
        i += 1
        while brace_balance > 0 and i < len(lines):
            nxt = lines[i].rstrip()
            block += "\n" + nxt
            brace_balance += _brace_delta(nxt)
            i += 1
        if _is_incomplete_macro_definition(block):
            continue
        key = _macro_dedupe_key(block)
        if key is not None and key in macro_positions:
            macros[macro_positions[key]] = None
        if key is not None:
            macro_positions[key] = len(macros)
        macros.append(block)
    return "\n".join(block for block in macros if block)


def _extract_preamble_macros(tex: str) -> str:
    """从合并后的 preamble 中提取用户自定义的命令。"""
    preamble = tex.split(r"\begin{document}", 1)[0]
    return _extract_macros(preamble)


def _consume_balanced(text: str, start: int, open_char: str, close_char: str) -> int:
    """返回平衡括号组结束后的下一个索引（处理 [] 或 {} 分组）。"""
    if start >= len(text) or text[start] != open_char:
        return start
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _remove_command_calls(text: str, command: str) -> str:
    r"""移除形如 ``\caption[short]{long}`` 的命令调用。

    消耗一个可选 ``[...]`` 参数，然后消耗所有连续的 ``{...}`` 参数
    （处理多参数命令，如 ``\resizebox{width}{height}{content}``）。
    """
    needle = f"\\{command}"
    parts: list[str] = []
    pos = 0
    while True:
        idx = text.find(needle, pos)
        if idx == -1:
            parts.append(text[pos:])
            break

        parts.append(text[pos:idx])
        j = idx + len(needle)

        # 一个可选 [short] 参数
        while j < len(text) and text[j].isspace():
            j += 1
        if j < len(text) and text[j] == "[":
            j = _consume_balanced(text, j, "[", "]")

        # 所有连续的 {mandatory} 参数
        while True:
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] == "{":
                j = _consume_balanced(text, j, "{", "}")
            else:
                break

        pos = j

    return "".join(parts)


def _strip_wrapper_commands(text: str, commands: tuple[str, ...]) -> str:
    body = text
    for command in commands:
        body = _remove_command_calls(body, command)
    return body


def _prepare_table_body(env_text: str) -> str:
    r"""剥离浮动体元数据，保留表格 body。

    同时用于 tabular 类和 pgfplotstable 表格。仅剥离会在 standalone 编译中
    产生问题的浮动体级元数据（caption、label、vspace、负 hspace），
    实际生成表格的环境（tabular / tabulary / tabularx / pgfplotstabletypeset）
    保留不动。

    覆盖的边界情况：
    - ``\\caption`` / ``\\caption*``，含可选 ``[short]`` 参数
    - ``\\label``
    - ``\\vspace`` / ``\\vspace*`` —— 负值裁剪内容，正值产生无意义空白
    - ``\\hspace`` / ``\\hspace*`` 带负参数 —— 将内容水平移出裁剪区域
    - 连续空行 —— 压缩为一行（tabular 列定义中出现多个空行
      会导致 ``\\@@array`` 解析错误）
    """
    body = _strip_wrapper_commands(env_text, ("caption", "caption*", "label"))
    # 剥离 \vspace / \vspace*：浮动体定位技巧，会收缩 standalone 包围盒
    # （负值 → 裁剪底部；正值 → 无意义空白）。tabular 内部行间距使用 \\[Xpt] 语法。
    body = re.sub(r"\\vspace\*?\{[^}]*\}", "", body)
    # 剥离负 \hspace / \hspace*：会将内容水平移出测量包围盒，裁剪渲染图像。
    body = re.sub(r"\\hspace\*?\{-[^}]*\}", "", body)
    # 压缩空行：tabular 列定义中出现多个空行会导致 \par 错误（\@@array 失败），
    # 单元格内同样非法。
    body = re.sub(r"\n[ \t]*\n", "\n", body)
    return body.strip()


def _prepare_table_body_retry(env_text: str) -> str:
    r"""构建更安全的 body 变体，用于 pdflatex 第二次编译尝试。

    仅用于 tabular 类表格（pgfplotstable 无需此步骤——
    不存在需要剥离的 resizebox / center 包裹）。

    此重试步骤解决的边界情况：
    - ``\\resizebox`` / ``\\scalebox`` / ``\\adjustbox`` 包裹 tabular
      —— 这些尺寸调整命令通常引用的 ``\\textwidth`` 在 standalone 文档中不同
    - ``\\begin{center}`` / ``\\end{center}`` / ``\\centering``
      —— 在 tabular 本地居中，而非在 minipage 中居中
    """
    body = _prepare_table_body(env_text)
    body = _strip_wrapper_commands(
        body,
        (
            "resizebox",
            "scalebox",
            "adjustbox",
        ),
    )
    body = re.sub(r"\\begin\{center\}", "", body)
    body = re.sub(r"\\end\{center\}", "", body)
    body = re.sub(r"\\centering\b", "", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _inline_pgf_data(body: str, source_dir: Path) -> str:
    r"""将 ``\pgfplotstableread{file}\macro`` 替换为内联文件内容。

    pgfplotstable 表格通过 ``\pgfplotstableread[options]{path/to/data.dat}\loadedmacro``
    从外部文件读取数据。由于 ``_render_table_latex_source`` 在临时目录中编译，
    相对路径无法解析。将数据文件内容内联，使 standalone LaTeX 源码完全自包含。
    """

    def _replace(m: re.Match) -> str:
        options = m.group(1) or ""
        filepath = m.group(2).strip()
        macro = m.group(3)
        data_path = source_dir / filepath
        if data_path.is_file():
            return f"\\pgfplotstableread[{options}]{{{data_path.read_text()}}}{macro}"
        return m.group(0)

    return re.sub(
        r"\\pgfplotstableread(?:\[([^\]]*)\])?\{([^}]+)\}(\\[A-Za-z@]+)",
        _replace,
        body,
        count=0,
        flags=re.DOTALL,
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="replace")


def _write_render_json(debug_dir: Path, stem: str, data: dict) -> None:
    """将完整渲染结果记录持久化为 JSON。"""
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"{stem}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_render_json(debug_dir: Path, stem: str) -> dict:
    """加载之前写入的渲染结果记录，不存在则返回 {}。"""
    p = debug_dir / f"{stem}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_status_renderer(debug_dir: Path, stem: str) -> str:
    return _read_render_json(debug_dir, stem).get("renderer", "")


def _render_table_latex_source(
    tex_src: str,
    output_path: Path,
    *,
    debug_dir: Path | None = None,
    stem: str = "table",
    status_note: str = "",
) -> bool:
    # 编译前始终持久化 LaTeX 源码，以便在成功和失败路径中保留。
    if debug_dir is not None:
        _write_text(debug_dir / f"{stem}.latex.tex", tex_src)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tex_file = tmp / "table.tex"
            tex_file.write_text(tex_src, encoding="utf-8")

            result = subprocess.run(
                [
                    "pdflatex",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "table.tex",
                ],
                cwd=tmpdir,
                capture_output=True,
                timeout=30,
            )

            pdf_file = tmp / "table.pdf"
            log_file = tmp / "table.log"
            if result.returncode != 0 or not pdf_file.exists():
                if debug_dir is not None:
                    _write_text(
                        debug_dir / f"{stem}.latex.stdout.txt",
                        result.stdout.decode("utf-8", errors="replace"),
                    )
                    _write_text(
                        debug_dir / f"{stem}.latex.stderr.txt",
                        result.stderr.decode("utf-8", errors="replace"),
                    )
                    if log_file.exists():
                        shutil.copy2(log_file, debug_dir / f"{stem}.latex.log")
                return False

            import fitz  # PyMuPDF

            doc = fitz.open(str(pdf_file))
            if len(doc) == 0:
                doc.close()
                return False

            page = doc[0]
            mat = fitz.Matrix(2.5, 2.5)  # ~225 dpi
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(str(output_path))
            doc.close()
            return True
    except Exception:
        if debug_dir is not None:
            _write_text(
                debug_dir / f"{stem}.latex.stderr.txt",
                "python_exception during compilation",
            )
        return False


def _render_table_latex(
    table_body: str,
    output_path: Path,
    preamble_macros: str = "",
    *,
    renew_stubs: str = "",
    text_width: str = "6.50in",
    debug_dir: Path | None = None,
    stem: str = "table",
    retry_table_body: str | None = None,
    repair: bool = False,
    max_repair_attempts: int = 3,
    enable_llm_repair: bool = False,
) -> bool:
    """将 ``table_body`` 编译为 standalone LaTeX 文档并光栅化为 PNG。

    同时处理 tabular 类和 pgfplotstable body。对于 tabular 表格，
    可提供重试变体（``retry_table_body``）——若首次编译失败且重试 body 不同，
    则进行第二次尝试。

    当 ``repair`` 为 True 时，启用 check → diagnose → repair → re-render 循环，
    自动尝试修复 LaTeX 编译或渲染质量问题。

    成功返回 True，任何失败（pdflatex 缺失、编译错误、空 PDF 等）返回 False。
    """
    if not shutil.which("pdflatex"):
        return False

    if debug_dir is None:
        debug_dir = output_path.parent / "debug"

    if not repair:
        attempts = [(table_body, "compile_error")]
        if (
            retry_table_body
            and retry_table_body.strip()
            and retry_table_body != table_body
        ):
            attempts.append((retry_table_body, "compile_error_retry"))

        for current_body, status_note in attempts:
            tex_src = (
                _LATEX_TEMPLATE.replace("@@RENEW_STUBS@@", renew_stubs)
                .replace("@@PREAMBLE_MACROS@@", preamble_macros)
                .replace("@@TABLE_BODY@@", current_body)
                .replace("@@TEXT_WIDTH@@", text_width)
            )
            if _render_table_latex_source(
                tex_src,
                output_path,
                debug_dir=debug_dir,
                stem=stem,
                status_note=status_note,
            ):
                return True

        return False

    # Repair path: build initial source and enter the repair loop.
    tex_src = (
        _LATEX_TEMPLATE.replace("@@RENEW_STUBS@@", renew_stubs)
        .replace("@@PREAMBLE_MACROS@@", preamble_macros)
        .replace("@@TABLE_BODY@@", table_body)
        .replace("@@TEXT_WIDTH@@", text_width)
    )

    def render_func(ts: str, op: Path) -> bool:
        return _render_table_latex_source(
            ts,
            op,
            debug_dir=debug_dir,
            stem=stem,
            status_note="repair_attempt",
        )

    result = repair_render(
        tex_src,
        output_path,
        debug_dir,
        stem,
        render_func,
        kind="table",
        max_attempts=max_repair_attempts,
        enable_llm=enable_llm_repair,
    )
    return result.success


# ── matplotlib 回退 ─────────────────────────────────────────────────────────────


def _clean_cell(text: str) -> str:
    text = re.sub(r"\\multicolumn\{\d+\}\{[^}]*\}\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\multirow\{\d+\}\{[^}]*\}\{([^}]*)\}", r"\1", text)
    for cmd in (
        "textbf",
        "textit",
        "underline",
        "emph",
        "textrm",
        "texttt",
        "textsc",
        "text",
    ):
        text = re.sub(rf"\\{cmd}\{{([^}}]*)\}}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?", "", text)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_tabular_rows(env_text: str) -> list[list[str]]:
    """解析 tabular 行，供 matplotlib 回退路径使用。

    仅处理 tabular / tabulary / tabularx。pgfplotstable 表格没有可解析的行，
    永远不会到达此函数（matplotlib 回退由 ``parse_tables`` 中的
    ``has_matplotlib_fallback`` 条件控制）。

    剥离 booktabs 横线命令、行列着色，清洗单元格标记
    （``\\textbf``、``\\multirow``、``\\multicolumn`` 等）。
    返回纯文本单元格内容的二维列表。
    """
    tab_m = re.search(
        r"\\begin\{(?:tabular|tabulary|tabularx)\*?\}(?:\{[^}]*\})?\{[^}]*\}(.*?)\\end\{(?:tabular|tabulary|tabularx)\*?\}",
        env_text,
        re.DOTALL,
    )
    if not tab_m:
        return []
    content = tab_m.group(1)
    content = re.sub(r"\\(?:hline|toprule|midrule|bottomrule)\b", "", content)
    content = re.sub(r"\\cline\{[^}]*\}", "", content)
    content = re.sub(r"\\(?:rowcolor|cellcolor)(?:\[[^\]]*\])?\{[^}]*\}", "", content)
    raw_rows = re.split(r"\\\\(?:\[[^\]]*\])?", content)
    result: list[list[str]] = []
    for raw_row in raw_rows:
        raw_row = raw_row.strip()
        if not raw_row:
            continue
        cells = [_clean_cell(c) for c in raw_row.split("&")]
        if any(c for c in cells):
            result.append(cells)
    return result


def _trim_whitespace(image_path: Path, padding: int = 12) -> None:
    try:
        import numpy as np
        from PIL import Image

        img = Image.open(str(image_path)).convert("RGB")
        arr = np.array(img)
        mask = (arr < 250).any(axis=2)
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        if len(rows) == 0 or len(cols) == 0:
            return
        top = max(0, rows[0] - padding)
        bottom = min(img.height, rows[-1] + padding + 1)
        left = max(0, cols[0] - padding)
        right = min(img.width, cols[-1] + padding + 1)
        img.crop((left, top, right, bottom)).save(str(image_path))
    except Exception:
        pass


def _render_table_matplotlib(
    rows: list[list[str]],
    output_path: Path,
    debug_dir: Path | None = None,
    stem: str = "table",
) -> bool:
    """最后手段的渲染器，仅用于 tabular 类表格。

    仅在两次 pdflatex 尝试均失败后到达。pgfplotstable 表格无法使用
    （没有可解析的 tabular 行）。

    渲染 booktabs 风格 PNG：上线（1.5 pt）、表头线（0.8 pt）、
    底线（1.5 pt），裁剪多余空白。
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.lines as mlines
        import matplotlib.pyplot as plt

        if not rows:
            return False

        n_cols = max(len(row) for row in rows)
        norm = [row + [""] * (n_cols - len(row)) for row in rows]
        header = norm[0]
        data = norm[1:] if len(norm) > 1 else [[""] * n_cols]

        fig_w = max(5.0, n_cols * 1.8)
        fig_h = max(1.2, (len(data) + 1) * 0.48 + 0.3)
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        ax.axis("off")

        tbl = ax.table(cellText=data, colLabels=header, loc="center", cellLoc="center")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(10)
        tbl.scale(1.0, 1.18)
        tbl.auto_set_column_width(list(range(n_cols)))

        cells_dict = tbl.get_celld()
        for (row, col), cell in cells_dict.items():
            cell.set_linewidth(0)
            cell.set_edgecolor("white")
            cell.set_facecolor("white")
            if row == 0:
                cell.set_text_props(weight="bold")

        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        all_bboxes = [cell.get_window_extent(renderer) for cell in cells_dict.values()]
        header_bboxes = [
            cell.get_window_extent(renderer)
            for (row, _), cell in cells_dict.items()
            if row == 0
        ]
        x0_px = min(b.x0 for b in all_bboxes)
        x1_px = max(b.x1 for b in all_bboxes)
        y_top_px = max(b.y1 for b in all_bboxes)
        y_bottom_px = min(b.y0 for b in all_bboxes)
        y_hdr_bottom_px = min(b.y0 for b in header_bboxes)
        inv = fig.transFigure.inverted()

        def _hline(y_px: float, lw: float) -> None:
            x0_f = inv.transform((x0_px, y_px))[0]
            x1_f = inv.transform((x1_px, y_px))[0]
            y_f = inv.transform((x0_px, y_px))[1]
            fig.add_artist(
                mlines.Line2D(
                    [x0_f, x1_f],
                    [y_f, y_f],
                    transform=fig.transFigure,
                    color="black",
                    linewidth=lw,
                    clip_on=False,
                )
            )

        _hline(y_top_px, 1.5)
        _hline(y_hdr_bottom_px, 0.8)
        _hline(y_bottom_px, 1.5)

        fig.patch.set_facecolor("white")
        plt.savefig(
            str(output_path),
            dpi=150,
            bbox_inches="tight",
            pad_inches=0.05,
            facecolor="white",
            edgecolor="none",
        )
        plt.close(fig)
        _trim_whitespace(output_path)
        return True
    except Exception:
        return False


# ── 公开 API ─────────────────────────────────────────────────────────────────────


def parse_tables(
    tex_path: Path,
    tables_dir: Path,
    max_tables: int = 10,
    force_rerender: bool = False,
    repair: bool = False,
    max_repair_attempts: int = 3,
    enable_llm_repair: bool = False,
) -> list[FigureInfo]:
    """解析合并后的 LaTeX 文件，返回渲染为 PNG 的表格信息列表。

    支持两种表格类型，详见模块 docstring。

    若 ``tex_path`` 不存在或无法读取，返回空列表。

    Args:
        repair: 启用 check → diagnose → repair → re-render 循环。
        max_repair_attempts: 每个表格最大修复尝试次数。
        enable_llm_repair: 规则修复失败后是否调用 LLM 兜底。
    """
    if not tex_path.exists():
        return []

    try:
        tex = tex_path.read_text(errors="replace")
    except Exception:
        return []

    tables_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = tex_path.parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    preamble = tex.split(r"\begin{document}", 1)[0]
    preamble_macros = _extract_macros(preamble)
    renew_stubs = _extract_renewcommand_stubs(tex)
    text_width = _detect_textwidth(tex)
    doc_pos = tex.find(r"\begin{document}")

    results: list[FigureInfo] = []
    tbl_number = 0

    for m in _TABLE_ENV.finditer(tex):
        env_text = _strip_tex_comments(m.group(1))

        # ── 检测表格类型 ───────────────────────────────────────────
        # 支持两种类型：
        #   1. 基于 tabular：\begin{table} 内嵌 \begin{tabular}
        #      （或 tabulary/tabularx）。绝大多数论文（≥99%）使用此方式。
        #   2. 基于 pgfplotstable：\pgfplotstabletypeset，无 tabular
        #      环境。极少见（~1%），从外部文件读取数据。
        tab_full = _TABULAR_FULL.search(env_text)
        is_pgf = bool(_PGFPLOTSTABLE.search(env_text)) if not tab_full else False

        if not tab_full and not is_pgf:
            continue

        # ── 准备表格 body ──────────────────────────────────────────
        if is_pgf:
            # pgfplotstable：保留完整 body（caption/label/hspace/vspace
            # 已由 _prepare_table_body 剥离）。内联外部数据文件，
            # 使编译自包含（pdflatex 使用的临时目录无法访问外部文件）。
            source_dir = tex_path.parent / "source"
            table_body = _inline_pgf_data(_prepare_table_body(env_text), source_dir)
            retry_table_body = None
            has_matplotlib_fallback = False
        else:
            table_body = _prepare_table_body(env_text)
            retry_table_body = _prepare_table_body_retry(env_text)
            has_matplotlib_fallback = True
        tabular_raw = tab_full.group(1) if tab_full else ""

        caption = _find_caption(env_text)
        label_m = _LABEL.search(env_text)
        label = label_m.group(1) if label_m else ""

        tbl_number += 1
        stem = f"table_{tbl_number:02d}"
        output_path = tables_dir / f"{stem}.png"
        render_backend = ""

        if output_path.exists() and not force_rerender:
            render_backend = _read_status_renderer(debug_dir, stem) or "cached"
        else:
            output_path.unlink(missing_ok=True)

            # ── 提取 body 中该表之前的宏定义 ───────────────────
            # 少数论文在 document body 内（\begin{document} 之后）
            # 定义 \definecolor / \newcommand / \def，紧贴在表格之前。
            # 仅扫描 preamble 会漏掉这些定义，导致 LaTeX 编译失败。
            if doc_pos != -1 and doc_pos < m.start():
                body_before = tex[doc_pos : m.start()]
                table_macros = _extract_macros(preamble + "\n" + body_before)
            else:
                table_macros = preamble_macros

            # ── LaTeX 编译 ──────────────────────────────────────
            # 用原始 body 尝试编译；仅对 tabular 表格用剥离变体重试
            # （pgfplotstable 没有需要剥离的 tabular 包裹）。
            ok = _render_table_latex(
                table_body or tabular_raw,
                output_path,
                table_macros,
                renew_stubs=renew_stubs,
                text_width=text_width,
                debug_dir=debug_dir,
                stem=stem,
                retry_table_body=retry_table_body or tabular_raw
                if retry_table_body is not None
                else None,
                repair=repair,
                max_repair_attempts=max_repair_attempts,
                enable_llm_repair=enable_llm_repair,
            )
            if ok:
                render_backend = "latex"

            # ── matplotlib 回退 ─────────────────────────────────
            # 仅用于 tabular 类表格。pgfplotstable 没有可解析的
            # tabular 行，因此仅走 LaTeX 路线。
            if not ok and has_matplotlib_fallback:
                rows = _parse_tabular_rows(env_text)
                ok = _render_table_matplotlib(
                    rows,
                    output_path,
                    debug_dir=debug_dir,
                    stem=stem,
                )
                if ok:
                    render_backend = "matplotlib"

            if not ok:
                _write_render_json(
                    debug_dir,
                    stem,
                    {
                        "kind": "table",
                        "number": tbl_number,
                        "status": "failed",
                        "caption": caption,
                        "label": label,
                        "latex_tex": f"{stem}.latex.tex"
                        if (debug_dir / f"{stem}.latex.tex").exists()
                        else None,
                        "stderr": f"{stem}.latex.stderr.txt"
                        if (debug_dir / f"{stem}.latex.stderr.txt").exists()
                        else None,
                    },
                )
                tbl_number -= 1
                continue

        try:
            if output_path.stat().st_size > _MAX_FILE_BYTES:
                tbl_number -= 1
                continue
        except OSError:
            tbl_number -= 1
            continue

        # 读取编译时使用的 LaTeX 源码（如有）
        latex_tex_path = debug_dir / f"{stem}.latex.tex"
        latex_source = (
            latex_tex_path.read_text(encoding="utf-8", errors="replace")
            if latex_tex_path.exists()
            else None
        )

        _write_render_json(
            debug_dir,
            stem,
            {
                "kind": "table",
                "number": tbl_number,
                "status": "success",
                "renderer": render_backend,
                "caption": caption,
                "label": label,
                "image": str(output_path.relative_to(tex_path.parent)),
                "text_width": text_width,
                "latex_source": latex_source,
            },
        )

        results.append(
            FigureInfo(
                image_path=output_path,
                caption=caption,
                label=label,
                number=tbl_number,
                kind="table",
                render_backend=render_backend,
            )
        )

        if len(results) >= max_tables:
            break

    return results
