"""Interactive CLI chat session grounded on a single paper."""

from __future__ import annotations

from pathlib import Path

from paper_tool.config import get_config
from paper_tool.models import PaperMetadata


_SYSTEM_PROMPT_TEMPLATE = """\
你是一位专业的学术论文问答助手，正在帮助研究者深入理解以下论文。

**论文标题**: {title}
**作者**: {authors}

**论文全文（或节选）**:
{paper_text}

---
请根据上述论文内容回答用户的问题。回答要准确、清晰、有深度。
如果问题在论文中找不到直接答案，请明确说明。
直接输出回答内容，不要包含任何前缀（如"回答："）。\
"""


def find_paper_file(query: str, papers_dir: Path) -> Path:
    """
    Resolve a paper query to a file path.

    Priority:
    1. Direct file path (absolute or relative)
    2. Exact filename in papers_dir
    3. Partial match (prefer .tex over .pdf)
    """
    # Direct path
    direct = Path(query)
    if direct.exists():
        return direct

    candidates = list(papers_dir.glob("*.tex")) + list(papers_dir.glob("*.pdf"))

    # Exact filename
    for f in candidates:
        if f.name == query:
            return f

    # Partial match (case-insensitive)
    q_lower = query.lower()
    matches = [f for f in candidates if q_lower in f.name.lower()]
    if not matches:
        raise FileNotFoundError(
            f"在 {papers_dir} 中找不到匹配 '{query}' 的论文文件\n"
            "提示：可以用 Arxiv ID（如 2603.08706）、文件名关键词或完整路径"
        )

    # Prefer LaTeX source for better text quality
    tex = [f for f in matches if f.suffix == ".tex"]
    return tex[0] if tex else matches[0]


def load_paper_text(file_path: Path, max_chars: int) -> str:
    """Extract text from .tex or .pdf, respecting max_chars budget."""
    from paper_tool.pdf_parser import extract_text, extract_text_from_latex

    if file_path.suffix == ".tex":
        return extract_text_from_latex(file_path, max_chars=max_chars)
    else:
        return extract_text(file_path, max_chars=max_chars)


class ChatSession:
    """
    Multi-turn conversation about a single paper.

    The paper text is embedded in the system prompt; conversation turns
    accumulate in self.messages.
    """

    def __init__(self, file_path: Path, title: str = "", authors: str = "") -> None:
        cfg = get_config()
        self._cfg = cfg
        self.file_path = file_path

        # Reserve tokens for conversation history; paper gets the rest
        reserved_for_history = 8000
        paper_char_budget = (cfg.llm_max_input_tokens - reserved_for_history) * 4

        paper_text = load_paper_text(file_path, max_chars=paper_char_budget)
        self.paper_char_count = len(paper_text)

        self._system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            title=title or file_path.stem,
            authors=authors or "（未知）",
            paper_text=paper_text,
        )
        self.messages: list[dict] = []

    def ask(self, question: str, debug: bool = False) -> str:
        """Send a question and return the model's response."""
        import litellm

        self.messages.append({"role": "user", "content": question})

        all_messages = [
            {"role": "system", "content": self._system_prompt},
            *self.messages,
        ]

        kwargs: dict = {
            "model": self._cfg.llm_model,
            "messages": all_messages,
            "max_tokens": 4096,
            "temperature": self._cfg.llm_temperature,
        }
        if self._cfg.openai_base_url:
            kwargs["api_base"] = self._cfg.openai_base_url

        if debug:
            print(f"\n[DEBUG] 发送 {len(all_messages)} 条消息，"
                  f"最新问题: {question[:80]}", flush=True)

        response = litellm.completion(**kwargs)
        choice = response.choices[0]
        answer = choice.message.content or ""
        if not answer:
            answer = getattr(choice.message, "reasoning_content", None) or ""

        self.messages.append({"role": "assistant", "content": answer})
        return answer.strip()

    def reset(self) -> None:
        """Clear conversation history but keep paper context."""
        self.messages.clear()

    @property
    def turn_count(self) -> int:
        return len(self.messages) // 2
