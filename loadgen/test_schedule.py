"""Self-check for the arrival schedule split. Run: python -m pytest loadgen/test_schedule.py
or just: python loadgen/test_schedule.py"""
from app.main import percentile


def test_percentile():
    assert percentile([], .5) == 0
    assert percentile([1.0], .99) == 1.0
    assert percentile([float(n) for n in range(1, 101)], .95) == 95.0


def test_schedule_covers_every_request_exactly_once():
    # Each worker takes offset, offset+stride, ... The union must be the whole plan,
    # with no duplicate and no gap, or the reported rate is wrong.
    for planned, workers in [(500, 4), (7, 4), (1, 1), (1000, 3), (3, 3)]:
        due = sorted(
            (index * workers + offset)
            for offset in range(workers)
            for index in range(len(range(offset, planned, workers)))
        )
        assert due == list(range(planned)), (planned, workers)


def test_schedule_is_evenly_paced():
    rps, workers, planned = 100.0, 4, 400
    times = sorted(
        (index * workers + offset) / rps
        for offset in range(workers)
        for index in range(len(range(offset, planned, workers)))
    )
    gaps = [round(b - a, 6) for a, b in zip(times, times[1:])]
    assert set(gaps) == {round(1 / rps, 6)}


if __name__ == "__main__":
    test_percentile()
    test_schedule_covers_every_request_exactly_once()
    test_schedule_is_evenly_paced()
    print("ok")
