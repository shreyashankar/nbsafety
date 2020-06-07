# -*- coding: utf-8 -*-
import logging

from .utils import assert_bool, make_safety_fixture

logging.basicConfig(level=logging.ERROR)

# Reset dependency graph before each test
_safety_fixture, _safety_state, run_cell_ = make_safety_fixture(setup_cells=['from fakelib import DotDict'])
run_cell = run_cell_


def stale_detected():
    return _safety_state[0].test_and_clear_detected_flag()


def assert_detected(msg=''):
    assert_bool(stale_detected(), msg=msg)


def assert_not_detected(msg=''):
    assert_bool(not stale_detected(), msg=msg)


def test_basic():
    run_cell('d = DotDict()')
    run_cell('d.x = DotDict()')
    run_cell('d.y = DotDict()')
    run_cell('d.x.a = 5')
    # run_cell('d.x.b = 6')
    run_cell('logging.info(d.x.a)')
    assert_not_detected()
    run_cell('logging.info(d.x)')
    assert_not_detected()
    run_cell('d.y.a = 7')
    run_cell('d.y.b = 8')
    # run_cell('logging.info(d.y.a)')
    assert_not_detected()
    run_cell('logging.info(d.y)')
    assert_not_detected()
    run_cell('x = d.x.a + 9')
    run_cell('y = d.y.a + 9')
    run_cell('d.y.a = 9')
    run_cell('logging.info(x)')
    assert_not_detected('`x` independent of changed `d.y.a`')
    run_cell('logging.info(y)')
    assert_detected('`y` depends on changed `d.y.a`')
    run_cell('logging.info(d.x.a)')
    assert_not_detected()
    run_cell('logging.info(d.x)')
    assert_not_detected()
    run_cell('logging.info(d)')
    assert_not_detected()
    run_cell('d.y = 10')
    run_cell('logging.info(x)')
    assert_not_detected('`x` independent of changed `d.y`')
    run_cell('logging.info(y)')
    assert_detected('`y` depends on changed `d.y`')
