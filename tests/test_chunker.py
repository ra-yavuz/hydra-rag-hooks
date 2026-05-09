from hydra_rag_hooks.chunker import chunk_text


def test_empty():
    assert chunk_text("") == []


def test_short_text_one_chunk():
    text = "hello\nworld\n"
    chunks = chunk_text(text, target_chars=1500, overlap_chars=200)
    assert len(chunks) == 1
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2


def test_no_mid_line_split():
    lines = [f"line {i:04d}\n" for i in range(200)]
    text = "".join(lines)
    chunks = chunk_text(text, target_chars=300, overlap_chars=50)
    for c in chunks:
        # chunk text must be a sequence of complete lines
        for ln in c.text.splitlines():
            assert ln.startswith("line ")


def test_overlap_makes_progress():
    lines = [f"line {i}\n" for i in range(50)]
    text = "".join(lines)
    chunks = chunk_text(text, target_chars=120, overlap_chars=30)
    assert len(chunks) >= 2
    starts = [c.start_line for c in chunks]
    assert starts == sorted(starts)
    assert starts[1] > starts[0]


def test_long_single_line():
    # A single long line still produces one chunk (no mid-line split allowed).
    text = "x" * 10000
    chunks = chunk_text(text, target_chars=1500, overlap_chars=200)
    assert len(chunks) == 1
    assert chunks[0].start_line == 1 and chunks[0].end_line == 1
