"""Quant engine: indicators, factors, and the checks that keep them honest.

Everything here is a pure function over a price panel. No I/O, no session, no
global state — which is what makes a look-ahead injection test possible at all:
poison the future of the input, recompute, and assert nothing moved.

The boundary is deliberate. `app.quant` computes; `app.quant.jobs` (which does
hold a session) loads, calls, and persists. A function that reached for the
database could not be given a poisoned frame.
"""
