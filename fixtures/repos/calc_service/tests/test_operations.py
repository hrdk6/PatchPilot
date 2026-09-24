from calc_service.operations import add, average, percentage, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2


def test_percentage_basic():
    assert percentage(25, 50) == 50.0


def test_percentage_with_zero_whole():
    # An empty bucket expects nothing, so nothing was missed: 0%.
    assert percentage(0, 0) == 0.0


def test_average_empty():
    assert average([]) == 0.0
