"""Local-only coarse scene classification with non-content summaries."""

from __future__ import annotations

from dataclasses import dataclass

from app.perception.models import OCRResult, SceneAnalysis, WindowInfo


@dataclass(frozen=True, slots=True)
class _SceneRule:
    category: str
    process_tokens: tuple[str, ...]
    text_tokens: tuple[str, ...]
    summary: str


_RULES = (
    _SceneRule(
        category="coding",
        process_tokens=("code", "cursor", "pycharm", "idea", "xcode", "terminal", "iterm"),
        text_tokens=("traceback", "exception", "function", "class ", "def ", "git "),
        summary="用户正在使用编程或终端工具。",
    ),
    _SceneRule(
        category="editing",
        process_tokens=("resolve", "premiere", "afterfx", "photoshop", "final cut"),
        text_tokens=("timeline", "render", "export", "时间线", "渲染"),
        summary="用户正在进行内容编辑。",
    ),
    _SceneRule(
        category="game",
        process_tokens=("steam", "game", "unity"),
        text_tokens=("fps", "match", "achievement", "游戏"),
        summary="用户可能正在进行游戏活动。",
    ),
    _SceneRule(
        category="reading",
        process_tokens=("chrome", "safari", "firefox", "edge", "preview"),
        text_tokens=("article", "documentation", "chapter", "文档", "阅读"),
        summary="用户正在浏览或阅读内容。",
    ),
)


class LocalSceneClassifier:
    def classify(self, window: WindowInfo, ocr: OCRResult) -> SceneAnalysis:
        process = window.process_name.casefold()
        text = ocr.text.casefold()[:20_000]
        best_rule: _SceneRule | None = None
        best_score = 0
        for rule in _RULES:
            process_hit = any(token in process for token in rule.process_tokens)
            text_hit = any(token.casefold() in text for token in rule.text_tokens)
            score = int(process_hit) * 2 + int(text_hit)
            if score > best_score:
                best_rule = rule
                best_score = score
        if best_rule is None:
            return SceneAnalysis(
                category="unknown",
                summary="用户正在使用普通桌面应用。",
                confidence=0.25,
            )
        confidence = 0.85 if best_score >= 3 else 0.65
        return SceneAnalysis(
            category=best_rule.category,
            summary=best_rule.summary,
            confidence=confidence,
        )
