from ml_service.text_lines import group_lines


def box(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def test_words_of_one_line_are_joined_in_reading_order() -> None:
    # EasyOCR output for a "LAN CAN / 39 Hàng Cót" overlay, in its own (unordered) order
    detections = [
        (box(150, 300, 300, 340), "LAN CAN", 0.98),
        (box(150, 360, 175, 390), "39", 0.9),
        (box(260, 358, 300, 392), "Cót", 0.85),
        (box(420, 700, 480, 720), "TikTok", 1.0),
        (box(185, 356, 250, 392), "Hàng", 0.7),
    ]
    assert [line["text"] for line in group_lines(detections)] == [
        "LAN CAN",
        "39 Hàng Cót",
        "TikTok",
    ]


def test_far_apart_text_on_the_same_row_stays_separate() -> None:
    detections = [(box(10, 100, 60, 120), "OPEN", 0.9), (box(400, 100, 460, 120), "@kjta", 0.9)]
    assert [line["text"] for line in group_lines(detections)] == ["OPEN", "@kjta"]


def test_stacked_overlapping_boxes_do_not_chain_into_one_line() -> None:
    # each box overlaps the next one vertically, but the first and last are on different lines
    detections = [
        (box(10, 100, 60, 130), "a1", 0.9),
        (box(70, 112, 120, 142), "b2", 0.9),
        (box(130, 124, 180, 154), "c3", 0.9),
    ]
    assert [line["text"] for line in group_lines(detections)] == ["a1 b2", "c3"]


def test_confidence_is_weighted_by_text_length() -> None:
    detections = [(box(0, 0, 100, 20), "Chocobomb", 1.0), (box(105, 0, 115, 20), "x", 0.0)]
    [line] = group_lines(detections)
    assert line["confidence"] == 0.9
