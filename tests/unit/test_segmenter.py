"""Incremental multilingual segmentation and boundary regressions."""

import pytest
from app.pipelines import DialogueSegmenter


def collect(chunks: list[str], **options: int) -> list[str]:
    segmenter = DialogueSegmenter("turn_test", **options)
    output: list[str] = []
    for chunk in chunks:
        output.extend(segment.text for segment in segmenter.feed(chunk))
    output.extend(segment.text for segment in segmenter.flush())
    return output


def test_chinese_japanese_and_reaction_boundaries() -> None:
    chinese = DialogueSegmenter("turn_test")
    assert chinese.feed("你好。") == []
    assert [segment.text for segment in chinese.feed("今天一起努力吧！")] == [
        "你好。今天一起努力吧！"
    ]
    assert collect(["これはテストです。次も大丈夫！"]) == [
        "これはテストです。",
        "次も大丈夫！",
    ]
    assert collect(["嗯？", "我听到了。"]) == ["嗯？", "我听到了。"]
    assert collect(["吾之爆裂魔法……", "准备完毕！"]) == [
        "吾之爆裂魔法……",
        "准备完毕！",
    ]


def test_english_abbreviation_decimal_and_streaming_period() -> None:
    segmenter = DialogueSegmenter("turn_test")

    assert segmenter.feed("Dr. Smith paid 3.14 dollars.") == []
    first = segmenter.feed(" Really?")
    assert [segment.text for segment in first] == ["Dr. Smith paid 3.14 dollars.", "Really?"]
    assert segmenter.flush() == []


def test_max_length_prefers_weak_pause_and_flushes_remainder() -> None:
    assert collect(
        ["这是一段比较长的回复，后面还有必须等待的内容"],
        min_chars=4,
        max_chars=12,
    ) == ["这是一段比较长的回复，", "后面还有必须等待的内容"]


def test_indices_are_monotonic_across_feed_and_flush() -> None:
    segmenter = DialogueSegmenter("turn_test", min_chars=2)
    segments = segmenter.feed("第一句。第二句！") + segmenter.flush()

    assert [segment.index for segment in segments] == [0, 1]
    assert all(segment.turn_id == "turn_test" for segment in segments)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(0, 42), (10, 9)],
)
def test_invalid_length_configuration_is_rejected(minimum: int, maximum: int) -> None:
    with pytest.raises(ValueError):
        DialogueSegmenter("turn_test", min_chars=minimum, max_chars=maximum)
