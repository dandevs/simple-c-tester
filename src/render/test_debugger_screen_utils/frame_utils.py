import os

import state as global_state
from .source_utils import event_has_useful_source_line


def filter_line_frames(
    timeline_events,
    time_state_changed,
    source_cache,
    debug_mode,
    cache_key,
    last_cache_key,
    last_cache,
):
    skip_seq = max(1, int(global_state.tsv_skip_seq_lines))

    if last_cache_key == cache_key:
        return last_cache

    frames = [
        event
        for event in timeline_events
        if event_has_useful_source_line(event.file_path, event.line, source_cache)
    ]

    if len(frames) <= 1:
        return frames

    if debug_mode:
        return frames

    if skip_seq <= 1:
        return frames

    filtered = [frames[0]]
    seq_since_emit = 0
    prev = frames[0]
    prev_abs_path = os.path.abspath(prev.file_path)

    for frame in frames[1:]:
        frame_abs_path = os.path.abspath(frame.file_path)
        same_file = frame_abs_path == prev_abs_path
        same_function = frame.function == prev.function
        is_sequential = same_file and same_function and frame.line == (prev.line + 1)

        if is_sequential:
            seq_since_emit += 1
            if seq_since_emit >= skip_seq:
                filtered.append(frame)
                seq_since_emit = 0
        else:
            filtered.append(frame)
            seq_since_emit = 0

        prev = frame
        prev_abs_path = frame_abs_path

    if filtered[-1] != frames[-1]:
        filtered.append(frames[-1])

    return filtered


def ensure_selected_frame_index(selected_frame_index, total_frames):
    if total_frames <= 0:
        return -1

    if selected_frame_index < 0 or selected_frame_index >= total_frames:
        return 0

    return selected_frame_index


def compute_frame_cards_window(
    selected_frame_index, total, code_widget_height, card_count_override=None
):
    if total <= 0:
        return (0, 0)

    height = max(1, code_widget_height) if code_widget_height else 1

    lines_above = max(0, int(global_state.tsv_lines_above))
    lines_below = max(0, int(global_state.tsv_lines_below))
    code_line_count = 1 + lines_above + lines_below
    card_height = 1 + code_line_count
    card_count = max(1, (height + 1) // (card_height + 1))
    card_count = min(total, card_count)

    center = selected_frame_index
    start = max(0, center - (card_count // 2))
    end = start + card_count
    if end > total:
        end = total
        start = max(0, end - card_count)
    return (start, end)
